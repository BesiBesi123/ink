"""Sleep/dream for ink: after a session, the model rereads a condensed transcript and writes durable lessons to
memory (one fact per file in ~/.ink/memory plus a line in MEMORY.md). Runs detached after /exit, or with /sleep.

The model only proposes notes as JSON; this code writes the files, so a bad reply can't touch anything else.
"""
import json, re, http.client, datetime
from pathlib import Path

from recall import exchanges, index_session

MAX_TRANSCRIPT = 40000   # chars (~10k tokens): keeps the dream inside the 32k context with room for the reply
MAX_NOTES = 5
MIN_EXCHANGES = 2


def _post(port, body, timeout=900):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request("POST", "/v1/chat/completions", json.dumps(body), {"Content-Type": "application/json"})
    r = conn.getresponse()
    data = json.loads(r.read() or b"{}")
    conn.close()
    if r.status != 200:
        raise RuntimeError(f"HTTP {r.status}: {str(data)[:200]}")
    return data["choices"][0]["message"].get("content") or ""


def _existing(memory_dir):
    idx = (memory_dir / "MEMORY.md")
    parts = ["MEMORY.md:\n" + (idx.read_text("utf-8", "replace") if idx.is_file() else "(empty)")]
    total = 0
    for f in sorted(memory_dir.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        t = f.read_text("utf-8", "replace")
        total += len(t)
        if total > 8000:
            break
        parts.append(f"--- {f.name}\n{t}")
    return "\n\n".join(parts)


PROMPT = """You are Ink's memory. Below is a condensed transcript of a finished coding session, then the current memory notes.

Decide what is worth remembering for FUTURE sessions. Good notes: the user's preferences and corrections (how they want things done), project facts that are not obvious from the code (where things live, how to build/run/deploy, constraints), and lessons (an approach that failed and why, a gotcha, a fix that worked). Bad notes: details of this one task, things the code or git history already shows, guesses.

Rules:
- At most {n} notes. Fewer is better. If nothing is worth keeping, return an empty list.
- Skip anything the current memory already says.
- To add to an existing file, use its exact file name: your content is APPENDED to that file, so write only the new facts (never repeat or rewrite what is there). If the session proved an existing fact wrong, add a line starting with "Correction:".
- For a new topic, use a new file name. Each note: short, specific, one topic. Lessons say what to do next time.

Reply with ONLY a JSON object, no other text:
{{"notes": [{{"file": "kebab-name.md", "title": "Short title", "hook": "one-line summary for the index", "content": "the note in markdown"}}]}}

=== SESSION ({cwd}) ===
{transcript}

=== CURRENT MEMORY ===
{memory}"""


def _transcript(messages):
    ex = exchanges(messages)
    text = "\n\n".join(ex)
    if len(text) > MAX_TRANSCRIPT:  # keep the first request (the goal) and the most recent work
        head = ex[0] if ex else ""
        text = head + "\n\n[... middle of the session omitted ...]\n\n" + text[-(MAX_TRANSCRIPT - len(head)):]
    return text, len(ex)


def _parse(reply):
    reply = re.sub(r"^```(json)?|```$", "", reply.strip(), flags=re.M).strip()
    m = re.search(r"\{[\s\S]*\}", reply)
    if not m:
        return []
    notes = json.loads(m.group(0)).get("notes") or []
    return [n for n in notes if isinstance(n, dict) and n.get("content")][:MAX_NOTES]


def _write(memory_dir, notes):
    memory_dir.mkdir(parents=True, exist_ok=True)
    idx = memory_dir / "MEMORY.md"
    lines = idx.read_text("utf-8", "replace").splitlines() if idx.is_file() else ["# Ink memory"]
    written = []
    for n in notes:
        name = re.sub(r"[^a-z0-9-]+", "-", str(n.get("file", "")).lower().removesuffix(".md")).strip("-")[:60]
        if not name or name == "memory":
            continue
        f = memory_dir / f"{name}.md"
        title = str(n.get("title") or name).strip()
        body = str(n["content"]).strip()
        if f.exists():  # never rewrite a note: append, so a bad dream can't erase what was known
            old = f.read_text("utf-8", "replace").rstrip()
            if body in old:
                continue
            f.write_text(f"{old}\n\nAdded {datetime.date.today()}:\n{body}\n", "utf-8")
        else:
            f.write_text(f"# {title}\n{body}\n", "utf-8")
        if not any(f"]({f.name})" in l for l in lines):  # existing index lines are kept as they are
            lines.append(f"- [{title}]({f.name}) - {str(n.get('hook') or '').strip()}")
        written.append(f.name)
    idx.write_text("\n".join(lines).rstrip() + "\n", "utf-8")
    return written


def dream(session_file, port, kwargs, memory_dir, db_path, log=lambda m: None):
    """Reflect on one session. Returns the list of memory files written (or an empty list)."""
    session_file, memory_dir = Path(session_file), Path(memory_dir)
    data = json.loads(session_file.read_text("utf-8"))
    messages = data.get("messages", [])
    index_session(db_path, session_file, data)
    n_msgs = len(messages)
    if data.get("dreamed_at_msgs") == n_msgs:
        return []
    transcript, n_ex = _transcript(messages)
    if n_ex < MIN_EXCHANGES and sum(len(m.get("tool_calls") or []) for m in messages) < 6:
        return []
    body = {"model": "local", "max_tokens": 4000, "temperature": 0.6,
            "messages": [{"role": "user", "content": PROMPT.format(n=MAX_NOTES, cwd=data.get("cwd", ""),
                                                                   transcript=transcript, memory=_existing(memory_dir))}],
            "chat_template_kwargs": kwargs}
    notes = _parse(_post(port, body))
    written = _write(memory_dir, notes) if notes else []
    data = json.loads(session_file.read_text("utf-8"))  # re-read: the session may have been saved meanwhile
    data["dreamed_at_msgs"] = n_msgs
    data["dreamed"] = datetime.datetime.now().isoformat(timespec="seconds")
    session_file.write_text(json.dumps(data), "utf-8")
    log(f"dream {session_file.name}: {written or 'nothing new'}")
    return written
