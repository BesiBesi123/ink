"""playtest tool for ink: runs playtest.mjs (headless Chrome, SwiftShader) and returns a compact text report.

    from playtest import run_playtest, TOOL_SCHEMA
    report, summary = run_playtest({"page": "index.html", "steps": [...]}, cwd)
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "playtest.mjs"
IS_WIN = os.name == "nt"

# Top-level keys sort alphabetically as page, root, seconds, size, steps (the chat template sorts them),
# which is also the natural writing order: what to open, then how long, then what to do.
TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "playtest",
        "description": (
            "Open a browser game/page in headless Chrome, play it with scripted input, and get a text report: "
            "PASS/FAIL, load time, fps, canvas/WebGL info, screen stats, uncaught exceptions with file:line, "
            "console errors/warnings, failed requests, and one result line per step. "
            "page = .html file (served over http so ES modules work) or http URL. "
            "Steps: {\"wait\":ms} {\"key\":\"ArrowRight\",\"ms\":800} (hold; key can be a list for combos; names: "
            "arrows, a-z, 0-9, Space, Enter, Escape, Shift, Tab) {\"type\":\"text\"} {\"click\":[x,y] or \"css selector\"} "
            "{\"drag\":[x1,y1,x2,y2]} {\"eval\":\"js\"} {\"expect\":\"js\",\"label\":\"...\"} (assert truthy) "
            "{\"wait_for\":\"js\",\"timeout\":ms} {\"screenshot\":\"shot.png\"}. "
            "Example steps: [{\"wait\":500},{\"eval\":\"window.game.player\"},{\"key\":\"ArrowRight\",\"ms\":800},"
            "{\"expect\":\"window.game.player.x > 100\",\"label\":\"player moves right\"}]"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "page": {"type": "string", "description": "html file path (relative to cwd) or http(s) URL"},
                "root": {"type": "string", "description": "optional folder to serve (default: the html file's folder)"},
                "seconds": {"type": "number", "description": "minimum run time after load, default 3"},
                "size": {"type": "string", "description": "viewport WxH, default 1280x720"},
                "steps": {"type": "array", "items": {"type": "object"}, "description": "input/check steps, run in order"},
            },
            "required": ["page"],
        },
    },
}


def _kill_tagged_chrome(tag):
    """Backstop if node had to be force-killed: kill Chrome processes using our unique profile, remove the dir."""
    if not tag:
        return
    if IS_WIN:
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' or Name='msedge.exe'\" | "
              f"Where-Object {{ $_.CommandLine -like '*playtest-{tag}-*' }} | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=20,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass
    else:
        subprocess.run(["pkill", "-9", "-f", f"playtest-{tag}-"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tmp = Path(tempfile.gettempdir())
    for d in tmp.glob(f"playtest-{tag}-*"):
        for _ in range(5):
            shutil.rmtree(d, ignore_errors=True)
            if not d.exists():
                break
            time.sleep(0.5)


def _norm_args(args):
    a = dict(args or {})
    page = a.get("page") or a.get("url") or a.get("file") or a.get("path")
    steps = a.get("steps") or []
    if isinstance(steps, str):  # small models sometimes send the array as a JSON string
        try:
            steps = json.loads(steps)
        except Exception:
            raise ValueError("steps must be a JSON array of step objects")
    if isinstance(steps, dict):
        steps = [steps]
    if not isinstance(steps, list):
        raise ValueError("steps must be a list")
    cfg = {"page": page, "steps": steps}
    for k in ("seconds", "size", "width", "height", "gpu", "ascii"):
        if a.get(k) not in (None, ""):
            cfg[k] = a[k]
    root = a.get("root") or a.get("serve_root")
    if root:
        cfg["root"] = root
    return cfg


def run_playtest(args: dict, cwd: str, timeout=180, stop=None):
    """Returns (report_text, summary_str). Never raises."""
    t0 = time.time()
    try:
        cfg = _norm_args(args)
    except ValueError as e:
        return f"playtest error: {e}", "playtest: bad arguments"
    if not cfg.get("page"):
        return "playtest error: give page (an .html file path or http URL)", "playtest: bad arguments"
    node = shutil.which("node")
    if not node:
        return "playtest error: node not found on PATH", "playtest: node missing"
    cwd = str(Path(cwd or os.getcwd()).resolve())
    page = str(cfg["page"])
    if not re.match(r"^(https?|data|about|file):", page, re.I):
        p = Path(page)
        cfg["page"] = str(p if p.is_absolute() else Path(cwd, p).resolve())
    if cfg.get("root") and not Path(cfg["root"]).is_absolute():
        cfg["root"] = str(Path(cwd, cfg["root"]).resolve())
    tag = uuid.uuid4().hex[:10]
    cfg["cwd"] = cwd
    cfg["profile_tag"] = tag
    cfg["timeout"] = max(20, int(timeout) - 15)  # node's own hard timeout fires before ours

    fd, cfg_path = tempfile.mkstemp(prefix="playtest-cfg-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    env = dict(os.environ, PLAYTEST_WATCH_STDIN="1")
    out_chunks = []
    reason = None
    try:
        proc = subprocess.Popen([node, str(SCRIPT), cfg_path], cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=env,
                                creationflags=subprocess.CREATE_NO_WINDOW if IS_WIN else 0)
        reader = threading.Thread(target=lambda: out_chunks.append(proc.stdout.read()), daemon=True)
        reader.start()
        while proc.poll() is None:
            if stop is not None and stop.is_set():
                reason = "stopped"
            elif time.time() - t0 > timeout:
                reason = f"timed out after {timeout}s"
            if reason:
                try:
                    proc.stdin.close()  # playtest.mjs sees EOF, closes Chrome and its profile, exits
                except Exception:
                    pass
                try:
                    proc.wait(15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(5)
                break
            time.sleep(0.2)
        reader.join(5)
        if reason or any(Path(tempfile.gettempdir()).glob(f"playtest-{tag}-*")):
            _kill_tagged_chrome(tag)
    except Exception as e:
        _kill_tagged_chrome(tag)
        return f"playtest error: {e}", "playtest: error"
    finally:
        try:
            os.unlink(cfg_path)
        except OSError:
            pass

    raw = out_chunks[0] if out_chunks else b""
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n").strip()
    secs = time.time() - t0
    if reason:
        text = (f"playtest {reason} (Chrome was closed)\n" + text).strip()
        return text, f"playtest {reason}"
    if not text:
        text = f"playtest error: no output (node exit {proc.returncode})"
    first = text.splitlines()[0]
    m = re.match(r"^(PASS|FAIL)\b", first)
    counts = text.splitlines()[1] if m and len(text.splitlines()) > 1 else ""
    summary = f"playtest {m.group(1)} in {secs:.0f}s: {counts}" if m else f"playtest: {first[:120]}"
    return text, summary


if __name__ == "__main__":
    import sys
    a = json.loads(open(sys.argv[1], encoding="utf-8").read()) if len(sys.argv) > 1 else {}
    rep, summ = run_playtest(a, os.getcwd())
    print(rep)
    print("---", summ)
