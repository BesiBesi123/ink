"""Links on the page as [{text, href}], optionally only those whose text or href contains `contains`."""
from servers._mcp import call


def links(contains: str | None = None, limit: int | None = None) -> object:
    """Links on the page as [{text, href}], optionally only those whose text or href contains `contains`."""
    return call("browser", "links", {"contains": contains, "limit": limit})
