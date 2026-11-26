"""Download a web page and return its title and readable text (tags, scripts and styles removed)."""
from servers._mcp import call


def fetch_url(url: str, max_chars: int | None = None) -> object:
    """Download a web page and return its title and readable text (tags, scripts and styles removed)."""
    return call("web", "fetch_url", {"url": url, "max_chars": max_chars})
