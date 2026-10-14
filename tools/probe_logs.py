"""精确判定 /api/v1/logs/* 是否对 API Key 开放（只读，不改任何配置）。

用法:
    python tools/probe_logs.py <base_url> <api_key>
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request


def call(base: str, path: str, key: str, style: str) -> tuple[int, str]:
    """GET。

    Args:
        base: 基地址。
        path: 路径。
        key: API Key。
        style: bearer / xapikey / none。

    Returns:
        (状态码, 响应体)。
    """
    headers = {"User-Agent": "dsh-diag"}
    if style == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    elif style == "xapikey":
        headers["X-API-Key"] = key
    req = urllib.request.Request(base.rstrip("/") + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read(3000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(3000).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def main() -> int:
    """入口。

    Returns:
        退出码。
    """
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    base, key = sys.argv[1], sys.argv[2]
    for path in ("/api/v1/logs/history", "/api/v1/logs/live"):
        for style in ("bearer", "xapikey", "none"):
            code, body = call(base, path, key, style)
            flat = body.replace("\n", " ")[:130]
            print(f"  {code:>3}  {style:<8} {path:<24} {flat}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
