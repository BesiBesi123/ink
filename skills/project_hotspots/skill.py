"""Rank the largest files in the project and count TODO/FIXME/XXX comments in each."""
from servers.local.list_files import list_files
from servers.local.file_info import file_info
from servers.local.grep_text import grep_text


def hotspots(glob: str = "*.py", top: int = 5) -> list[dict]:
    """Return the `top` biggest files matching glob as [{path, lines, todo}], biggest first."""
    files = list_files(glob=glob, limit=5000)["files"]
    lines = {r["path"]: r["lines"] or 0 for r in file_info(files)}
    todo = {}
    for m in grep_text(r"#.*\b(TODO|FIXME|XXX)\b", glob=glob, limit=5000)["matches"]:
        todo[m["path"]] = todo.get(m["path"], 0) + 1
    rows = [{"path": p, "lines": n, "todo": todo.get(p, 0)} for p, n in lines.items()]
    return sorted(rows, key=lambda r: -r["lines"])[:top]


def table(rows: list[dict]) -> str:
    w = max([len(r["path"]) for r in rows] + [4])
    return "\n".join([f"{'path':<{w}}  lines  todo"] + [f"{r['path']:<{w}}  {r['lines']:>5}  {r['todo']:>4}" for r in rows])
