"""Settings + hooks for ink (Claude Code-style).

Settings are layered: ~/.ink/settings.json, then <project>/.ink/settings.json (project wins; hook lists add up).
{
  "hooks": {
    "PreToolUse":  [{"matcher": "edit_file|write_file", "command": "python check_path.py"}],
    "PostToolUse": [{"matcher": "edit_file", "command": "npx prettier --write \\"$INK_FILE\\""}],
    "UserPromptSubmit": [...], "Stop": [...], "SessionStart": [...], "PreCompact": [...]
  },
  "ask":  ["regex of commands that always need your OK", ...],
  "deny": ["regex of commands that are never run", ...]
}
A hook command gets the event as JSON on stdin and env INK_EVENT / INK_TOOL / INK_FILE / INK_PROJECT.
Exit code 2 = block (PreToolUse: the call is refused; Stop: the turn continues with stderr as feedback;
UserPromptSubmit: the prompt is refused). stdout of PostToolUse / UserPromptSubmit / SessionStart hooks is
added to the model's context. Any other exit code is ignored (a broken hook never breaks ink). Timeout 60 s.
"""
import json, os, re, subprocess
from pathlib import Path

HOME_SETTINGS = Path.home() / ".ink" / "settings.json"
EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "SessionStart", "PreCompact")
BASH = r"C:\Program Files\Git\bin\bash.exe"


def _load(p):
    try:
        return json.loads(Path(p).read_text("utf-8")) if Path(p).is_file() else {}
    except Exception:
        return {}


def load_settings(cwd):
    base, proj = _load(HOME_SETTINGS), _load(Path(cwd) / ".ink" / "settings.json")
    merged = {**base, **{k: v for k, v in proj.items() if k not in ("hooks", "ask", "deny")}}
    merged["hooks"] = {e: (base.get("hooks", {}).get(e) or []) + (proj.get("hooks", {}).get(e) or []) for e in EVENTS}
    merged["ask"] = (base.get("ask") or []) + (proj.get("ask") or [])
    merged["deny"] = (base.get("deny") or []) + (proj.get("deny") or [])
    return merged


class Hooks:
    def __init__(self, cwd, log=lambda m: None):
        self.cwd = Path(cwd)
        self.log = log
        self.settings = load_settings(cwd)
        self.hooks = self.settings["hooks"]

    def active(self):
        return any(self.hooks.values())

    def _run(self, event, payload, tool="", file=""):
        outs, block = [], None
        for h in self.hooks.get(event) or []:
            m = h.get("matcher")
            if m and tool and not re.fullmatch(m, tool):
                continue
            cmd = h.get("command")
            if not cmd:
                continue
            env = dict(os.environ, INK_EVENT=event, INK_TOOL=tool, INK_FILE=file, INK_PROJECT=str(self.cwd))
            argv = [BASH, "-c", cmd] if os.name == "nt" and Path(BASH).exists() else ["bash", "-c", cmd]
            try:
                r = subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=int(h.get("timeout", 60)), cwd=self.cwd, env=env)
            except Exception as e:
                self.log(f"hook {event} {cmd!r} failed: {e!r}")
                continue
            if r.returncode == 2:
                block = (r.stderr or r.stdout or "blocked").strip()[:2000]
                break
            if r.stdout.strip():
                outs.append(r.stdout.strip()[:4000])
        return block, "\n".join(outs)

    def pre_tool(self, tool, args, cwd):
        if not self.hooks.get("PreToolUse"):
            return None
        block, _ = self._run("PreToolUse", {"event": "PreToolUse", "tool": tool, "args": args, "cwd": str(cwd)},
                             tool, str(args.get("path") or ""))
        return block

    def post_tool(self, tool, args, result, cwd):
        if not self.hooks.get("PostToolUse"):
            return ""
        _, text = self._run("PostToolUse", {"event": "PostToolUse", "tool": tool, "args": args,
                                            "result": result[:8000], "cwd": str(cwd)}, tool, str(args.get("path") or ""))
        return text

    def prompt(self, text):
        """-> (blocked_reason or None, extra_context)"""
        if not self.hooks.get("UserPromptSubmit"):
            return None, ""
        return self._run("UserPromptSubmit", {"event": "UserPromptSubmit", "prompt": text, "cwd": str(self.cwd)})

    def stop(self, last_message):
        """-> feedback text if a Stop hook wants the turn to continue, else None"""
        if not self.hooks.get("Stop"):
            return None
        block, _ = self._run("Stop", {"event": "Stop", "last_message": last_message[:8000], "cwd": str(self.cwd)})
        return block

    def simple(self, event, payload=None):
        if not self.hooks.get(event):
            return ""
        _, text = self._run(event, dict(payload or {}, event=event, cwd=str(self.cwd)))
        return text
