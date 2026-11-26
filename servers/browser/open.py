"""Go to url and return {url, title, text}."""
from servers._mcp import call


def open(url: str, max_chars: int | None = None) -> object:
    """Go to url and return {url, title, text}."""
    return call("browser", "open", {"url": url, "max_chars": max_chars})
