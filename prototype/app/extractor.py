"""抽取模块：从正文文本中抽出结构化岗位信息。

可插拔 LLM 后端：
  - openai（OpenAI 兼容协议，也覆盖 Qwen 的 DashScope 兼容模式）
  - anthropic（Anthropic 原生协议）
  - mock（无 key 时的占位实现，返回 demo 数据，便于先跑通链路）

输出结构：List[JobDraft]，每个 JobDraft 对应 PRD §F1 的字段子集（详见 prompts/extract_jobs.txt）。
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
DEFAULT_PROMPT_PATH = PROMPTS_DIR / "extract_jobs.txt"


@dataclass
class JobDraft:
    """岗位档案草稿（与 PRD §F1 对齐的最小字段集）。"""

    company: str = ""
    title: str = ""
    location: str = ""
    category: str = ""  # 行业大类：AI / 供应链 / 运筹优化 ...
    responsibilities: List[str] = field(default_factory=list)
    requirements: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    salary_text: str = ""
    experience_text: str = ""
    education_text: str = ""
    job_type: str = ""  # 实习 / 校招 / 社招
    extras: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def load_prompt(path: Path | None = None) -> str:
    p = path or DEFAULT_PROMPT_PATH
    return p.read_text(encoding="utf-8")


def extract_jobs(
    text: str,
    *,
    provider: Optional[str] = None,
    prompt_template: Optional[str] = None,
    hint_company: Optional[str] = None,
) -> List[JobDraft]:
    """主入口：从正文抽取岗位列表。

    provider 优先级：参数 > 环境变量 LLM_PROVIDER > "mock"。
    """
    provider = (provider or os.environ.get("LLM_PROVIDER") or "mock").lower()
    prompt = (prompt_template or load_prompt()).format(
        text=text,
        hint_company=hint_company or "（未提供，按文中识别）",
    )

    if provider == "mock":
        return _mock_extract(text)
    if provider == "openai" or provider == "qwen":
        return _openai_extract(prompt)
    if provider == "anthropic":
        return _anthropic_extract(prompt)

    logger.warning("Unknown provider %r, fallback to mock", provider)
    return _mock_extract(text)


# -------------------- 后端实现 --------------------


def _openai_extract(prompt: str) -> List[JobDraft]:
    try:
        from openai import OpenAI  # type: ignore
    except ImportError:
        logger.error("openai SDK 未安装，请 `pip install openai`")
        return []

    base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("QWEN_BASE_URL")
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("QWEN_API_KEY")
    model = os.environ.get("OPENAI_MODEL") or os.environ.get("QWEN_MODEL") or "gpt-4o-mini"

    if not api_key:
        logger.warning("OPENAI_API_KEY 未配置，回退 mock")
        return _mock_extract(prompt)

    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是岗位信息抽取助手，严格按要求输出 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        return _parse_jobs_json(content)
    except Exception as e:  # noqa: BLE001
        logger.error("OpenAI 抽取失败：%s", e)
        return []


def _anthropic_extract(prompt: str) -> List[JobDraft]:
    try:
        import anthropic  # type: ignore
    except ImportError:
        logger.error("anthropic SDK 未安装，请 `pip install anthropic`")
        return []

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    model = os.environ.get("ANTHROPIC_MODEL") or "claude-3-5-haiku-latest"
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY 未配置，回退 mock")
        return _mock_extract(prompt)

    client = anthropic.Anthropic(api_key=api_key)
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        content = msg.content[0].text if msg.content else "{}"
        return _parse_jobs_json(content)
    except Exception as e:  # noqa: BLE001
        logger.error("Anthropic 抽取失败：%s", e)
        return []


def _mock_extract(text: str) -> List[JobDraft]:
    """无 LLM 时的占位实现：返回 1-2 个 demo 岗位 + 在日志提示用户接 LLM。"""
    logger.info(
        "[mock 模式] 未调用 LLM，请按 README 配置 LLM_PROVIDER 与 API key。\n"
        "示例返回：1 个 LLM 岗 + 1 个供应链岗，仅用于跑通链路。",
    )
    # 简单根据关键词判断公司/岗位类目
    lower = text.lower()
    company = ""
    for kw in ("字节跳动", "ByteDance", "豆包", "扣子", "coze"):
        if kw in text:
            company = "字节跳动"
            break
    if not company:
        for kw in ("大疆", "DJI", "Insta360", "影石", "拼多多", "滴滴"):
            if kw in text:
                company = kw
                break

    demo_jobs: List[JobDraft] = [
        JobDraft(
            company=company or "示例公司",
            title="LLM 应用工程师（示例）",
            location="北京 / 上海",
            category="AI",
            responsibilities=[
                "参与大模型应用产品（如对话助手、智能体）后端开发",
                "设计 prompt / 调用链 / 评测体系",
            ],
            requirements=[
                "本科及以上，计算机相关专业",
                "熟悉 Python、至少一种 LLM 应用框架",
            ],
            skills=["Python", "LLM", "RAG", "Prompt 工程"],
            salary_text="（mock，无薪资信息）",
            experience_text="1 年以上（含实习）",
            education_text="本科及以上",
            job_type="社招",
            extras={"_note": "mock 数据，请接入真实 LLM 后重新抽取"},
        ),
        JobDraft(
            company=company or "示例公司",
            title="供应链计划专员（示例）",
            location="深圳",
            category="供应链",
            responsibilities=[
                "负责需求预测与库存计划",
                "协同采购、生产、仓储推进 S&OP",
            ],
            requirements=[
                "本科及以上，供应链/物流/工业工程相关专业",
                "熟悉 ERP / APS 系统",
            ],
            skills=["需求预测", "S&OP", "SAP"],
            salary_text="（mock，无薪资信息）",
            experience_text="1-3 年",
            education_text="本科及以上",
            job_type="社招",
            extras={"_note": "mock 数据"},
        ),
    ]
    # 粗略按文章长度截断以避免产生"幻觉"——mock 模式下这是合理的"仅示意"
    return demo_jobs[: min(2, max(1, text.count("招聘") + text.count("岗位")))]


# -------------------- JSON 解析 --------------------


def _parse_jobs_json(content: str) -> List[JobDraft]:
    """解析 LLM 输出（容错：可能包在 markdown 代码块里）。"""
    text = content.strip()
    if text.startswith("```"):
        # 去掉 ```json ... ```
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 截取首个 { ... } 块再试一次
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            logger.error("LLM 输出非 JSON：%s", text[:200])
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            logger.error("LLM JSON 二次解析失败：%s / %s", e, text[:200])
            return []

    # 兼容两种结构：{"jobs": [...]} 或直接 [...]
    if isinstance(data, dict) and "jobs" in data:
        items = data["jobs"]
    elif isinstance(data, list):
        items = data
    else:
        items = []

    jobs: List[JobDraft] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            jobs.append(
                JobDraft(
                    company=str(item.get("company") or "").strip(),
                    title=str(item.get("title") or "").strip(),
                    location=str(item.get("location") or "").strip(),
                    category=str(item.get("category") or "").strip(),
                    responsibilities=_as_str_list(item.get("responsibilities")),
                    requirements=_as_str_list(item.get("requirements")),
                    skills=_as_str_list(item.get("skills")),
                    salary_text=str(item.get("salary_text") or "").strip(),
                    experience_text=str(item.get("experience_text") or "").strip(),
                    education_text=str(item.get("education_text") or "").strip(),
                    job_type=str(item.get("job_type") or "").strip(),
                    extras={k: v for k, v in item.items()
                            if k not in {"company", "title", "location", "category",
                                         "responsibilities", "requirements", "skills",
                                         "salary_text", "experience_text", "education_text",
                                         "job_type"}},
                )
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("跳过一条非法 job 记录: %s", e)
    return jobs


def _as_str_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        # 尝试按换行 / 中英文逗号切分
        parts = re.split(r"[\n\r,，；;]+", v)
        return [p.strip() for p in parts if p.strip()]
    return [str(v).strip()]