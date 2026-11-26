"""Regex search project text files; returns path, line number and matching line."""
from servers._mcp import call


def grep_text(pattern: str, glob: str | None = None, limit: int | None = None, root: str | None = None) -> object:
    """Regex search project text files; returns path, line number and matching line."""
    return call("local", "grep_text", {"pattern": pattern, "glob": glob, "limit": limit, "root": root})
