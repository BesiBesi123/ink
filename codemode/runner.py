"""Child side of code mode: runs one script with full access (any file, programs, network).

MCP tools are called through the host's proxy, which masks personal data in their results.
usage: python -B runner.py <script.py>
"""
import os
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [APP]

from codemode import helpers  # noqa: E402
from servers import _mcp  # noqa: E402

os.chdir(os.environ["INK_PROJECT"])
g = {"__name__": "__main__", "search_tools": helpers.search_tools, "save_skill": helpers.save_skill,
     "list_skills": helpers.list_skills, "call": _mcp.call}
with open(sys.argv[1], encoding="utf-8") as f:
    src = f.read()
exec(compile(src, "<code>", "exec"), g)
