"""Save a PNG of the page (path relative to the project)."""
from servers._mcp import call


def screenshot(path: str | None = None, full_page: bool | None = None) -> object:
    """Save a PNG of the page (path relative to the project)."""
    return call("browser", "screenshot", {"path": path, "full_page": full_page})
