# project_hotspots

Rank the largest files in the project and count TODO/FIXME/XXX comments in each (MCP tools: local.list_files, local.file_info, local.grep_text). Keywords: biggest files, line count, todo, fixme, hotspots.

## How to call it

```python
from skills.project_hotspots.skill import *
print(table(hotspots(glob="*.py", top=5)))   # or hotspots(...) for the rows as dicts
```
