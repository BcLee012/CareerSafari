"""抓取模块：从 URL 拿到 HTML、从 HTML 抽取正文。

安全要点（改之前先读）：
  - URL 必须过 `security.assert_safe_url`（协议白名单 + 公网地址校验）。
  - **重定向必须逐跳校验**：若只校验首跳，`302 → http://169.254.169.254/`
    就能拿到云主机元数据。所以这里关掉 requests 的自动跳转，手动一跳跃一验证。
  - 响应体有大小上限，避免超大页面把内存吃满。

公众号文章有反爬，且真实采集链路是「智能体 WebFetch + JSON 落盘」，
本模块主要服务于本地/自建部署时的 URL 入口。
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urljoin

import requests
import trafilatura
from bs4 import BeautifulSoup

from .security import assert_safe_url

logger = logging.getLogger(__name__)

USER_AGENTS = [
    # 模拟移动端微信内置浏览器
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.45(0x18002d39) NetType/WIFI Language/zh_CN",
    # PC Chrome
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36",
]

REDIRECT_CODES = {301, 302, 303, 307, 308}

MAX_REDIRECTS = 4
MAX_BYTES = 3 * 1024 * 1024  # 单页最多读 3MB


def fetch_url(url: str, timeout: float = 12.0) -> Optional[str]:
    """抓取 URL 主正文。校验失败或抓取失败均返回 None。"""
    try:
        current = assert_safe_url(url)
    except Exception as exc:  # noqa: BLE001 —— 含 HTTPException，统一按"不可抓取"处理
        logger.warning("URL 校验未通过，拒绝抓取：%s（%s）", url, exc)
        return None

    for _hop in range(MAX_REDIRECTS + 1):
        resp = _get_once(current, timeout)
        if resp is None:
            return None

        if resp.status_code in REDIRECT_CODES:
            location = resp.headers.get("location")
            resp.close()
            if not location:
                logger.warning("重定向缺少 Location 头：%s", current)
                return None
            next_url = urljoin(current, location)
            try:
                current = assert_safe_url(next_url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("重定向目标未通过校验，拒绝跟随：%s（%s）", next_url, exc)
                return None
            continue

        if resp.status_code >= 400:
            logger.warning("抓取返回 %s：%s", resp.status_code, current)
            resp.close()
            return None

        html = _read_limited(resp)
        resp.close()
        if not html:
            return None
        text = clean_html(html)
        if text and len(text) > 200:
            return text
        logger.warning("正文过短或抽取失败：%s", current)
        return None

    logger.warning("重定向次数超过 %d，放弃：%s", MAX_REDIRECTS, url)
    return None


def _get_once(url: str, timeout: float) -> Optional[requests.Response]:
    """用多个 UA 试一次请求（不自动跟随重定向）。全失败返回 None。"""
    last_err: Optional[Exception] = None
    for ua in USER_AGENTS:
        try:
            resp = requests.get(
                url,
                headers={
                    "User-Agent": ua,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )
            return resp
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            logger.warning("fetch ua=%s failed: %s", ua[:40], exc)
    logger.error("全部 UA 均失败：%s（%s）", url, last_err)
    return None


def _read_limited(resp: requests.Response, max_bytes: int = MAX_BYTES) -> Optional[str]:
    """按上限读取响应体并解码。"""
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                logger.warning("响应体超过 %d 字节，已截断", max_bytes)
                break
            chunks.append(chunk)
    except Exception as exc:  # noqa: BLE001
        logger.error("读取响应体失败：%s", exc)
        return None

    raw = b"".join(chunks)
    if not raw:
        return None
    encoding = resp.encoding or "utf-8"
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def clean_html(html: str) -> Optional[str]:
    """HTML → 正文文本。优先 trafilatura，失败回退 BeautifulSoup。"""
    if not html:
        return None
    try:
        extracted = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
            with_metadata=False,
            output_format="txt",  # trafilatura v2：是 txt 不是 text
        )
        if extracted and len(extracted.strip()) > 100:
            return _normalize_whitespace(extracted)
    except Exception as exc:  # noqa: BLE001
        logger.warning("trafilatura failed: %s", exc)

    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "iframe"]):
            tag.decompose()
        return _normalize_whitespace(soup.get_text(separator="\n"))
    except Exception as exc:  # noqa: BLE001
        logger.error("clean_html failed: %s", exc)
        return None


def _normalize_whitespace(text: str) -> str:
    """合并多余空白、删除空行。"""
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)
