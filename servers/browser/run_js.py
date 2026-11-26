"""Evaluate a JavaScript expression or function in the page and return its (JSON) result."""
from servers._mcp import call


def run_js(script: str) -> object:
    """Evaluate a JavaScript expression or function in the page and return its (JSON) result."""
    return call("browser", "run_js", {"script": script})
