"""Click a button/link/element (CSS selector or its visible text); returns the page afterwards."""
from servers._mcp import call


def click(target: str, max_chars: int | None = None) -> object:
    """Click a button/link/element (CSS selector or its visible text); returns the page afterwards."""
    return call("browser", "click", {"target": target, "max_chars": max_chars})
