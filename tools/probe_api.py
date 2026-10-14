"""按官方文档探测 AstrBot OpenAPI（只读）。

文档: https://docs.astrbot.app/dev/openapi.html
- 端点前缀 /api/v1
- Authorization: Bearer abk_xxx  或  X-API-Key: abk_xxx

用法:
    python tools/probe_api.py <base_url> <api_key>
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def call(base: str, path: str, key: str, style: str = "bearer") -> tuple[int, str]:
    """发一个 GET 请求。

    Args:
        base: 基地址。
        path: 路径。
        key: API Key。
        style: bearer 或 xapikey。

    Returns:
        (状态码, 响应体)。
    """
    headers = {"User-Agent": "dsh-diag"}
    if style == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    else:
        headers["X-API-Key"] = key
    req = urllib.request.Request(base.rstrip("/") + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
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
    print(f"base={base}  key_len={len(key)}  prefix={key[:4]}\n")

    print("=== Bearer style, /api/v1 ===")
    for path in (
        "/api/v1/plugins",
        "/api/v1/bots",
        "/api/v1/providers",
        "/api/v1/personas",
        "/api/v1/configs",
        "/api/v1/openapi.json",
        "/api/logs/history",
    ):
        code, body = call(base, path, key)
        head = body.replace("\n", " ")[:150]
        print(f"  {code:>3}  {path:<28} {head}")

    print("\n=== X-API-Key style ===")
    for path in ("/api/v1/plugins", "/api/v1/configs"):
        code, body = call(base, path, key, style="xapikey")
        print(f"  {code:>3}  {path:<28} {body.replace(chr(10), ' ')[:110]}")

    # 插件列表里找本插件，并打印其配置 schema 可见的键
    code, body = call(base, "/api/v1/plugins", key)
    if code == 200:
        try:
            data = json.loads(body)
            items = data.get("data") or data.get("plugins") or data
            if isinstance(items, dict):
                items = items.get("plugins", items)
            names = []
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict):
                        names.append(it.get("name") or it.get("id") or it.get("plugin_id"))
            print(f"\nplugins visible ({len(names)}): {names}")
        except Exception as exc:  # noqa: BLE001
            print(f"\nplugin list parse failed: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
