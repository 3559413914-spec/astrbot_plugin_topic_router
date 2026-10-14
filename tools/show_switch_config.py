"""只读查询：本插件与"自动切换话题 / 提示"相关的配置。

用法:
    python tools/show_switch_config.py <base_url> <api_key>
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

WATCH = (
    "mode",
    "auto_threshold",
    "min_new_topic_confidence",
    "new_conversation_notice",
    "always_llm",
    "sim_continue",
    "sim_new_topic",
    "time_gap_minutes",
    "enabled",
)


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
    plugin = "astrbot_plugin_topic_router_pro"
    code, body = call(base, f"/api/v1/plugins/{plugin}/config", key)
    if code != 200:
        print(f"HTTP {code}: {body[:200]}")
        return 1
    data = json.loads(body).get("data", {})
    cfg = data.get("config") or {}
    inner = cfg.get(plugin) if isinstance(cfg.get(plugin), dict) else cfg
    print(f"plugin={data.get('plugin_name')}  log_level={data.get('log_level')!r}\n")
    for k in WATCH:
        if k in inner:
            print(f"  {k:<28} = {inner[k]!r}")
    print(f"\n  (共 {len(inner)} 个键)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
