"""Type text into an input (CSS selector, label, placeholder or text); submit=True presses Enter."""
from servers._mcp import call


def fill(target: str, text: str, submit: bool | None = None, max_chars: int | None = None) -> object:
    """Type text into an input (CSS selector, label, placeholder or text); submit=True presses Enter."""
    return call("browser", "fill", {"target": target, "text": text, "submit": submit, "max_chars": max_chars})
