"""存储模块：SQLite 持久化岗位档案。

对应 PRD §F1 的字段集。表结构刻意做得比 §F1 略"扁平"——
招聘源字段经常结构化不一致，先以 JSON 存，待校准时再结构化。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, List, Optional

from .extractor import JobDraft

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "careersafari.db"


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
    page_kind TEXT,                -- 岗位详情页 / 岗位列表页 / 招聘公告页 / 门户首页
    recruit_deadline TEXT,         -- ISO 日期 截止时间（用于判定"已截止/在招/临近"）
    raw_text TEXT,
    extras_json TEXT,              -- 评分依据、可信度权重、采集方式标记等

    status TEXT DEFAULT 'pending', -- pending / calibrated / archived
    confidence REAL DEFAULT 0.5,   -- 抽取置信度（mock 时默认 0.3，待接入 LLM 后调整）
    created_at TEXT NOT NULL,
    calibrated_at TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_category ON jobs(category);
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
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # 兼容旧库：增量加列
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
            if "extras_json" not in cols:
                conn.execute("ALTER TABLE jobs ADD COLUMN extras_json TEXT")
            if "page_kind" not in cols:
                conn.execute("ALTER TABLE jobs ADD COLUMN page_kind TEXT")
            if "recruit_deadline" not in cols:
                conn.execute("ALTER TABLE jobs ADD COLUMN recruit_deadline TEXT")

    def insert_jobs(
        self,
        jobs: List[JobDraft],
        *,
        source_url: Optional[str] = None,
        source_tier: Optional[str] = None,
        page_kind: Optional[str] = None,
        recruit_deadline: Optional[str] = None,
        raw_text: Optional[str] = None,
    ) -> List[int]:
        ids: List[int] = []
        now = datetime.utcnow().isoformat(timespec="seconds")
        with self._conn() as conn:
            for j in jobs:
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
                        j.company or "",
                        j.title or "（未命名岗位）",
                        j.location,
                        j.category,
                        j.job_type,
                        json.dumps(j.responsibilities, ensure_ascii=False),
                        json.dumps(j.requirements, ensure_ascii=False),
                        json.dumps(j.skills, ensure_ascii=False),
                        j.salary_text,
                        j.experience_text,
                        j.education_text,
                        source_url,
                        source_tier,
                        page_kind,
                        recruit_deadline,
                        raw_text,
                        json.dumps(j.extras or {}, ensure_ascii=False),
                        now,
                    ),
                )
                ids.append(int(cur.lastrowid))
        return ids

    def list_jobs(
        self,
        *,
        status: Optional[str] = None,
        company: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 100,
    ) -> List[dict]:
        sql = "SELECT * FROM jobs WHERE 1=1"
        args: list[Any] = []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if company:
            sql += " AND company LIKE ?"
            args.append(f"%{company}%")
        if category:
            sql += " AND category = ?"
            args.append(category)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_job(self, job_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_dict(r) if r else None

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
        now = datetime.utcnow().isoformat(timespec="seconds")
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, confidence = COALESCE(?, confidence),
                    calibrated_at = ?, notes = COALESCE(?, notes)
                WHERE id = ?
                """,
                (status, confidence, now, notes, job_id),
            )
        return self.get_job(job_id)

    def stats(self) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS n FROM jobs GROUP BY status
                """
            )
            by_status = {r["status"]: r["n"] for r in rows.fetchall()}
            total = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        return {"total": total, "by_status": by_status}


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
    # 反序列化 extras
    if d.get("extras_json"):
        try:
            d["extras"] = json.loads(d["extras_json"])
        except json.JSONDecodeError:
            d["extras"] = {}
    else:
        d["extras"] = {}
    # 派生：在招状态（基于截止日期与今天）
    d["recruit_status"] = _derive_recruit_status(d.get("recruit_deadline"))
    return d


def _derive_recruit_status(deadline: Optional[str]) -> str:
    """根据截止日期判断在招状态：expired / closing_soon / open / unknown"""
    if not deadline:
        return "unknown"
    try:
        dl = datetime.fromisoformat(deadline[:10])
    except (ValueError, TypeError):
        return "unknown"
    today = datetime.utcnow().date()
    days_left = (dl.date() - today).days
    if days_left < 0:
        return "expired"
    if days_left <= 14:
        return "closing_soon"
    return "open"