"""ink_watch - digest of what ink did since the last look, for reviewing its behaviour (INK-track.md).
  python ink_watch.py              new activity in the most recent session (remembers where it stopped)
  python ink_watch.py SESSION      a given session file/stem;  --all  from the start;  --peek  don't advance
Reads ~/.ink/trace/<session>.jsonl (live, every step) when present, else the session JSON (saved per turn)."""
import json, re, sys, time
from pathlib import Path

STATE = Path.home() / ".ink"
SESS = STATE / "sessions"
TRACE = STATE / "trace"
MARK = STATE / "watch-marks.json"


def short(s, n=160):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def load_marks():
    try:
        return json.loads(MARK.read_text("utf-8"))
    except Exception:
        return {}


def digest_session(f, start):
    d = json.loads(f.read_text("utf-8"))
    msgs = d["messages"]
    print(f"session {f.stem} · cwd {d.get('cwd')} · {d.get('profile')}/{d.get('effort')} · ctx {d.get('ctx_used')} · "
          f"{len(msgs)} msgs (new from #{start})")
    if d.get("goal"):
        print("goal:", short(d["goal"], 200))
    if d.get("plan"):
        marks = {"done": "x", "doing": ">", "todo": " ", "dropped": "-"}
        print("plan:", " | ".join(f"[{marks.get(s['status'], '?')}] {short(s['step'], 60)}" for s in d["plan"]))
    calls, fails, sigs, harness = 0, 0, {}, []
    for i, m in enumerate(msgs[start:], start):
        r = m["role"]
        if r == "user":
            c = str(m.get("content") or "")
            if c.startswith("[harness]") or c.startswith("[Stop hook]"):
                harness.append((i, c))
                print(f"#{i} HARNESS  {short(c, 220)}")
            elif not c.startswith("[") or c.startswith("[the user"):
                print(f"#{i} USER     {short(c, 220)}")
        elif r == "assistant":
            t = (m.get("content") or "").strip()
            if t:
                print(f"#{i} SAYS     {short(t, 220)}")
            for tc in m.get("tool_calls") or []:
                calls += 1
                fn = tc["function"]
                sig = fn["name"] + fn.get("arguments", "")
                sigs[sig] = sigs.get(sig, 0) + 1
                print(f"#{i}  → {fn['name']:<10} {short(fn.get('arguments'), 150)}")
        elif r == "tool":
            c = str(m.get("content") or "")
            bad = c.startswith("Error") or "[exit code" in c[-60:] or "FAILED" in c or "SYNTAX CHECK FAILED" in c
            fails += bad
            notes = re.findall(r"\[harness\][^\n]*|DIAGNOSTICS[^\n]*|Step check passed[^\n]*|checks FAILED[^\n]*|tsc --noEmit[^\n]*", c)
            flag = "✗" if bad else "✓"
            print(f"#{i}  {flag} {short(c.split(chr(10))[0], 140)}" + (f"   ⟵ {short(' / '.join(notes), 160)}" if notes else ""))
    reps = [(n, s) for s, n in sigs.items() if n >= 2]
    print(f"\nsummary: {calls} tool calls, {fails} failed results, {len(harness)} harness messages"
          + (f", repeated calls: " + "; ".join(f"{n}x {short(s, 70)}" for n, s in reps) if reps else ""))
    return len(msgs)


def digest_trace(f, start):
    lines = f.read_text("utf-8", "replace").splitlines()
    print(f"trace {f.stem} · {len(lines)} events (new from {start})")
    for l in lines[start:]:
        try:
            e = json.loads(l)
        except Exception:
            continue
        t, ty = e.get("t", ""), e.get("type")
        if ty in ("text_delta", "thinking_delta", "status", "completions"):
            continue
        body = {k: v for k, v in e.items() if k not in ("t", "type")}
        print(f"{t} {ty:<12} {short(json.dumps(body, ensure_ascii=False), 200)}")
    return len(lines)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    every, peek = "--all" in sys.argv, "--peek" in sys.argv
    if args:
        stem = Path(args[0]).stem
    else:
        files = sorted(SESS.glob("*.json"), key=lambda p: p.stat().st_mtime)
        if not files:
            print("no sessions")
            return
        stem = files[-1].stem
    marks = load_marks()
    tr = TRACE / f"{stem}.jsonl"
    if tr.exists():
        key = "trace:" + stem
        n = digest_trace(tr, 0 if every else marks.get(key, 0))
    else:
        key = "sess:" + stem
        n = digest_session(SESS / f"{stem}.json", 0 if every else marks.get(key, 0))
    if not peek:
        marks[key] = n
        MARK.write_text(json.dumps(marks), "utf-8")


if __name__ == "__main__":
    main()
