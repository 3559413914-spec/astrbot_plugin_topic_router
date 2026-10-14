"""列出 AstrBot OpenAPI 里与重启/插件开关/chat 相关的端点及所需 scope（只读）。

用法:
    python tools/list_endpoints.py <base_url> <api_key>
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def call(base: str, path: str, key: str) -> tuple[int, str]:
    """GET。

    Args:
        base: 基地址。
        path: 路径。
        key: API Key。

    Returns:
        (状态码, 响应体)。
    """
    req = urllib.request.Request(
        base.rstrip("/") + path,
        headers={"Authorization": f"Bearer {key}", "User-Agent": "dsh-diag"},
    )
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
    code, body = call(base, "/api/v1/openapi.json", key)
    if code != 200:
        print(f"openapi.json -> {code} {body[:200]}")
        return 1
    spec = json.loads(body)
    paths = spec.get("paths", {})

    groups = {
        "restart/lifecycle": ("restart", "shutdown", "reload", "stop", "start"),
        "plugin enable/disable": ("plugins",),
        "chat / trigger": ("chat", "im/message"),
        "files": ("file",),
        "logs": ("log",),
    }
    for title, keys in groups.items():
        print(f"\n=== {title} ===")
        for p in sorted(paths):
            low = p.lower()
            if not any(k in low for k in keys):
                continue
            for method, meta in paths[p].items():
                scope = (meta.get("openapi_extra") or {}).get("x-astrbot-scope")
                if scope is None:
                    scope = (meta.get("x-astrbot-scope") if isinstance(meta, dict) else None)
                summary = (meta.get("summary") or "")[:46]
                print(f"  {method.upper():<6} {p:<46} scope={scope}  {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
