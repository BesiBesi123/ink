"""Readable text of the current page, or of one element."""
from servers._mcp import call


def read(selector: str | None = None, max_chars: int | None = None) -> object:
    """Readable text of the current page, or of one element."""
    return call("browser", "read", {"selector": selector, "max_chars": max_chars})
