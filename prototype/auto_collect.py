"""自动采集脚本：根据 URL 域名对来源可信度自动评分 + 入库。

评分规则（P1-P4），与 PRD §5.1 对齐：
  P1 企业官方：企业招聘官网、官方技术博客、官方社区（bbs.*）+ 教育部 24365
  P2 招聘平台：BOSS直聘、实习僧、智联、拉勾、猎聘、脉脉招聘版块
  P3 转发/聚合：高校就业网、第三方招聘聚合站（quanzhi、nowcoder、jiuyeqiao、deizao 等）
  P4 UGC：小红书、知乎、OfferShow、脉脉讨论区等用户内容

Usage:
    python auto_collect.py              # 跑 W1 三家预置 query
    python auto_collect.py --list       # 列出已入库的来源
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

# 让脚本可以找到 app 包
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LLM_PROVIDER", "mock")  # 自动收集暂不调 LLM，原始文本直接入库

from app.storage import Storage  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("auto_collect")

# ---------- 可信度分类器 ----------


TIER_RULES: list[tuple[str, str, list[str]]] = [
    # (tier, 描述, host_suffix 列表)
    ("P1", "企业官方招聘/技术博客/官方社区", [
        "jobs.bytedance.com", "bytedance.com", "seed.bytedance.com",
        "careers.dji.com", "we.dji.com", "bbs.dji.com", "dji.com",
        "talent.didiglobal.com", "didiglobal.com", "didichuxing.com",
        "hr.tencent.com", "careers.tencent.com",
        "talent.alibaba.com", "alibaba.com",
        "lagou.com",  # 拉勾本身，可视为 P2，部分企业官方账号入驻
    ]),
    ("P2", "招聘平台（BOSS/实习僧/智联/拉勾/猎聘）", [
        "boss.com", "bosszhipin.com", "zhipin.com",
        "shixiseng.com", "lagou.com", "liepin.com",
        "zhaopin.com", "51job.com", "jobui.com",
    ]),
    ("P3", "第三方招聘聚合 / 高校就业网 / 转发", [
        "24365.smartedu.cn",  # 教育部全国大学生就业服务平台
        "quanzhi.com", "nowcoder.com", "jiuyeqiao.cn", "deizao.net",
        "job.sicau.edu.cn",  # 高校就业网（通用 pattern：job.<学校域名>）
        "hbkdjyb",  # 河北科大招生就业处
    ]),
    ("P4", "UGC 内容（小红书/知乎/OfferShow/脉脉讨论）", [
        "xiaohongshu.com", "xhslink.com",
        "zhihu.com", "offer.show", "offer100.com",
        "maimai.cn",  # 脉脉（含讨论区）
    ]),
]

# 高/中/低 可信度映射（用于前端排序权重）
TIER_WEIGHT = {"P1": 1.0, "P2": 0.8, "P3": 0.5, "P4": 0.3}


@dataclass
class CollectedSource:
    company: str
    title: str
    location: str = ""
    category: str = ""  # AI / 供应链 / 运筹优化
    job_type: str = ""  # 校招/实习/社招
    source_url: str = ""
    source_tier: str = ""
    tier_weight: float = 0.0
    raw_text_snippet: str = ""
    extras: dict = None  # 评分依据、检测到的关键词

    def to_dict(self):
        d = asdict(self)
        d["extras"] = self.extras or {}
        return d


def classify_url(url: str) -> tuple[str, float, str]:
    """根据 URL host 返回 (tier, weight, 命中规则描述)。未匹配则 P3 + 0.3。"""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return "P3", 0.3, "URL 解析失败"
    host = host.lower()

    for tier, desc, suffixes in TIER_RULES:
        for suf in suffixes:
            if host == suf or host.endswith("." + suf):
                return tier, TIER_WEIGHT[tier], desc
    return "P3", 0.3, "未匹配已知规则，默认转发/聚合"


# ---------- 关键词驱动的轻量抽取（无 LLM 时的兜底） ----------


CATEGORY_KEYWORDS = {
    "AI": ["LLM", "Agent", "大模型", "多模态", "视觉", "具身", "NLP", "AIGC", "Seed", "豆包", "通义", "文心", "扣子", "RAG", "Prompt"],
    "供应链": ["供应链", "采购", "S&OP", "库存", "物流", "APS", "PMC", "计划", "仓储", "生产排程", "需求预测"],
    "运筹优化": ["运筹", "调度", "路径规划", "定价", "收益管理", "强化学习", "优化", "ETA", "司乘匹配", "供需"],
}

JOB_TYPE_KEYWORDS = {
    "校招": ["校招", "应届", "应届生", "毕业生", "校园招聘", "26 届", "27 届", "2026 届", "2027 届"],
    "实习": ["实习", "实习生", "日薪", "实习岗位"],
    "社招": ["社招", "社会招聘", "有经验", "工作经验", "5 年", "3 年"],
}

CITY_KEYWORDS = ["北京", "上海", "深圳", "杭州", "广州", "成都", "南京", "苏州", "武汉", "西安", "厦门", "香港"]


def detect_category(text: str) -> tuple[str, list[str]]:
    hits = {}
    for cat, kws in CATEGORY_KEYWORDS.items():
        h = [k for k in kws if k in text]
        if h:
            hits[cat] = h
    if not hits:
        return "其他", []
    # 取命中关键词最多的类目
    cat = max(hits, key=lambda c: len(hits[c]))
    return cat, hits[cat]


def detect_job_type(text: str) -> str:
    for jt, kws in JOB_TYPE_KEYWORDS.items():
        if any(k in text for k in kws):
            return jt
    return "不明确"


def detect_location(text: str) -> str:
    found = [c for c in CITY_KEYWORDS if c in text]
    return " / ".join(found[:4]) if found else ""


def extract_title(company_hint: str, text: str) -> str:
    """从原始文本中挑第一个像岗位名的片段。"""
    # 优先抓带"工程师" / "招聘"的词组
    m = re.search(r"([\u4e00-\u9fa5A-Za-z0-9]{2,20}(?:工程师|实习生|算法|产品|经理|研究员|助理)[\u4e00-\u9fa5A-Za-z0-9（()）\-/]{0,30})", text)
    if m:
        return m.group(1).strip()
    # 次选：截取第二行
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return lines[0][:60] if lines else "（未识别）"


# ---------- 主流程：把 WebSearch/WebFetch 结果入库 ----------


def collect_one(source: dict, default_company: str) -> CollectedSource:
    """source = {"url", "title", "text_snippet", "company_hint"?}"""
    url = source.get("url") or ""
    title = (source.get("title") or "").strip()
    text = source.get("text_snippet") or ""
    company = source.get("company_hint") or default_company

    tier, weight, reason = classify_url(url)
    category, cat_hits = detect_category(text)
    job_type = detect_job_type(text)
    location = detect_location(text)
    real_title = extract_title(company, text) if not title else title[:60]

    return CollectedSource(
        company=company,
        title=real_title,
        location=location,
        category=category,
        job_type=job_type,
        source_url=url,
        source_tier=tier,
        tier_weight=weight,
        raw_text_snippet=text[:600],
        extras={
            "tier_reason": reason,
            "category_hits": cat_hits,
            "original_title": title,
        },
    )


# ---------- 数据写入 ----------
# 直接构造 JobDraft 风格的 dict 给 storage.insert_jobs


def to_job_drafts(sources: List[CollectedSource]):
    """CollectedSource → JobDraft（避开 LLM，直接入库）。

    返回 (drafts, source_meta) 两份，source_meta 用于 insert_jobs 的
    source_url/source_tier/raw_text 参数，确保列字段也被正确写入。
    """
    from app.extractor import JobDraft
    drafts = []
    metas = []
    for s in sources:
        d = JobDraft(
            company=s.company,
            title=s.title,
            location=s.location,
            category=s.category,
            responsibilities=[],
            requirements=[],
            skills=[],
            salary_text="",
            experience_text="",
            education_text="",
            job_type=s.job_type,
            extras={
                "source_tier": s.source_tier,
                "tier_weight": s.tier_weight,
                "auto_collected": True,
                **s.extras,
            },
        )
        drafts.append(d)
        metas.append({
            "source_url": s.source_url,
            "source_tier": s.source_tier,
            "raw_text": s.raw_text_snippet,
        })
    return drafts, metas


# ---------- 预置：W1 三家的真实搜索结果 ----------


W1_RESULTS = [
    # ---- 字节跳动 ----
    {
        "url": "https://jobs.bytedance.com/campus/",
        "title": "字节跳动校园招聘（官网首页）",
        "company_hint": "字节跳动",
        "text_snippet": "字节跳动的使命是激发创造、丰富生活。招聘项目一键投递，北京、上海、广州、香港、新加坡、首尔、东京、悉尼等全球办公室。校招生在字节跳动如何成长，欢迎加入。",
    },
    {
        "url": "https://jobs.bytedance.com/campus/position/7529522766400194823/detail?recomId=3f197e36-db23-11f0-ba25-fa163e5044b2",
        "title": "前端开发工程师-豆包 上海 2026 届校招",
        "company_hint": "字节跳动",
        "text_snippet": "前端开发工程师-豆包 上海正式研发 - 前端 2026 届校园招聘。负责面向 AI 场景的平台系统产品-豆包的业务前端开发工作。熟悉 React 和 TypeScript，2026 届本科及以上学历，计算机、软件工程等相关专业优先。",
    },
    {
        "url": "https://seed.bytedance.com/zh/blog/bytedance-seed-2027-foundation-model-campus-recruitment-is-now-open-internships-included",
        "title": "字节跳动 Seed 2027 届大模型人才校招启动（含实习）",
        "company_hint": "字节跳动",
        "text_snippet": "字节跳动 Seed 团队 2027 届大模型人才校招正式启动，含实习岗位。研究方向：基础大模型、视觉智能、语音智能、机器学习系统、大模型应用、具身智能。工作地点：北京、上海、深圳、杭州、新加坡、圣何塞、西雅图。应届生 2026 年 9 月—2027 年 8 月毕业的本科、硕士及博士同学。",
    },
    {
        "url": "https://www.quanzhi.com/job/6a0134bb5e571018291fb7dd",
        "title": "【26 届校招】测试开发工程师-豆包",
        "company_hint": "字节跳动",
        "text_snippet": "【26 届校招】测试开发工程师-豆包 全职招聘网。2-3 万 x15 薪。年终奖、餐补、五险一金、股票期权、定期体检。负责豆包等产品的质量保障工作。2026 届本科及以上学历。Python/Go/Java 编程基础。",
    },
    {
        "url": "https://job.sicau.edu.cn/employment/zwss/zwss_info/index.html?id=0206f19961f747a590cdfd2e439720a3&enid=266282",
        "title": "豆包 AI 搜索架构工程师-Seed 大模型人才校招",
        "company_hint": "字节跳动",
        "text_snippet": "字节跳动 Seed 团队 2026 届校招：豆包 AI 搜索架构工程师。建设豆包 AI 搜，搭建完整 RAG 功能的 LLM 搜索引擎。北京、上海、深圳、杭州工作地点。",
    },
    # ---- 大疆 ----
    {
        "url": "https://apply.careers.dji.com/campus-recruitment/dji/143359",
        "title": "大疆招聘官网 - 简历投递与校招说明",
        "company_hint": "大疆",
        "text_snippet": "DJI 大疆招聘。本次校园招聘面向 2027 届高校毕业生，优秀的 2026 届毕业生也可适当放宽筛选条件。请登录 DJI 大疆招聘官网 careers.dji.com 或关注 DJI 大疆招聘微信公众号。",
    },
    {
        "url": "https://bbs.dji.com/pro/detail?tid=478892",
        "title": "DJI 大疆 2026 拓疆者秋季校园招聘公告",
        "company_hint": "大疆",
        "text_snippet": "大疆 2026 拓疆者秋季校园招聘正式开启，面向 2026 届海内外应届毕业生。工作城市深圳、上海、北京。招聘岗位：嵌入式、算法、芯片、软件、测试、机械、硬件、光学、产品、质量、工艺、综合类。校招流程：网申、初筛、面试、结果反馈。",
    },
    {
        "url": "https://m.nowcoder.com/feed/main/detail/c7f6e4c1eb1647b895a09a83d4d83667",
        "title": "DJI 大疆 2027 拓疆者校招（牛客网员工转发）",
        "company_hint": "大疆",
        "text_snippet": "DJI 2027 届拓疆者校招：算法、软件、嵌入式、芯片、硬件、光学、机械；也有市场、供应链、产品、设计非技术线。深圳、上海、北京都有坑。投递入口 apply.careers.dji.com。",
    },
    {
        "url": "https://24365.smartedu.cn/student/jobs/17r3EVpR2UPWtAMTS7orU9/detail.html",
        "title": "DJI 大疆 2026 拓疆者秋季校园招聘（教育部就业平台）",
        "company_hint": "大疆",
        "text_snippet": "DJI 大疆 2026 拓疆者秋季校园招聘。招聘 200 人，本科及以上，不限专业。工作地点广东省深圳市。校招流程：简历投递、初筛复筛、面试、结果反馈。",
    },
    # ---- 滴滴 ----
    {
        "url": "http://talent.didiglobal.com/",
        "title": "滴滴人才招聘（官方招聘门户）",
        "company_hint": "滴滴",
        "text_snippet": "滴滴人才招聘，让年轻身经百战。社会招聘、校园招聘、实习生招聘、海外招聘。工作在滴滴，和我们一起改变亿万人的出行。",
    },
    {
        "url": "https://talent.didiglobal.com/social/p/57038",
        "title": "算法工程师 - 滴滴国际化出行交易市场",
        "company_hint": "滴滴",
        "text_snippet": "算法工程师 - 滴滴国际化出行交易市场（IBG MPA）。负责国际化网约车交易策略算法设计和实现，包括司乘匹配、运力调度、流量分发、供需预测、仿真系统。机器学习、强化学习、运筹优化等技术栈。北京。",
    },
    {
        "url": "https://job.jiuyeqiao.cn/zhiwei/90127.html",
        "title": "26 届正式批 - 算法工程师（交易策略）品类交易（滴滴）",
        "company_hint": "滴滴",
        "text_snippet": "滴滴（中国）科技有限公司。负责网约车各环节最中心的交易引擎策略设计：订单分配、运力调度、用户推荐、拼车合乘。技术栈：机器学习、强化学习、运筹优化、因果推断。2026 届毕业生，本科及以上。",
    },
    {
        "url": "https://www.deizao.net/m/index/shixixq/jobid/171447",
        "title": "国际事业群 IBG - 算法实习生（滴滴）",
        "company_hint": "滴滴",
        "text_snippet": "国际事业群 IBG 算法实习生。运用机器学习（因果推断、时序预测、转化率预估）、强化学习等技术构建模型策略，搭建线性规划、整数规划、动态规划等运筹优化模型。硕士及以上在读。杭州。",
    },
    {
        "url": "https://www.quanzhi.com/job/6a9793db67c5b4892cc35397",
        "title": "Marketplace - 算法实习生（滴滴）",
        "company_hint": "滴滴",
        "text_snippet": "Marketplace 算法实习生。智能决策算法实习生（仿真方向）。构建基于深度模型的智能定价系统，优化网约车场景下的定价补贴效率。XGBoost、MILP、DragonNet、运筹优化。北京，硕士及以上，300-400 元/天。",
    },
]


def main():
    storage = Storage()
    print(f"\n开始自动采集 W1 三家公司（{len(W1_RESULTS)} 条来源）\n")

    results: List[CollectedSource] = []
    for src in W1_RESULTS:
        s = collect_one(src, src.get("company_hint", ""))
        results.append(s)
        marker = {
            "P1": "🟢",
            "P2": "🔵",
            "P3": "🟡",
            "P4": "🟠",
        }.get(s.source_tier, "⚪")
        print(f"{marker} [{s.source_tier}] {s.company} | {s.title[:40]:<40} | "
              f"{s.category:<5} | {s.location or '—':<12} | {s.job_type}")
        print(f"    url: {s.source_url}")

    # 入库（用 JobDraft 风格，绕过 LLM）—— 逐条插入以带上各自的 source_meta
    drafts, metas = to_job_drafts(results)
    ids = []
    for d, m in zip(drafts, metas):
        ids.extend(storage.insert_jobs([d], **m))
    print(f"\n✅ 已插入 {len(ids)} 条记录（id={ids[0]}..{ids[-1]}）")

    # 统计
    by_tier = {}
    for s in results:
        by_tier.setdefault(s.source_tier, []).append(s.company)
    print("\n按来源等级统计：")
    for tier in ("P1", "P2", "P3", "P4"):
        if tier in by_tier:
            print(f"  {tier}: {len(by_tier[tier])} 条  ({', '.join(sorted(set(by_tier[tier])))})")

    print(f"\n数据库现状：{storage.stats()}")


if __name__ == "__main__":
    main()