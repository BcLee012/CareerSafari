"""抓取模块：从 URL 拿到 HTML、从 HTML 抽取正文。

公众号文章有反爬，但 W1 原型先走通链路，因此同时支持：
  - fetch_url(url)：抓 HTML → 去噪 → 返回正文文本
  - clean_html(html)：纯 HTML → 文本（供 LLM 处理）

真实公众号链路（mobile 端 / PC 端 / 引用页）大概率会被拦截，
最稳的方式是用户手动复制正文 → 走 raw text 入口。
"""
from __future__ import annotations

import logging
from typing import Optional

import requests
import trafilatura
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENTS = [
    # 模拟移动端微信内置浏览器
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.45(0x18002d39) NetType/WIFI Language/zh_CN",
    # PC Chrome
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36",
]


def fetch_url(url: str, timeout: float = 12.0) -> Optional[str]:
    """抓取 URL 主正文。失败返回 None。"""
    last_err = None
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
                allow_redirects=True,
            )
            resp.raise_for_status()
            text = clean_html(resp.text)
            if text and len(text) > 200:
                return text
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("fetch_url ua=%s failed: %s", ua[:40], e)
            continue
    logger.error("fetch_url all attempts failed: %s", last_err)
    return None


def clean_html(html: str) -> Optional[str]:
    """HTML → 正文文本。优先 trafilatura（公众号专用效果不错），失败回退 BeautifulSoup。"""
    if not html:
        return None
    try:
        extracted = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
            with_metadata=False,
            output_format="txt",
        )
        if extracted and len(extracted.strip()) > 100:
            return _normalize_whitespace(extracted)
    except Exception as e:  # noqa: BLE001
        logger.warning("trafilatura failed: %s", e)

    # 回退：BeautifulSoup 粗暴抽取
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "iframe"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
        return _normalize_whitespace(text)
    except Exception as e:  # noqa: BLE001
        logger.error("clean_html failed: %s", e)
        return None


def _normalize_whitespace(text: str) -> str:
    """合并多余空白、删除首尾空行。"""
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)