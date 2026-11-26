"""Go back one page; returns the page afterwards."""
from servers._mcp import call


def back(max_chars: int | None = None) -> object:
    """Go back one page; returns the page afterwards."""
    return call("browser", "back", {"max_chars": max_chars})
