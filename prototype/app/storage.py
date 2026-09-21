"""存储模块：SQLite 持久化岗位档案。

对应 PRD §F1 的字段集。表结构刻意做得比 §F1 略"扁平"——
招聘源字段经常结构化不一致，先以 JSON 存，待校准时再结构化。

工程要点：
- 开启 **WAL**：读写不互相阻塞，SQLite 在"读多写少"的信息站场景下够用。
- `(company,title,location)` 建 **唯一索引** + 插入时跳过已存在记录，
  让重复导入天然幂等 —— 之前每次重跑都要手敲去重 SQL，就是这么来的。
- 时间统一存 **UTC 带时区**；但"今天是否已过截止日"必须按 **东八区** 算，
  否则 `recruit_status` 会在每天 08:00 前后判错一天。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional

from .extractor import JobDraft

logger = logging.getLogger(__name__)

# 可用环境变量覆盖数据库位置（容器部署挂载卷、测试用临时库都靠它）
DEFAULT_DB_PATH = Path(
    os.environ.get("CAREERSAFARI_DB")
    or (Path(__file__).resolve().parent.parent / "data" / "careersafari.db")
)

# 业务面向中国大陆用户，截止日期按东八区判定
CN_TZ = timezone(timedelta(hours=8))


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,
    category TEXT,
    job_type TEXT,                 -- 实习 / 校招 / 社招
    responsibilities_json TEXT,
    requirements_json TEXT,
    skills_json TEXT,
    salary_text TEXT,
    experience_text TEXT,
    education_text TEXT,

    source_url TEXT,
    source_tier TEXT,              -- P1 企业官方 / P2 招聘平台 / P3 转发聚合 / P4 UGC
    page_kind TEXT,                -- 岗位详情页 / 列表页 / 公告页 / 门户首页
    recruit_deadline TEXT,         -- ISO 日期，用于判定 已截止/临近/在招
    raw_text TEXT,
    extras_json TEXT,

    status TEXT DEFAULT 'pending', -- pending / calibrated / archived
    confidence REAL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    calibrated_at TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_category ON jobs(category);
CREATE INDEX IF NOT EXISTS idx_jobs_job_type ON jobs(job_type);
"""


class Storage:
    """线程安全的 SQLite 包装。"""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
            for col in ("extras_json", "page_kind", "recruit_deadline"):
                if col not in cols:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT")

            # 唯一索引：从根上杜绝重复岗位。
            # 老库若已存在重复数据，建索引会失败 —— 此时降级为告警，不阻断启动。
            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_jobs_identity "
                    "ON jobs(company, title, location)"
                )
            except sqlite3.IntegrityError:
                logger.warning(
                    "存在重复岗位，唯一索引未建立。请先清理重复："
                    "DELETE FROM jobs WHERE id NOT IN "
                    "(SELECT MIN(id) FROM jobs GROUP BY company, title, location);"
                )

    # ---------------- 写入 ----------------

    def insert_jobs(
        self,
        jobs: List[JobDraft],
        *,
        source_url: Optional[str] = None,
        source_tier: Optional[str] = None,
        page_kind: Optional[str] = None,
        recruit_deadline: Optional[str] = None,
        raw_text: Optional[str] = None,
        skip_existing: bool = True,
    ) -> List[int]:
        """批量插入。

        `skip_existing=True`（默认）时，(company,title,location) 已存在的记录会被跳过，
        因此重复导入是幂等的 —— 只返回真正新增的 id。
        若需要覆盖旧值，请先删除再插入（load_collects.py --replace 就是这么做的）。
        """
        ids: List[int] = []
        skipped = 0
        now = _utc_now_iso()
        with self._conn() as conn:
            for job in jobs:
                company = job.company or ""
                title = job.title or "（未命名岗位）"
                location = job.location or ""

                if skip_existing:
                    exists = conn.execute(
                        "SELECT id FROM jobs WHERE company = ? AND title = ? "
                        "AND IFNULL(location,'') = IFNULL(?,'')",
                        (company, title, location),
                    ).fetchone()
                    if exists:
                        skipped += 1
                        continue

                cur = conn.execute(
                    """
                    INSERT INTO jobs (
                        company, title, location, category, job_type,
                        responsibilities_json, requirements_json, skills_json,
                        salary_text, experience_text, education_text,
                        source_url, source_tier, page_kind, recruit_deadline,
                        raw_text, extras_json,
                        created_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                    """,
                    (
                        company, title, location, job.category, job.job_type,
                        json.dumps(job.responsibilities, ensure_ascii=False),
                        json.dumps(job.requirements, ensure_ascii=False),
                        json.dumps(job.skills, ensure_ascii=False),
                        job.salary_text, job.experience_text, job.education_text,
                        source_url, source_tier, page_kind, recruit_deadline,
                        raw_text,
                        json.dumps(job.extras or {}, ensure_ascii=False),
                        now,
                    ),
                )
                ids.append(int(cur.lastrowid))
        if skipped:
            logger.info("insert_jobs: 跳过 %d 条已存在记录（幂等导入）", skipped)
        return ids

    # ---------------- 查询 ----------------

    def list_jobs(
        self,
        *,
        status: Optional[str] = None,
        company: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 100,
    ) -> List[dict]:
        jobs, _ = self.list_jobs_paged(
            status=status, company=company, category=category, limit=limit, offset=0
        )
        return jobs

    def list_jobs_paged(
        self,
        *,
        status: Optional[str] = None,
        company: Optional[str] = None,
        category: Optional[str] = None,
        job_type: Optional[str] = None,
        title: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[dict], int]:
        """带总数与分页的列表查询。返回 (jobs, total)。"""
        where = " WHERE 1=1"
        args: list[Any] = []
        if status:
            where += " AND status = ?"
            args.append(status)
        if company:
            where += " AND company LIKE ?"
            args.append(f"%{company}%")
        if category:
            where += " AND category = ?"
            args.append(category)
        if job_type:
            where += " AND job_type = ?"
            args.append(job_type)
        if title:
            where += " AND (title LIKE ? OR IFNULL(skills_json,'') LIKE ?)"
            args.extend([f"%{title}%", f"%{title}%"])

        with self._conn() as conn:
            total = conn.execute(f"SELECT COUNT(*) AS n FROM jobs{where}", args).fetchone()["n"]
            rows = conn.execute(
                f"SELECT * FROM jobs{where} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
        return [_row_to_dict(r) for r in rows], int(total)

    def get_job(self, job_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_dict(row) if row else None

    # ---------------- 变更 ----------------

    def calibrate(
        self,
        job_id: int,
        *,
        status: str = "calibrated",
        confidence: Optional[float] = None,
        notes: Optional[str] = None,
    ) -> Optional[dict]:
        if status not in {"pending", "calibrated", "archived"}:
            raise ValueError("invalid status")
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, confidence = COALESCE(?, confidence),
                    calibrated_at = ?, notes = COALESCE(?, notes)
                WHERE id = ?
                """,
                (status, confidence, _utc_now_iso(), notes, job_id),
            )
        return self.get_job(job_id)

    def delete_job(self, job_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            return cur.rowcount > 0

    # ---------------- 统计 ----------------

    def stats(self) -> dict:
        with self._conn() as conn:
            by_status = {
                r["status"]: r["n"]
                for r in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
                ).fetchall()
            }
            by_category = {
                r["category"] or "未分类": r["n"]
                for r in conn.execute(
                    "SELECT category, COUNT(*) AS n FROM jobs GROUP BY category"
                ).fetchall()
            }
            by_job_type = {
                r["job_type"] or "不明确": r["n"]
                for r in conn.execute(
                    "SELECT job_type, COUNT(*) AS n FROM jobs GROUP BY job_type"
                ).fetchall()
            }
            total = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
            companies = conn.execute(
                "SELECT COUNT(DISTINCT company) AS n FROM jobs"
            ).fetchone()["n"]
            deadlines = [
                r["recruit_deadline"]
                for r in conn.execute("SELECT recruit_deadline FROM jobs").fetchall()
            ]

        by_recruit_status: dict[str, int] = {}
        for dl in deadlines:
            key = _derive_recruit_status(dl)
            by_recruit_status[key] = by_recruit_status.get(key, 0) + 1

        return {
            "total": total,
            "companies": int(companies),
            "by_status": by_status,
            "by_category": by_category,
            "by_job_type": by_job_type,
            "by_recruit_status": by_recruit_status,
            "today_cn": _today_cn().isoformat(),
        }


def _utc_now_iso() -> str:
    """带时区的 UTC 时间戳（替代已废弃的 datetime.utcnow）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _today_cn() -> date:
    return datetime.now(CN_TZ).date()


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("responsibilities_json", "requirements_json", "skills_json"):
        if d.get(key):
            try:
                d[key[:-5]] = json.loads(d[key])
            except json.JSONDecodeError:
                d[key[:-5]] = []
        else:
            d[key[:-5]] = []
    if d.get("extras_json"):
        try:
            d["extras"] = json.loads(d["extras_json"])
        except json.JSONDecodeError:
            d["extras"] = {}
    else:
        d["extras"] = {}
    d["recruit_status"] = _derive_recruit_status(d.get("recruit_deadline"))
    return d


def _derive_recruit_status(deadline: Optional[str]) -> str:
    """按东八区日期判断在招状态：expired / closing_soon / open / unknown。"""
    if not deadline:
        return "unknown"
    try:
        dl = datetime.fromisoformat(str(deadline)[:10]).date()
    except (ValueError, TypeError):
        return "unknown"
    days_left = (dl - _today_cn()).days
    if days_left < 0:
        return "expired"
    if days_left <= 14:
        return "closing_soon"
    return "open"
