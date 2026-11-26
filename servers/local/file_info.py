"""Return byte size and line count for each given project-relative path."""
from servers._mcp import call


def file_info(paths: list) -> object:
    """Return byte size and line count for each given project-relative path."""
    return call("local", "file_info", {"paths": paths})
