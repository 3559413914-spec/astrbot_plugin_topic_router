"""方案 A：重载插件 + 用 chat API 触发一条测试消息，并记录精确时间戳。

目的：让插件按当前配置（trace_enabled 已为 True）重新实例化，然后触发一次管道，
供人工比对 trace.log 是否新增 ENTRY 行。

用法:
    python tools/trigger_probe.py <base_url> <api_key>
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request


def post(base: str, path: str, key: str, payload: dict | None = None, timeout: int = 60):
    """POST 一个 API 路径。

    Args:
        base: 基地址。
        path: 路径。
        key: API Key。
        payload: JSON body。
        timeout: 超时秒数。

    Returns:
        (状态码, 响应体)。
    """
    data = json.dumps(payload).encode() if payload is not None else b"{}"
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "dsh-diag",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    marker = f"dsh-probe-{int(time.time())}"
    print(f"local time   : {stamp}")
    print(f"marker       : {marker}\n")

    # --- 1) 重载插件，让它按当前配置重新实例化 ---
    code, body = post(base, f"/api/v1/plugins/{plugin}/reload", key)
    print(f"[1] reload  -> HTTP {code}")
    print(f"    {body[:300]}\n")

    # 给插件一点时间重新构造
    time.sleep(4)

    # --- 2) 触发一条消息（webchat 会话，不碰用户的微信会话） ---
    payload = {
        "message": f"入口探针测试 {marker} 请忽略这条消息",
        "username": "dsh_probe",
        "session_id": f"dsh_probe_{marker}",
        "enable_streaming": False,
    }
    t0 = time.time()
    print(f"[2] chat    -> 发送时间 {time.strftime('%H:%M:%S', time.localtime(t0))}")
    code, body = post(base, "/api/v1/chat", key, payload, timeout=120)
    t1 = time.time()
    print(f"    HTTP {code}  用时 {t1 - t0:.1f}s")
    print(f"    {body[:400]}\n")

    print("=" * 62)
    print("请在 NAS 上立即查看以下文件的内容（尤其最后几行）：")
    print("  /vol1/@appdata/astrbot/data/plugin_data/astrbot_plugin_topic_router_pro/trace.log")
    print("")
    print(f"要找的标记: {marker}")
    print(f"对应时间  : {time.strftime('%H:%M:%S', time.localtime(t0))} 前后")
    print("")
    print("判读：")
    print("  出现 ENTRY ... msg='入口探针测试 <marker> ...'  -> 入口在跑")
    print("  只有 HOOK/VERDICT、没有同时间的 ENTRY          -> 入口被上游静默")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
