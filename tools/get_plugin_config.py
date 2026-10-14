"""读取本插件在 NAS 上的实际配置（只读）。

用法:
    python tools/get_plugin_config.py <base_url> <api_key> [plugin_id]
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

SECRET_HINTS = ("secret", "key", "token", "password", "passwd", "pass")


def call(base: str, path: str, key: str) -> tuple[int, str]:
    """GET 一个 API 路径。

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
    plugin = sys.argv[3] if len(sys.argv) > 3 else "astrbot_plugin_topic_router_pro"

    # 从 openapi.json 里找出所有与 plugin 配置相关的 GET 路径
    code, body = call(base, "/api/v1/openapi.json", key)
    if code != 200:
        print(f"openapi.json -> {code}")
        return 1
    spec = json.loads(body)
    paths = sorted(spec.get("paths", {}).keys())
    print("=== plugin/config related paths ===")
    for p in paths:
        low = p.lower()
        if ("plugin" in low and "config" in low) or low.startswith("/api/v1/config"):
            methods = ",".join(sorted(m.upper() for m in spec["paths"][p]))
            print(f"  {methods:<20} {p}")

    print("\n=== read attempts ===")
    cands = [
        f"/api/v1/plugins/{plugin}/config",
        f"/api/v1/plugins/{plugin}/config/schema",
    ]
    for p in cands:
        code, body = call(base, p, key)
        head = body.replace("\n", " ")[:200]
        print(f"  {code:>3}  {p}\n       {head}")

        if code == 200:
            try:
                data = json.loads(body)
            except Exception:  # noqa: BLE001
                continue
            payload = data.get("data", data)
            if isinstance(payload, dict):
                # 只挑我们关心的开关，避免打印敏感项
                keep = {}
                for k, v in payload.items():
                    if any(h in k.lower() for h in SECRET_HINTS):
                        keep[k] = "<redacted>"
                    else:
                        keep[k] = v
                print("       -> keys:", sorted(keep.keys()))
                for k in ("trace_enabled", "debug_log", "detect_in_hook", "enabled", "mode"):
                    if k in keep:
                        print(f"       {k} = {keep[k]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
