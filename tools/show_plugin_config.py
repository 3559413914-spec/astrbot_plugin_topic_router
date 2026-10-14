"""打印本插件在 NAS 上的关键配置项与每插件日志级别（只读）。

用法:
    python tools/show_plugin_config.py <base_url> <api_key> [plugin_id]
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

WATCH = (
    "enabled",
    "mode",
    "trace_enabled",
    "debug_log",
    "detect_in_hook",
    "provider_id",
    "always_llm",
    "timeout_seconds",
)


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

    code, body = call(base, f"/api/v1/plugins/{plugin}/config", key)
    print(f"GET config -> {code}")
    if code != 200:
        print(body[:400])
        return 1

    data = json.loads(body).get("data", {})
    print(f"plugin_name : {data.get('plugin_name')}")
    print(f"log_level   : {data.get('log_level')!r}     <-- 每插件日志级别（null=跟随全局）")

    cfg = data.get("config")
    if isinstance(cfg, dict):
        # 有时是 {plugin_name: {...}}
        inner = cfg.get(plugin) if plugin in cfg and isinstance(cfg[plugin], dict) else cfg
        print("\n=== 关键配置项 ===")
        for k in WATCH:
            if k in inner:
                print(f"  {k:<20} = {inner[k]!r}")
        print(f"\n  (共 {len(inner)} 个键)")
    else:
        print(f"\nconfig 不是 dict: {type(cfg)}")

    # schema 里看 trace_enabled 的定义是否存在（确认装的是带该开关的版本）
    code, body = call(base, f"/api/v1/plugins/{plugin}/config/schema", key)
    if code == 200:
        schema_text = body
        has_trace = "trace_enabled" in schema_text
        print(f"\nschema 里存在 trace_enabled 定义: {has_trace}")
        idx = schema_text.find("trace_enabled")
        if idx > 0:
            print("  " + schema_text[max(0, idx - 40) : idx + 220].replace("\n", " "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
