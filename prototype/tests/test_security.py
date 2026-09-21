"""安全与工程加固的回归测试。

覆盖本次修复的问题：
  1. 写接口鉴权（fail closed）
  2. SSRF 防护（私网字面量 IP / 伪协议 / IPv4 映射绕过）
  3. 接口限流
  4. 分页与去重幂等
  5. 截止日期按东八区判定

运行方式（无需 pytest，也可被 pytest 直接收集）：
    python tests/test_security.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在导入 app 之前设好环境，因为 Storage 在导入期就实例化了
TMP_DB = Path(tempfile.gettempdir()) / "careersafari_sec_test.db"
for suffix in ("", "-wal", "-shm"):
    candidate = Path(str(TMP_DB) + suffix)
    if candidate.exists():
        candidate.unlink()
os.environ["CAREERSAFARI_DB"] = str(TMP_DB)
os.environ["ADMIN_TOKEN"] = "test-token-123"
os.environ.pop("FETCH_ALLOW_NON_PUBLIC", None)

from fastapi.testclient import TestClient  # noqa: E402

from app.extractor import JobDraft  # noqa: E402
from app.main import app  # noqa: E402
from app.security import assert_safe_url, parse_limiter, safe_href  # noqa: E402
from app.storage import CN_TZ, Storage, _derive_recruit_status  # noqa: E402

client = TestClient(app)
TOKEN = {"X-Admin-Token": "test-token-123"}

_results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    _results.append((name, bool(condition), detail))


def expect_blocked(url: str) -> bool:
    try:
        assert_safe_url(url)
        return False
    except Exception:
        return True


# ---------------- 1. SSRF 防护 ----------------


def test_ssrf_blocks_dangerous_targets() -> None:
    for url in [
        "http://127.0.0.1:8765/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/x",
        "http://192.168.1.1/",
        "http://[::ffff:127.0.0.1]/x",
        "http://[::1]/x",
    ]:
        check(f"SSRF 拦截 {url[:38]}", expect_blocked(url))


def test_ssrf_blocks_bad_schemes() -> None:
    for url in ["file:///etc/passwd", "javascript:alert(1)", "ftp://x/y", "gopher://x"]:
        check(f"协议拦截 {url[:30]}", expect_blocked(url))


def test_ssrf_allows_public_literal_ip() -> None:
    # 用字面量公网 IP，绕开 DNS（沙箱里 DNS 被劫持到保留段）
    ok = True
    try:
        assert_safe_url("http://93.184.216.34/jobs")
    except Exception as exc:  # noqa: BLE001
        ok = False
        check("放行公网字面量 IP", False, str(exc))
    if ok:
        check("放行公网字面量 IP", True)


def test_safe_href() -> None:
    check("safe_href 保留 https", safe_href("https://x.com/a") == "https://x.com/a")
    check("safe_href 拒绝 javascript:", safe_href("javascript:alert(1)") is None)
    check("safe_href 拒绝 data:", safe_href("data:text/html,x") is None)
    check("safe_href 处理空值", safe_href(None) is None and safe_href("") is None)


# ---------------- 2. 接口鉴权 ----------------


def test_write_endpoints_require_token() -> None:
    check("DELETE 无令牌 → 401", client.delete("/jobs/1").status_code == 401)
    check(
        "DELETE 错误令牌 → 401",
        client.delete("/jobs/1", headers={"X-Admin-Token": "wrong"}).status_code == 401,
    )
    check(
        "calibrate 无令牌 → 401",
        client.post("/jobs/1/calibrate", json={"status": "calibrated"}).status_code == 401,
    )
    check("parse 无令牌 → 401", client.post("/parse", json={"text": "x"}).status_code == 401)


def test_read_endpoints_stay_open() -> None:
    check("GET /jobs 免鉴权", client.get("/jobs").status_code == 200)
    check("GET /stats 免鉴权", client.get("/stats").status_code == 200)
    check("GET /healthz 免鉴权", client.get("/healthz").status_code == 200)
    check("GET /robots.txt 免鉴权", client.get("/robots.txt").status_code == 200)


def test_write_endpoints_reject_when_token_unset() -> None:
    """fail closed：服务端没配 ADMIN_TOKEN 时必须拒绝，而不是放行。"""
    import app.security as sec

    saved = os.environ.pop("ADMIN_TOKEN", None)
    sec_module_token = os.environ.get("ADMIN_TOKEN")
    try:
        resp = client.delete("/jobs/1", headers=TOKEN)
        check("未配 ADMIN_TOKEN 时写操作 → 503（fail closed）", resp.status_code == 503, resp.text[:80])
    finally:
        if saved is not None:
            os.environ["ADMIN_TOKEN"] = saved
        _ = sec_module_token


# ---------------- 3. 限流 ----------------


def test_parse_rate_limited() -> None:
    parse_limiter._hits.clear()
    codes = []
    for _ in range(12):
        codes.append(
            client.post("/parse", json={"text": "招聘 测试"}, headers=TOKEN).status_code
        )
    check("前 10 次 /parse 未被限流", all(c in (200, 502) for c in codes[:10]), str(codes))
    check("第 11 次起触发 429", 429 in codes[10:], str(codes))
    parse_limiter._hits.clear()


# ---------------- 4. 存储层 ----------------


def _draft(title: str, company: str = "测试公司", location: str = "北京") -> JobDraft:
    return JobDraft(company=company, title=title, location=location, category="AI")


def test_storage_dedup_is_idempotent() -> None:
    storage = Storage()
    first = storage.insert_jobs([_draft("去重岗 A"), _draft("去重岗 B")])
    second = storage.insert_jobs([_draft("去重岗 A"), _draft("去重岗 B")])
    check("首次插入返回 id", len(first) == 2, str(first))
    check("重复插入被跳过（幂等）", len(second) == 0, str(second))

    third = storage.insert_jobs([_draft("去重岗 A"), _draft("去重岗 C")])
    check("部分新增只返回新增 id", len(third) == 1, str(third))


def test_storage_pagination() -> None:
    storage = Storage()
    storage.insert_jobs([_draft(f"分页岗 {i}") for i in range(7)])

    page1, total1 = storage.list_jobs_paged(title="分页岗", limit=3, offset=0)
    page2, total2 = storage.list_jobs_paged(title="分页岗", limit=3, offset=3)
    check("分页 total 一致", total1 == total2 == 7, f"{total1}/{total2}")
    check("首页返回 3 条", len(page1) == 3, str(len(page1)))
    check("第二页返回 3 条", len(page2) == 3, str(len(page2)))
    check(
        "两页无重叠",
        {j["id"] for j in page1}.isdisjoint({j["id"] for j in page2}),
    )


def test_recruit_status_timezone() -> None:
    today_cn = datetime.now(CN_TZ).date()
    check("无截止日 → unknown", _derive_recruit_status(None) == "unknown")
    check("非法日期 → unknown", _derive_recruit_status("不是日期") == "unknown")
    check(
        "30 天后 → open",
        _derive_recruit_status((today_cn + timedelta(days=30)).isoformat()) == "open",
    )
    check(
        "7 天后 → closing_soon",
        _derive_recruit_status((today_cn + timedelta(days=7)).isoformat()) == "closing_soon",
    )
    check(
        "东八区今天 → closing_soon（不是 expired）",
        _derive_recruit_status(today_cn.isoformat()) == "closing_soon",
    )
    check(
        "昨天 → expired",
        _derive_recruit_status((today_cn - timedelta(days=1)).isoformat()) == "expired",
    )


def test_api_rejects_oversized_limit() -> None:
    resp = client.get("/jobs?limit=99999")
    check("limit 被收敛到 200", resp.status_code == 200 and resp.json()["limit"] == 200)
    resp = client.get("/jobs?offset=-5")
    check("负 offset 被收敛到 0", resp.status_code == 200 and resp.json()["offset"] == 0)


# ---------------- 运行 ----------------


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(f"{fn.__name__} 执行异常", False, f"{type(exc).__name__}: {exc}")

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = [r for r in _results if not r[1]]

    print(f"\n{'=' * 62}")
    print(f"共 {len(_results)} 项断言，通过 {passed}，失败 {len(failed)}")
    print("=" * 62)
    if failed:
        for name, _, detail in failed:
            print(f"  ✗ {name}" + (f"  [{detail}]" if detail else ""))
        return 1
    print("  ✓ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
