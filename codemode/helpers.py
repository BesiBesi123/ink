"""Helpers preloaded into every code-mode run: search_tools, save_skill, list_skills, call."""
import json
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
REGISTRY = APP / "servers" / "_registry.json"
SKILLS = APP / "skills"


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def _skill_entries() -> list[dict]:
    out = []
    for md in sorted(SKILLS.glob("*/SKILL.md")):
        text = md.read_text("utf-8", "replace")
        first = next((l.strip("# ").strip() for l in text.splitlines() if l.strip() and not l.startswith("#")), "")
        out.append({"kind": "skill", "name": md.parent.name, "description": first,
                    "import": f"from skills.{md.parent.name}.skill import *", "path": f"skills/{md.parent.name}/SKILL.md",
                    "text": text})
    return out


def search_tools(query: str = "", limit: int = 8) -> list[dict]:
    """Find MCP tools and saved skills by name or keyword. Returns name, one-line description and the import line.
    Saved skills are listed first: reuse one before writing new code. An empty query lists everything."""
    tools = [dict(t, kind="tool", name=f"{t['server']}.{t['tool']}") for t in json.loads(REGISTRY.read_text("utf-8"))]
    entries = _skill_entries() + tools
    q = _words(query)
    scored = []
    for e in entries:
        hay = " ".join([e["name"], e.get("description", ""), " ".join(e.get("keywords", [])), e.get("text", "")]).lower()
        name = e["name"].lower()
        score = sum(3 if w in name else 1 for w in q if w in hay) if q else 1
        if score:
            scored.append((score * (2 if e["kind"] == "skill" else 1), e))
    scored.sort(key=lambda x: -x[0])
    return [{"kind": e["kind"], "name": e["name"], "description": e.get("description", "")[:160],
             "import": e["import"], "required": e.get("required", [])} for _, e in scored[:limit]]


def list_skills() -> list[str]:
    return [e["name"] for e in _skill_entries()]


def save_skill(name: str, code: str, description: str, usage: str) -> str:
    """Save working code as skills/<name>/skill.py with a SKILL.md (what it does + how to call it)."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", name):
        raise ValueError("skill name must be snake_case, e.g. count_todos")
    funcs = re.findall(r"^def ([a-zA-Z_]\w*)\(", code, re.M)
    if not funcs:
        raise ValueError("a skill must define a function (def name(...)) with parameters, not run on import")
    if not any(f"{f}(" in usage for f in funcs):
        raise ValueError(f"usage must show a call, e.g. print({funcs[0]}(...))")
    if re.search(r"""^(?!def |class |import |from |@|\s|#|$|["')\]}]|[A-Z_][A-Z0-9_]*\s*=)""", code, re.M):
        raise ValueError("only imports, constants, functions and classes at the top level; it must not run on import")
    d = SKILLS / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "skill.py").write_text(f'"""{description.strip()}"""\n' + code.strip() + "\n", "utf-8")
    (d / "SKILL.md").write_text(f"# {name}\n\n{description.strip()}\n\n## How to call it\n\n```python\n"
                                f"from skills.{name}.skill import *\n{usage.strip()}\n```\n", "utf-8")
    return f"saved skills/{name}/ (skill.py, SKILL.md)"
