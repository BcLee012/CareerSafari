"""FastAPI 入口：W1 原型对外接口。

POST /parse            接 URL 或 raw text → 抓 → 抽 → 落库（pending）
GET  /jobs             列出岗位（可按 status/company/category 筛选）
GET  /jobs/{id}        单条详情
POST /jobs/{id}/calibrate  标记校准状态 + 置信度 + 备注
GET  /stats            统计：总数 / 各状态分布
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .extractor import extract_jobs
from .fetcher import fetch_url
from .storage import Storage

# 加载 .env（如有）
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("app")

app = FastAPI(title="CareerSafari W1 Prototype", version="0.1.0")
storage = Storage()

# 挂载前端静态文件（/ 与 /static 均可用）
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index_page():
    from fastapi.responses import FileResponse
    idx = _STATIC_DIR / "index.html"
    if idx.exists():
        return FileResponse(str(idx))
    return index()


# ---------- 模型 ----------


class ParseRequest(BaseModel):
    url: Optional[str] = Field(default=None, description="公众号推文 URL；与 text 二选一")
    text: Optional[str] = Field(default=None, description="推文正文（手动粘贴）")
    company_hint: Optional[str] = Field(default=None, description="可选，公司名提示")
    source_tier: Optional[str] = Field(default="P1", description="数据来源等级：P1-P4")


class ParseResponse(BaseModel):
    inserted_ids: list[int]
    job_count: int
    source_url: Optional[str]
    text_length: int
    provider: str


class CalibrateRequest(BaseModel):
    status: str = Field(default="calibrated", description="pending / calibrated / archived")
    confidence: Optional[float] = None
    notes: Optional[str] = None


# ---------- 路由 ----------


@app.get("/")
def index():
    return {
        "name": "CareerSafari W1 Prototype",
        "endpoints": [
            "POST /parse",
            "GET /jobs",
            "GET /jobs/{id}",
            "POST /jobs/{id}/calibrate",
            "GET /stats",
            "GET /docs",
        ],
        "llm_provider": os.environ.get("LLM_PROVIDER", "mock"),
    }


@app.post("/parse", response_model=ParseResponse)
def parse(req: ParseRequest):
    if not req.url and not req.text:
        raise HTTPException(status_code=400, detail="url 与 text 必须至少给一个")

    if req.url:
        text = fetch_url(req.url)
        if not text:
            raise HTTPException(
                status_code=502,
                detail=(
                    "抓取失败。公众号反爬较强，建议改用 text 字段直接粘贴正文。"
                    "示例：{\"text\": \"...正文...\", \"company_hint\": \"字节跳动\"}"
                ),
            )
        source_url = req.url
    else:
        text = req.text or ""
        source_url = None

    provider = os.environ.get("LLM_PROVIDER", "mock")
    jobs = extract_jobs(text, hint_company=req.company_hint)
    if not jobs:
        return ParseResponse(
            inserted_ids=[],
            job_count=0,
            source_url=source_url,
            text_length=len(text),
            provider=provider,
        )

    ids = storage.insert_jobs(
        jobs,
        source_url=source_url,
        source_tier=req.source_tier,
        raw_text=text[:5000],  # 截断，避免 SQLite 文本过大
    )
    logger.info("parse: provider=%s, jobs=%d, ids=%s", provider, len(ids), ids)
    return ParseResponse(
        inserted_ids=ids,
        job_count=len(ids),
        source_url=source_url,
        text_length=len(text),
        provider=provider,
    )


@app.get("/jobs")
def list_jobs(
    status: Optional[str] = None,
    company: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 50,
):
    return {"jobs": storage.list_jobs(status=status, company=company, category=category, limit=limit)}


@app.get("/jobs/{job_id}")
def get_job(job_id: int):
    job = storage.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.post("/jobs/{job_id}/calibrate")
def calibrate(job_id: int, req: CalibrateRequest):
    job = storage.calibrate(job_id, status=req.status, confidence=req.confidence, notes=req.notes)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.get("/stats")
def stats():
    return storage.stats()


@app.delete("/jobs/{job_id}")
def delete_job(job_id: int):
    """删除单条岗位记录（开发期用，公开版需做权限校验）。"""
    with storage._conn() as conn:  # noqa: SLF001
        cur = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="job not found")
    return {"deleted": job_id}