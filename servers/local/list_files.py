"""List project files matching a glob, skipping VCS/cache/venv dirs."""
from servers._mcp import call


def list_files(glob: str | None = None, limit: int | None = None, root: str | None = None) -> object:
    """List project files matching a glob, skipping VCS/cache/venv dirs."""
    return call("local", "list_files", {"glob": glob, "limit": limit, "root": root})
