"""FastAPI 入口：CareerSafari 岗位信息导航。

接口一览：
  GET    /                     前端页面
  GET    /healthz              健康检查（容器编排用）
  POST   /parse                接 URL 或 raw text → 抓 → 抽 → 落库   [需管理令牌 + 限流]
  GET    /jobs                 列出岗位（status/company/category/title + 分页） [限流]
  GET    /jobs/{id}            单条详情                              [限流]
  POST   /jobs/{id}/calibrate  标记校准状态                           [需管理令牌]
  DELETE /jobs/{id}            删除单条                               [需管理令牌]
  GET    /stats                统计
  GET    /docs                 OpenAPI 文档
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .extractor import extract_jobs
from .fetcher import fetch_url
from .security import (
    configured_admin_token,
    rate_limit_parse,
    rate_limit_read,
    require_admin,
    safe_href,
)
from .storage import Storage

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("app")

app = FastAPI(
    title="CareerSafari",
    version="0.2.0",
    description="求职信息导航 + 结构化岗位知识库。写操作需 X-Admin-Token。",
)

storage = Storage()

_ROOT = Path(__file__).resolve().parent.parent
_STATIC_DIR = _ROOT / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ---------- 模型 ----------


class ParseRequest(BaseModel):
    url: Optional[str] = Field(default=None, description="推文 URL；与 text 二选一")
    text: Optional[str] = Field(default=None, description="推文正文（手动粘贴）")
    company_hint: Optional[str] = Field(default=None, description="可选，公司名提示")
    source_tier: Optional[str] = Field(default="P1", description="来源等级 P1-P4")


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


class JobListResponse(BaseModel):
    jobs: list[dict]
    total: int
    limit: int
    offset: int


# ---------- 工具 ----------


def _public_job(job: dict) -> dict:
    """出站前收敛 source_url，避免脏协议流到前端。

    历史数据里可能早就存了 `javascript:` 之类的值，前端虽然也会校验，
    但服务端不该把不安全的值发出去。
    """
    job["source_url"] = safe_href(job.get("source_url"))
    return job


# ---------- 页面与健康检查 ----------


@app.get("/", include_in_schema=False)
def index_page():
    idx = _STATIC_DIR / "index.html"
    if idx.exists():
        return FileResponse(str(idx))
    raise HTTPException(status_code=500, detail="前端文件缺失")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.get("/disclaimer", include_in_schema=False)
def disclaimer_page():
    page = _STATIC_DIR / "disclaimer.html"
    if page.exists():
        return FileResponse(str(page))
    raise HTTPException(status_code=500, detail="免责声明页缺失")


@app.get("/robots.txt", include_in_schema=False)
def robots_txt(request: Request):
    base = str(request.base_url).rstrip("/")
    body = "\n".join([
        "# CareerSafari 求职信息导航（技术演示站）",
        "# 允许收录公开页面；禁止抓取接口与文档。",
        "User-agent: *",
        "Allow: /$",
        "Allow: /disclaimer",
        "Disallow: /parse",
        "Disallow: /stats",
        "Disallow: /jobs",
        "Disallow: /docs",
        "Disallow: /openapi.json",
        "",
        f"Sitemap: {base}/sitemap.xml",
        "",
    ])
    return PlainTextResponse(body, media_type="text/plain; charset=utf-8")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml(request: Request):
    """站点地图。

    说明：当前前端是单页应用，岗位详情没有独立 URL，所以这里只能列出静态页。
    要做岗位页 SEO，需要先支持服务端渲染或 /jobs/{id} 的独立可读页面。
    """
    base = str(request.base_url).rstrip("/")
    pages = [("/", "1.0", "daily"), ("/disclaimer", "0.5", "monthly")]
    items = "\n".join(
        f"  <url>\n"
        f"    <loc>{base}{path}</loc>\n"
        f"    <changefreq>{freq}</changefreq>\n"
        f"    <priority>{priority}</priority>\n"
        f"  </url>"
        for path, priority, freq in pages
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{items}\n"
        "</urlset>\n"
    )
    return Response(content=xml, media_type="application/xml")


# ---------- 接口 ----------


@app.post(
    "/parse",
    response_model=ParseResponse,
    dependencies=[Depends(rate_limit_parse), Depends(require_admin)],
)
def parse(req: ParseRequest):
    if not req.url and not req.text:
        raise HTTPException(status_code=400, detail="url 与 text 必须至少给一个")

    if req.text and not req.url:
        text = req.text
        source_url = None
    else:
        text = fetch_url(req.url or "")
        if not text:
            raise HTTPException(
                status_code=502,
                detail=(
                    "抓取失败：可能是反爬拦截、正文过短，或目标地址被安全策略拒绝。"
                    "建议改用 text 字段直接粘贴正文。"
                ),
            )
        source_url = req.url

    provider = os.environ.get("LLM_PROVIDER", "mock")
    jobs = extract_jobs(text, hint_company=req.company_hint)
    if not jobs:
        return ParseResponse(
            inserted_ids=[], job_count=0, source_url=source_url,
            text_length=len(text), provider=provider,
        )

    ids = storage.insert_jobs(
        jobs,
        source_url=source_url,
        source_tier=req.source_tier,
        raw_text=text[:5000],
    )
    logger.info("parse: provider=%s, jobs=%d, ids=%s", provider, len(ids), ids)
    return ParseResponse(
        inserted_ids=ids, job_count=len(ids), source_url=source_url,
        text_length=len(text), provider=provider,
    )


@app.get("/jobs", response_model=JobListResponse, dependencies=[Depends(rate_limit_read)])
def list_jobs(
    status: Optional[str] = None,
    company: Optional[str] = None,
    category: Optional[str] = None,
    job_type: Optional[str] = None,
    title: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    jobs, total = storage.list_jobs_paged(
        status=status, company=company, category=category,
        job_type=job_type, title=title, limit=limit, offset=offset,
    )
    return JobListResponse(
        jobs=[_public_job(j) for j in jobs], total=total, limit=limit, offset=offset
    )


@app.get("/jobs/{job_id}", dependencies=[Depends(rate_limit_read)])
def get_job(job_id: int):
    job = storage.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return _public_job(job)


@app.post("/jobs/{job_id}/calibrate", dependencies=[Depends(require_admin)])
def calibrate(job_id: int, req: CalibrateRequest):
    try:
        job = storage.calibrate(
            job_id, status=req.status, confidence=req.confidence, notes=req.notes
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return _public_job(job)


@app.delete("/jobs/{job_id}", dependencies=[Depends(require_admin)])
def delete_job(job_id: int):
    """删除单条岗位记录（需管理令牌）。"""
    deleted = storage.delete_job(job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="job not found")
    logger.info("deleted job id=%s", job_id)
    return {"deleted": job_id}


@app.get("/stats", dependencies=[Depends(rate_limit_read)])
def stats():
    payload = storage.stats()
    # 透出前端需要的运行时状态：
    # - llm_provider：顶部徽章显示
    # - write_enabled：服务端是否配置了 ADMIN_TOKEN，前端据此提示
    payload["llm_provider"] = os.environ.get("LLM_PROVIDER", "mock")
    payload["write_enabled"] = configured_admin_token() is not None
    return payload
