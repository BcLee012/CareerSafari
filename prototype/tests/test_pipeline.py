"""端到端测试：用模拟的招聘推文文本跑通 fetch → extract → 存储 → 查询 整链路。

未配置 LLM 时走 mock，确保即使没 API key 也能跑通。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# 让 import 找到 app 包
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 强制使用临时数据库，避免污染
TMP_DB = Path(tempfile.gettempdir()) / "careersafari_test.db"
if TMP_DB.exists():
    TMP_DB.unlink()

# 设置环境变量后再 import
os.environ["LLM_PROVIDER"] = "mock"
os.environ["CAREERSAFARI_DB"] = str(TMP_DB)

from app.extractor import extract_jobs  # noqa: E402
from app.storage import Storage  # noqa: E402


SAMPLE_ARTICLE = """
字节跳动 豆包大模型团队 2026 春季校园招聘正式开启！

我们正在寻找对大模型应用与 Agent 工程化充满热情的同学。本次招聘覆盖以下岗位：

【岗位 1：LLM 应用工程师】
工作地点：北京 / 上海
岗位职责：
1. 参与豆包 / 扣子（Coze）智能体平台后端开发；
2. 设计 prompt 与 agent 调用链，搭建评测体系；
3. 与算法团队协作，把 LLM 能力落地到业务场景。
任职要求：
- 本科及以上，计算机相关专业；
- 熟悉 Python，熟悉至少一种 LLM 框架（LangChain / LlamaIndex 等）；
- 有 RAG、Agent 项目经验者优先。

【岗位 2：AI 产品经理（Agent 方向）】
工作地点：北京
岗位职责：
1. 负责扣子智能体产品的功能规划与迭代；
2. 撰写 PRD，对接算法、设计、研发；
3. 通过用户研究与数据分析驱动产品决策。
任职要求：
- 硕士及以上，2 年以上产品经验；
- 对 LLM 产品形态有深入理解。

【实习机会】
日常实习生同步开放，欢迎 26/27 届同学投递。日薪 400-600 元，提供免费三餐。
"""


def test_extraction():
    print("\n[1] 抽取模块测试")
    jobs = extract_jobs(SAMPLE_ARTICLE, hint_company="字节跳动")
    assert jobs, "mock 模式应返回至少 1 个 JobDraft"
    print(f"   - 抽取到 {len(jobs)} 个岗位（mock 模式）")
    for j in jobs:
        print(f"   · {j.title} @ {j.company} | {j.category} | 地点 {j.location}")
    return jobs


def test_storage(jobs):
    print("\n[2] 存储模块测试（临时 DB）")
    storage = Storage(db_path=TMP_DB)
    ids = storage.insert_jobs(
        jobs,
        source_url="https://example.com/article/123",
        source_tier="P1",
        raw_text=SAMPLE_ARTICLE[:5000],
    )
    print(f"   - 插入 ids: {ids}")
    listed = storage.list_jobs(status="pending")
    assert len(listed) == len(jobs), f"列出数量 {len(listed)} 应等于 {len(jobs)}"
    print(f"   - 按 status=pending 列出: {len(listed)} 条")

    # 校准其中一条
    first_id = ids[0]
    calibrated = storage.calibrate(first_id, status="calibrated", confidence=0.9,
                                    notes="测试校准")
    assert calibrated["status"] == "calibrated"
    print(f"   - 校准 id={first_id}: status={calibrated['status']}, confidence={calibrated['confidence']}")

    stats = storage.stats()
    print(f"   - 统计: total={stats['total']}, by_status={stats['by_status']}")
    assert stats["by_status"]["pending"] == len(jobs) - 1
    assert stats["by_status"]["calibrated"] == 1
    return storage


def test_fetcher_clean_html():
    print("\n[3] fetcher 文本清洗测试")
    from app.fetcher import clean_html
    raw_html = """
    <html><head><title>测试</title></head>
    <body>
      <script>window.alert('x')</script>
      <h1>字节跳动 2026 校招</h1>
      <p>招聘 LLM 应用工程师</p>
      <ul><li>北京</li><li>上海</li></ul>
    </body></html>
    """
    cleaned = clean_html(raw_html)
    assert cleaned and "字节跳动" in cleaned
    print(f"   - 清洗后长度: {len(cleaned)} 字符")
    print(f"   - 内容预览: {cleaned[:80]}...")


def test_json_parser():
    print("\n[4] LLM JSON 解析容错测试")
    from app.extractor import _parse_jobs_json
    # markdown 代码块包裹
    md = """```json
{"jobs": [{"company": "A", "title": "B", "category": "AI", "responsibilities": ["x"]}]}
```"""
    jobs = _parse_jobs_json(md)
    assert len(jobs) == 1 and jobs[0].title == "B"
    print(f"   - markdown 包裹: {len(jobs)} 个")
    # 裸 JSON
    bare = '[{"company":"X","title":"Y"}]'
    jobs2 = _parse_jobs_json(bare)
    assert len(jobs2) == 1 and jobs2[0].company == "X"
    print(f"   - 裸 JSON 数组: {len(jobs2)} 个")
    # 非法
    bad = "this is not json"
    jobs3 = _parse_jobs_json(bad)
    assert jobs3 == []
    print(f"   - 非法输入: 0 个（符合预期）")


if __name__ == "__main__":
    test_json_parser()
    test_fetcher_clean_html()
    jobs = test_extraction()
    test_storage(jobs)
    print("\n✅ 所有测试通过")