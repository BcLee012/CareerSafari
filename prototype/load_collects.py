"""加载器：把 collects/*.json（真实抓取的结构化结果）导入数据库。

设计意图（与 auto_collect.py 的区别）：
  - auto_collect.py：**按 URL 猜来源等级**，字段靠关键词盲猜 —— 只能拿到元数据
  - load_collects.py：加载**已经真正抓取过页面并结构化好的** JSON —— 字段完整

collects/*.json 由「采集器」产出。当前采集器 = 人工 + 智能体（agent-webfetch），
将来可替换为 Playwright + LLM 的自动化管线，但 JSON 格式不变。

JSON schema：
{
  "source_url": str,
  "source_tier": "P1|P2|P3|P4",
  "page_kind": "岗位详情页|岗位列表页|招聘公告页|门户首页",
  "credibility_note": str,
  "collected_at": str,
  "collector": str,
  "jobs": [ { company, title, location, category, job_type,
              responsibilities[], requirements[], skills[],
              salary_text, experience_text, education_text, extra_fields{} } ]
}

Usage:
    python load_collects.py            # 加载 collects/ 下所有 json
    python load_collects.py --dry-run  # 只打印不写库
    python load_collects.py --replace  # 先清空原有采集数据再导入
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.extractor import JobDraft  # noqa: E402
from app.storage import Storage  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("load_collects")

COLLECTS_DIR = ROOT / "collects"


def apply_salary_refs(path: Path, storage: Storage) -> None:
    """把薪资参考合到匹配岗位的 extras.salary_ref，不覆盖 salary_text。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    refs = data.get("refs", [])
    print(f"\n💰 {path.name}（薪资参考，{len(refs)} 条）")
    matched = 0
    for ref in refs:
        m = ref.get("match", {})
        for j in storage.list_jobs(limit=500):
            if all(j.get(k) == v for k, v in m.items()):
                # 合入 extras（合并而非覆盖）
                extras = dict(j.get("extras") or {})
                extras["salary_ref"] = ref["salary_ref"]
                extras["salary_ref_tier"] = ref.get("tier")
                extras["salary_ref_url"] = ref.get("evidence_url")
                extras["salary_ref_note"] = ref.get("evidence_note")
                # 写回
                with storage._conn() as conn:  # noqa: SLF001
                    conn.execute(
                        "UPDATE jobs SET extras_json = ? WHERE id = ?",
                        (json.dumps(extras, ensure_ascii=False), j["id"]),
                    )
                matched += 1
                print(f"   ✓ 合入 id={j['id']} {j['company']} | {j['title'][:30]} → {ref['salary_ref']}")
    print(f"   共合入 {matched} 条岗位")


def load_file(path: Path) -> tuple[list[JobDraft], dict, list]:
    """读一个 collects JSON → (JobDraft 列表, 元信息, 每岗 deadline 列表)。"""
    data = json.loads(path.read_text(encoding="utf-8"))

    meta = {
        "source_url": data.get("source_url"),
        "source_tier": data.get("source_tier"),
        "page_kind": data.get("page_kind"),
        "collector": data.get("collector"),
        "credibility_note": data.get("credibility_note"),
        "collected_at": data.get("collected_at"),
        "recruit_deadline": data.get("recruit_deadline"),  # 该页面/批次整体截止
    }

    drafts: list[JobDraft] = []
    per_job_deadlines: list = []
    for j in data.get("jobs", []):
        per_job_dl = j.get("recruit_deadline")
        page_dl = meta.get("recruit_deadline")
        effective_dl = per_job_dl or page_dl

        drafts.append(
            JobDraft(
                company=(j.get("company") or "").strip(),
                title=(j.get("title") or "").strip(),
                location=(j.get("location") or "").strip(),
                category=(j.get("category") or "").strip(),
                responsibilities=list(j.get("responsibilities") or []),
                requirements=list(j.get("requirements") or []),
                skills=list(j.get("skills") or []),
                salary_text=(j.get("salary_text") or "").strip(),
                experience_text=(j.get("experience_text") or "").strip(),
                education_text=(j.get("education_text") or "").strip(),
                job_type=(j.get("job_type") or "").strip(),
                extras={
                    "collector": meta["collector"],
                    "credibility_note": meta["credibility_note"],
                    "extra_fields": j.get("extra_fields") or {},
                    "collected_at": meta["collected_at"],
                    "recruit_deadline_per_job": per_job_dl,
                },
            )
        )
        per_job_deadlines.append(effective_dl)

    return drafts, meta, per_job_deadlines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    ap.add_argument("--replace", action="store_true", help="先删除所有自动采集记录再导入")
    args = ap.parse_args()

    storage = Storage()

    if args.replace and not args.dry_run:
        with storage._conn() as conn:  # noqa: SLF001
            cur = conn.execute(
                "DELETE FROM jobs WHERE extras_json LIKE '%\"collector\"%'"
            )
        logger.info("已清理旧的采集记录：%d 条", cur.rowcount)

    # 常规运行前也自动去重（避免反复 load 后膨胀）
    if not args.dry_run:
        with storage._conn() as conn:
            cur = conn.execute(
                "DELETE FROM jobs WHERE id NOT IN (SELECT MIN(id) FROM jobs GROUP BY company, title, location)"
            )
        if cur.rowcount:
            logger.info("自动去重：%d 条", cur.rowcount)

    files = sorted(COLLECTS_DIR.glob("*.json"))
    if not files:
        logger.warning("collects/ 下没有 JSON 文件")
        return

    total_jobs = 0
    for f in files:
        # 先 peek 文件头，判断是否为薪资参考类型
        peek = json.loads(f.read_text(encoding="utf-8"))
        if peek.get("_schema") == "salary_refs":
            if not args.dry_run:
                apply_salary_refs(f, storage)
            else:
                print(f"\n💰 {f.name}（薪资参考，dry-run 跳过）")
            continue

        drafts, meta, per_job_dls = load_file(f)
        print(f"\n📄 {f.name}")
        print(f"   来源等级 {meta['source_tier']} · 页面类型 {meta['page_kind']} · 采集器 {meta['collector']}")
        print(f"   可信度说明：{(meta['credibility_note'] or '')[:80]}...")
        print(f"   含 {len(drafts)} 个岗位：")

        for d, dl in zip(drafts, per_job_dls):
            fill = "◎" if d.responsibilities and d.requirements else "○"
            dl_mark = f"·截止{dl}" if dl else ""
            print(f"     {fill} {d.title} | {d.category} | {d.location} | "
                  f"职责{len(d.responsibilities)}条 要求{len(d.requirements)}条 技能{len(d.skills)}项 {dl_mark}")

        if not args.dry_run:
            # 逐条插入，每条带各自的 deadline
            ids = []
            for d, dl in zip(drafts, per_job_dls):
                ids.extend(storage.insert_jobs(
                    [d],
                    source_url=meta["source_url"],
                    source_tier=meta["source_tier"],
                    page_kind=meta["page_kind"],
                    recruit_deadline=dl,
                ))
                if dl and not args.dry_run:
                    pass  # DEBUG_OK
            print(f"   → 已入库 ids={ids[0]}..{ids[-1]}（共 {len(ids)} 条）")
        total_jobs += len(drafts)

    if not args.dry_run:
        print(f"\n✅ 共导入 {total_jobs} 个岗位。数据库现状：{storage.stats()}")
        # 跑完后再去重一次
        with storage._conn() as conn:  # noqa: SLF001
            cur = conn.execute(
                "DELETE FROM jobs WHERE id NOT IN (SELECT MIN(id) FROM jobs GROUP BY company, title, location)"
            )
        if cur.rowcount:
            print(f"🧹 最终去重：删除 {cur.rowcount} 条重复。最终：{storage.stats()}")
    else:
        print(f"\n（dry-run）共 {total_jobs} 个岗位，未写库")


if __name__ == "__main__":
    main()