"""批量回填 recruit_deadline 字段到所有 collects/*.json。

依据：
  - DJI 拓疆者 2026 届校招：网申 2025-07-02 ~ 2025-08-13（已截止）
  - JD TET 2026 届：~ 2026-04-15（已截止）
  - JD 新星计划 2026 届：~ 2026-04-30（已截止）
  - 滴滴算法实习（来自 P3 quanzhi）：2026-12-30
  - 滴滴 IBG 算法实习：2026-12-30
  - 美团/京东校招（默认）：~ 2026-08-31（校招批次通常 6-8 月）
  - 美团/京东社招：无截止（社招常年）
  - 美团/京东实习：无截止
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COLLECTS = ROOT / "collects"

# 每条 collect 的"页面级"截止日期（整体批次）
PAGE_DEADLINES = {
    "dji-campus-143359.json": "2025-08-13",       # 2026 拓疆者网申截止
    "didi-57038.json": None,                       # 社招常年，无截止
    "didi-ibg-intern.json": "2026-12-30",
    "didi-marketplace-intern.json": "2026-12-30",
    "jd-campus.json": "2026-08-31",               # 京东 2026 校招批次
    "meituan-positions.json": None,                # 社招为主，少量实习无截止
}

# 按 job_type 推算兜底截止（页面级未指定时）
TYPE_DEADLINES = {
    "校招": "2026-08-31",   # 校招批次秋招通常 8 月底截止
    "实习": "2026-12-31",   # 实习常态
    "社招": None,           # 常年
}

for fname, page_dl in PAGE_DEADLINES.items():
    p = COLLECTS / fname
    if not p.exists():
        continue
    data = json.loads(p.read_text(encoding="utf-8"))
    data["recruit_deadline"] = page_dl

    for j in data.get("jobs", []):
        # 优先级：单岗位 > 页面级 > 按 job_type 推算
        per_job = j.get("recruit_deadline")
        if per_job:
            continue
        if page_dl:
            j["recruit_deadline"] = page_dl
        else:
            jt = j.get("job_type", "")
            j["recruit_deadline"] = TYPE_DEADLINES.get(jt)

    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    dl = page_dl or "（按 job_type 推算）"
    print(f"  ✓ {fname}  page_dl={dl}  jobs={len(data.get('jobs', []))}")

print("\n✅ 所有 collect JSON 已回填 recruit_deadline")