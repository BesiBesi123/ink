"""Host side of code mode: an MCP proxy on 127.0.0.1 plus run_code(), which runs model-written Python in the sandbox.

Only the proxy (in Ink's own process) talks to the real MCP servers. On the way in it maps [EMAIL_1]-style
placeholders back to real values; on the way out it masks personal data. Everything the script prints is
masked again and capped before the model sees it.
"""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from codemode.pii import Vault

APP = Path(__file__).resolve().parents[1]
RUN_DIR = APP / ".codemode" / "run"
OUT_CHARS = 6000

_vault = None
_proxy = None
_lock = threading.Lock()


def vault() -> Vault:
    global _vault
    if _vault is None:
        _vault = Vault()
    return _vault


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        from servers import _mcp
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            args = vault().unmask(req.get("args") or {})
            body = {"result": vault().mask(_mcp.call(req["server"], req["tool"], args))}
        except Exception as e:
            body = {"error": vault().mask_text(f"{type(e).__name__}: {e}")}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def proxy_url(project: str) -> str:
    global _proxy
    with _lock:
        if _proxy is None:
            if str(APP) not in sys.path:
                sys.path.insert(0, str(APP))
            os.environ.pop("INK_MCP_PROXY", None)
            os.environ["INK_PROJECT"] = str(project)  # stdio servers started by the proxy inherit it
            _proxy = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            threading.Thread(target=_proxy.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{_proxy.server_address[1]}/"


def run_code(code: str, project: str, timeout: int = 120, stop: threading.Event | None = None) -> tuple[str, str]:
    """Run Python in the sandbox with cwd = project. Returns (masked, capped output, short status)."""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    script = RUN_DIR / f"run_{int(time.time() * 1000)}.py"
    script.write_text(code, "utf-8")
    env = dict(os.environ, INK_MCP_PROXY=proxy_url(project), INK_PROJECT=str(project), PYTHONIOENCODING="utf-8",
               PYTHONUTF8="1")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen([sys.executable, "-B", "-X", "utf8", str(APP / "codemode" / "runner.py"), str(script)],
                            cwd=project, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, creationflags=flags)
    chunks = []
    reader = threading.Thread(target=lambda: chunks.append(proc.stdout.read()), daemon=True)
    reader.start()
    t0, killed = time.time(), None
    while reader.is_alive():
        reader.join(0.1)
        if time.time() - t0 > timeout:
            killed = f"timed out after {timeout}s"
        elif stop is not None and stop.is_set():
            killed = "interrupted by user"
        if killed:
            if os.name == "nt":  # the script may have started programs of its own: kill the whole tree
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
            else:
                proc.kill()
            reader.join(3)
            break
    code_ = proc.wait(5) if not killed else None
    script.unlink(missing_ok=True)
    text = (chunks[0] if chunks else b"").decode("utf-8", "replace").replace("\r\n", "\n").rstrip()
    text = text.replace(f'File "{script}"', 'File "<code>"')
    text = vault().mask_text(text)
    if len(text) > OUT_CHARS:
        text = text[: OUT_CHARS // 3] + f"\n… [{len(text) - OUT_CHARS} chars cut: filter in code, print less] …\n" + \
               text[-(OUT_CHARS - OUT_CHARS // 3):]
    if killed:
        return (text + f"\n[{killed}]").strip(), killed
    if code_:
        return (text or "(no output)") + f"\n[exit code {code_}]", f"exit {code_}"
    return text or "(no output - print() the result you need)", "ok"
