"""Search the web (Bing, DuckDuckGo fallback). Returns up to `limit` results with title, url and snippet."""
from servers._mcp import call


def web_search(query: str, limit: int | None = None) -> object:
    """Search the web (Bing, DuckDuckGo fallback). Returns up to `limit` results with title, url and snippet."""
    return call("web", "web_search", {"query": query, "limit": limit})
