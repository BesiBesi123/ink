"""Regenerate servers/<server>/<tool>.py wrappers and servers/_registry.json from live MCP servers.

Usage: python -m servers._discover
"""
import json
import re
from pathlib import Path

from servers import _mcp

ROOT = _mcp.PROJECT_ROOT
HERE = Path(__file__).resolve().parent
STOP = {"the", "a", "an", "and", "or", "to", "of", "for", "with", "in", "on", "by", "is", "are",
        "this", "that", "it", "as", "be", "from", "when", "which", "each", "your", "you", "will"}


def _ident(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z_]+", "_", name).strip("_")
    return s if s and not s[0].isdigit() else f"t_{s}"


def _pytype(schema: dict) -> str:
    t = (schema or {}).get("type")
    return {"string": "str", "integer": "int", "number": "float", "boolean": "bool",
            "array": "list", "object": "dict"}.get(t, "object")


def _keywords(name: str, desc: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", f"{name} {desc}".lower())
    seen = []
    for w in words:
        if len(w) > 2 and w not in STOP and w not in seen:
            seen.append(w)
    return seen[:14]


def _wrapper(server: str, tool: str, schema: dict, desc: str) -> str:
    props = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    params, passed = [], []
    for pname, pschema in props.items():
        ident = _ident(pname)
        ann = _pytype(pschema)
        if pname in required:
            params.append(f"{ident}: {ann}")
        else:
            params.append(f"{ident}: {ann} | None = None")
        passed.append(f'"{pname}": {ident}')
    sig = ", ".join(params)
    body = ", ".join(passed)
    one = desc.replace('"', "'").replace("\n", " ").strip()[:160]
    return (
        f'"""{one}"""\n'
        "from servers._mcp import call\n\n\n"
        f"def {_ident(tool)}({sig}) -> object:\n"
        f'    """{one}"""\n'
        f'    return call("{server}", "{tool}", {{{body}}})\n'
    )


def main():
    servers = _mcp.load_servers()
    registry = []
    for server in sorted(servers):
        try:
            tools = _mcp.list_tools(server)
        except Exception as e:
            print(f"skip {server}: {type(e).__name__}: {e}")
            continue
        folder = HERE / _ident(server)
        folder.mkdir(exist_ok=True)
        (folder / "__init__.py").write_text("", "utf-8")
        for t in tools:
            name, desc = t["name"], (t.get("description") or "").strip()
            schema = t.get("inputSchema") or {}
            fname = _ident(name) + ".py"
            (folder / fname).write_text(_wrapper(server, name, schema, desc), "utf-8")
            registry.append({
                "server": server, "tool": name, "path": f"servers/{_ident(server)}/{fname}",
                "import": f"from servers.{_ident(server)}.{_ident(name)} import {_ident(name)}",
                "description": desc, "keywords": _keywords(name, desc),
                "required": list(schema.get("required") or []),
            })
        print(f"{server}: {len(tools)} tool(s) -> servers/{_ident(server)}/")
    (HERE / "_registry.json").write_text(json.dumps(registry, indent=2), "utf-8")
    print(f"registry: {len(registry)} tool(s) -> servers/_registry.json")


if __name__ == "__main__":
    main()
