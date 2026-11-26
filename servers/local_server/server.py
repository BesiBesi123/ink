"""Local MCP server (stdio) exposing real project-introspection tools."""
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("INK_PROJECT") or os.getcwd()).resolve()
SKIP = {".git", "__pycache__", ".venv", "venv", "node_modules", ".codemode", ".mypy_cache"}


def _safe(rel: str) -> Path:
    """Project-relative or absolute path, anywhere on the machine."""
    return (ROOT / os.path.expanduser(rel)).resolve()


def _rel(p: Path) -> str:
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def list_files(glob: str = "**/*", limit: int = 200, root: str = ".") -> dict:
    base = _safe(root)
    hits = []
    for p in base.rglob("*"):
        if p.is_dir() or any(part in SKIP for part in p.relative_to(base).parts):
            continue
        rel = _rel(p)
        if fnmatch.fnmatch(p.relative_to(base).as_posix(), glob) or fnmatch.fnmatch(p.name, glob):
            hits.append(rel)
    hits.sort()
    return {"files": hits[:limit], "total": len(hits), "truncated": len(hits) > limit}


def file_info(paths: list[str]) -> list[dict]:
    out = []
    for rel in paths:
        p = _safe(rel)
        if not p.is_file():
            out.append({"path": rel, "error": "not a file"})
            continue
        data = p.read_bytes()
        try:
            lines = data.decode("utf-8").count("\n") + 1
        except UnicodeDecodeError:
            lines = None
        out.append({"path": rel, "bytes": len(data), "lines": lines})
    return out


def grep_text(pattern: str, glob: str = "**/*.py", limit: int = 50, root: str = ".") -> dict:
    rx = re.compile(pattern)
    hits = []
    base = _safe(root)
    for p in base.rglob("*"):
        if p.is_dir() or any(part in SKIP for part in p.relative_to(base).parts):
            continue
        rel = _rel(p)
        if not (fnmatch.fnmatch(p.relative_to(base).as_posix(), glob) or fnmatch.fnmatch(p.name, glob)):
            continue
        try:
            for n, line in enumerate(p.read_text("utf-8").splitlines(), 1):
                if rx.search(line):
                    hits.append({"path": rel, "line": n, "text": line.strip()[:200]})
                    if len(hits) >= limit:
                        return {"matches": hits, "truncated": True}
        except (UnicodeDecodeError, OSError):
            continue
    return {"matches": hits, "truncated": False}


TOOLS = {
    "list_files": {
        "description": "List project files matching a glob, skipping VCS/cache/venv dirs.",
        "fn": list_files,
        "schema": {"type": "object", "properties": {
            "glob": {"type": "string", "description": "e.g. **/*.py"},
            "limit": {"type": "integer"}, "root": {"type": "string"}}, "required": []},
    },
    "file_info": {
        "description": "Return byte size and line count for each given project-relative path.",
        "fn": file_info,
        "schema": {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"}}}, "required": ["paths"]},
    },
    "grep_text": {
        "description": "Regex search project text files; returns path, line number and matching line.",
        "fn": grep_text,
        "schema": {"type": "object", "properties": {
            "pattern": {"type": "string"}, "glob": {"type": "string"}, "limit": {"type": "integer"},
            "root": {"type": "string"}},
            "required": ["pattern"]},
    },
}


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
            "serverInfo": {"name": "ink-local", "version": "1.0"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": n, "description": t["description"], "inputSchema": t["schema"]} for n, t in TOOLS.items()]}}
    if method == "tools/call":
        params = msg.get("params") or {}
        tool = TOOLS.get(params.get("name"))
        if not tool:
            return {"jsonrpc": "2.0", "id": mid, "result": {"isError": True, "content": [
                {"type": "text", "text": f"unknown tool {params.get('name')}"}]}}
        try:
            data = tool["fn"](**(params.get("arguments") or {}))
            return {"jsonrpc": "2.0", "id": mid, "result": {"isError": False, "content": [
                {"type": "text", "text": json.dumps(data)}]}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {"isError": True, "content": [
                {"type": "text", "text": f"{type(e).__name__}: {e}"}]}}
    if method and method.startswith("notifications/"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            resp = handle(json.loads(line))
        except Exception as e:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
