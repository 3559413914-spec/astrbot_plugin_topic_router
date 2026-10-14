"""从 OpenAPI 里打印指定端点的请求/响应 schema（只读）。

用法:
    python tools/show_schema.py <base_url> <api_key> <path_substring> ...
"""

from __future__ import annotations

import json
import sys
import urllib.request


def main() -> int:
    """入口。

    Returns:
        退出码。
    """
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    base, key = sys.argv[1], sys.argv[2]
    wanted = sys.argv[3:]

    req = urllib.request.Request(
        base.rstrip("/") + "/api/v1/openapi.json",
        headers={"Authorization": f"Bearer {key}", "User-Agent": "dsh-diag"},
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        spec = json.loads(resp.read().decode("utf-8", "replace"))

    paths = spec.get("paths", {})
    schemas = spec.get("components", {}).get("schemas", {})

    def resolve(ref: str, depth: int = 0):
        """把 $ref 展开成可读结构。

        Args:
            ref: 引用字符串。
            depth: 递归深度。

        Returns:
            简化后的结构描述。
        """
        if depth > 3 or not isinstance(ref, str) or not ref.startswith("#/"):
            return ref
        node = spec
        for part in ref[2:].split("/"):
            node = node.get(part, {})
        return describe(node, depth + 1)

    def describe(node, depth: int = 0):
        """描述一个 schema 节点。

        Args:
            node: schema 节点。
            depth: 递归深度。

        Returns:
            描述字符串或字典。
        """
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return resolve(node["$ref"], depth)
        if "properties" in node:
            return {
                k: describe(v, depth + 1)
                for k, v in node["properties"].items()
            }
        if "anyOf" in node:
            return [describe(x, depth + 1) for x in node["anyOf"]]
        out = {}
        for key in ("type", "default", "enum", "required", "format"):
            if key in node:
                out[key] = node[key]
        return out or node

    for path in sorted(paths):
        if not any(w in path for w in wanted):
            continue
        for method, meta in paths[path].items():
            print(f"\n=== {method.upper()} {path} ===")
            print(f"  summary: {meta.get('summary')}")
            print(f"  scope  : {(meta.get('openapi_extra') or {}).get('x-astrbot-scope')}")
            rb = meta.get("requestBody")
            if rb:
                content = rb.get("content", {})
                for ctype, cmeta in content.items():
                    print(f"  requestBody ({ctype}):")
                    print("   ", json.dumps(describe(cmeta.get("schema", {})), ensure_ascii=False)[:900])
            params = meta.get("parameters") or []
            for p in params:
                print(
                    f"  param: {p.get('name')} in={p.get('in')} "
                    f"required={p.get('required')} schema={json.dumps(p.get('schema'))[:120]}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
