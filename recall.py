"""Searchable memory for ink: past sessions and memory notes in a SQLite FTS5 index (BM25 ranking, stdlib only).

Each session is split into exchanges (a user request plus what the agent did and said), so a hit brings back
one coherent piece of work. Sessions are re-indexed on every save; memory notes on every search.
"""
import json, re, sqlite3
from pathlib import Path

CHUNK_CHARS = 1500


def _db(path):
    c = sqlite3.connect(str(path))
    c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(text, source UNINDEXED, cwd UNINDEXED, "
              "ts UNINDEXED, kind UNINDEXED, tokenize='porter unicode61')")
    return c


def _short(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def exchanges(messages):
    """Group a session's messages into exchanges: one per real user request."""
    out, cur = [], []
    for m in messages:
        role, content = m.get("role"), m.get("content") or ""
        if role == "system":
            continue
        if role == "user" and not content.startswith("[harness]"):
            if cur:
                out.append(cur)
            cur = [f"USER: {_short(content, 700)}"]
        elif role == "assistant":
            if content:
                cur.append(f"INK: {_short(content, 600)}")
            for tc in m.get("tool_calls") or []:
                f = tc.get("function") or {}
                cur.append(f"did {f.get('name')}({_short(f.get('arguments'), 160)})")
        elif role == "tool" and content.startswith("Error"):
            cur.append(f"error: {_short(content, 200)}")
    if cur:
        out.append(cur)
    return ["\n".join(x)[:CHUNK_CHARS] for x in out]


def index_session(db_path, session_file, data):
    try:
        c = _db(db_path)
        src = Path(session_file).name
        ts = src[:15]
        with c:
            c.execute("DELETE FROM chunks WHERE source = ?", (src,))
            c.executemany("INSERT INTO chunks (text, source, cwd, ts, kind) VALUES (?, ?, ?, ?, 'session')",
                          [(t, src, data.get("cwd", ""), ts) for t in exchanges(data.get("messages", []))])
        c.close()
    except Exception:
        pass


def index_memory(db_path, memory_dir):
    c = _db(db_path)
    with c:
        c.execute("DELETE FROM chunks WHERE kind = 'memory'")
        for f in sorted(Path(memory_dir).glob("*.md")):
            if f.name == "MEMORY.md":
                continue
            c.execute("INSERT INTO chunks (text, source, cwd, ts, kind) VALUES (?, ?, '', '', 'memory')",
                      (f.read_text("utf-8", "replace")[:CHUNK_CHARS * 2], f.name))
    c.close()


def search(db_path, memory_dir, query, limit=5, exclude=None):
    """Return a text block with the best-matching past exchanges and memory notes."""
    index_memory(db_path, memory_dir)
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 1][:12]
    if not words:
        return "Error: give recall some keywords."
    q = " OR ".join(f'"{w}"' for w in words)
    c = _db(db_path)
    rows = c.execute("SELECT text, source, cwd, ts, kind FROM chunks WHERE chunks MATCH ? AND source != ? "
                     "ORDER BY bm25(chunks) LIMIT ?", (q, exclude or "", int(limit))).fetchall()
    c.close()
    if not rows:
        return f"No past sessions or memory notes match: {query}"
    parts = []
    for text, source, cwd, ts, kind in rows:
        if kind == "memory":
            head = f"[memory note {source}]"
        else:
            when = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}" if len(ts) >= 13 else ts
            head = f"[session {when} in {cwd}]"
        parts.append(head + "\n" + text)
    return "\n\n".join(parts)
