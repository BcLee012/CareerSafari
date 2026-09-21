"""安全模块：接口鉴权 / 限流 / URL 安全校验（SSRF 防护）。

设计取舍（有意为之，别随手改回去）：
- 写操作（/parse、/calibrate、DELETE）一律要求 `X-Admin-Token`。
- `ADMIN_TOKEN` **未配置时写操作直接关闭（fail closed）**，而不是放行。
  原型阶段"为了本地方便而默认放行"的写法，一旦部署到公网就是删库后门。
- 限流是**进程内**滑动窗口：单进程部署有效，多 worker 下是「每 worker 一份配额」，
  要精确限流需换成 Redis，本项目规模暂不需要。
- SSRF 防护同时校验协议、主机名、DNS 解析出的**全部** IP，并逐跳校验重定向
  （否则 302 到 169.254.169.254 就能绕过首跳检查）。
"""
from __future__ import annotations

import ipaddress
import logging
import os
import secrets
import socket
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Optional
from urllib.parse import urlparse

from fastapi import Header, HTTPException, Request

logger = logging.getLogger(__name__)

# ---------------- 1. 管理令牌鉴权 ----------------

ADMIN_TOKEN_ENV = "ADMIN_TOKEN"


def configured_admin_token() -> Optional[str]:
    """读取服务端配置的管理令牌；未配置返回 None。"""
    token = (os.environ.get(ADMIN_TOKEN_ENV) or "").strip()
    return token or None


def require_admin(x_admin_token: Optional[str] = Header(default=None)) -> None:
    """写操作的 FastAPI 依赖：校验 `X-Admin-Token` 请求头。"""
    expected = configured_admin_token()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=(
                "写操作未启用：服务端未配置 ADMIN_TOKEN。"
                "本地开发请在 .env 中设置后重启；这是防止公网部署时写接口裸奔的开关。"
            ),
        )
    # compare_digest 防止时序侧信道
    if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=401, detail="无效或缺失的管理令牌。")


# ---------------- 2. 限流 ----------------


class SlidingWindowLimiter:
    """进程内滑动窗口限流器。"""

    def __init__(self, max_events: int, window_seconds: float, *, max_keys: int = 10_000):
        self.max_events = max_events
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._hits: dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> None:
        """记一次访问；超限则抛 429。"""
        now = time.monotonic()
        with self._lock:
            # 防止被大量伪造 IP 撑爆内存
            if len(self._hits) > self.max_keys:
                self._hits.clear()
            queue = self._hits[key]
            while queue and now - queue[0] > self.window_seconds:
                queue.popleft()
            if len(queue) >= self.max_events:
                retry_after = self.window_seconds - (now - queue[0])
                raise HTTPException(
                    status_code=429,
                    detail=f"请求过于频繁，请 {max(1, int(retry_after))} 秒后重试。",
                    headers={"Retry-After": str(max(1, int(retry_after)))},
                )
            queue.append(now)


# 抓取接口最耗资源（要发外部请求），配额收紧
parse_limiter = SlidingWindowLimiter(max_events=10, window_seconds=60)
# 普通读接口给宽一些
read_limiter = SlidingWindowLimiter(max_events=120, window_seconds=60)


def client_ip(request: Request) -> str:
    """取客户端 IP。反代场景优先信任 X-Forwarded-For 的第一跳。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit_parse(request: Request) -> None:
    parse_limiter.hit(f"parse:{client_ip(request)}")


def rate_limit_read(request: Request) -> None:
    read_limiter.hit(f"read:{client_ip(request)}")


# ---------------- 3. URL 安全校验（SSRF 防护） ----------------

ALLOWED_SCHEMES = {"http", "https"}

# 逃生开关：某些开发环境（如带透明代理的沙箱）会把所有域名解析到保留网段
# （实测解析到 198.18.0.x，RFC 2544 测试段），导致公网校验全数误杀。
# 设为 1 可跳过「公网地址」这一项校验 —— **仅供本地开发，公网部署严禁开启**。
ALLOW_NON_PUBLIC_ENV = "FETCH_ALLOW_NON_PUBLIC"

_warned_non_public = False


def _allow_non_public() -> bool:
    value = (os.environ.get(ALLOW_NON_PUBLIC_ENV) or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _check_addr(addr: ipaddress.IPv4Address | ipaddress.IPv6Address, raw: str) -> None:
    """IPv4-mapped IPv6 要先拆开，否则 ::ffff:127.0.0.1 能绕过判断。"""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if not addr.is_global:
        raise HTTPException(
            status_code=400,
            detail=f"目标地址指向内网或保留网段（{raw}），已拒绝抓取。",
        )


def _assert_public_host(host: str) -> None:
    """校验主机名安全。

    注意：**字面量 IP 永远严格校验，不受逃生开关影响**。
    因为直接填 IP 是最典型的 SSRF 手法，而开关存在的唯一理由是
    「沙箱/透明代理把域名解析到了保留网段」—— 只跟域名解析有关。
    """
    global _warned_non_public

    # 1) 字面量 IP：无 DNS 参与，一律严格
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        _check_addr(literal, host)
        return

    # 2) 域名：开发环境可跳过解析校验
    if _allow_non_public():
        if not _warned_non_public:
            logger.warning(
                "%s=1：已跳过域名的公网地址校验（字面量 IP 仍严格拦截）。"
                "仅限本地开发，公网部署务必删除该变量。",
                ALLOW_NON_PUBLIC_ENV,
            )
            _warned_non_public = True
        return

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise HTTPException(status_code=400, detail=f"域名无法解析：{host}") from exc

    if not infos:
        raise HTTPException(status_code=400, detail=f"域名无法解析：{host}")

    for info in infos:
        raw_ip = info[4][0]
        try:
            addr = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        _check_addr(addr, raw_ip)


def assert_safe_url(url: str) -> str:
    """校验待抓取 URL：协议白名单 + 主机公网可达。不通过直接抛 HTTPException。"""
    if not url or not url.strip():
        raise HTTPException(status_code=400, detail="链接为空。")

    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise HTTPException(
            status_code=400,
            detail=f"仅支持 http/https 链接，收到：{parsed.scheme or '(空)'}",
        )
    host = parsed.hostname
    if not host:
        raise HTTPException(status_code=400, detail="链接缺少主机名。")

    _assert_public_host(host)
    return url.strip()


def safe_href(url: Optional[str]) -> Optional[str]:
    """把 URL 收敛成可安全放进 href 的形式；不安全则返回 None。

    前端也会做一次同样校验（双保险）——历史数据里可能已经存了脏 URL。
    """
    if not url:
        return None
    candidate = url.strip()
    if urlparse(candidate).scheme.lower() not in ALLOWED_SCHEMES:
        return None
    return candidate
