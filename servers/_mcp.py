"""Minimal MCP client: HTTP (Streamable HTTP/SSE) and stdio transports.

Server definitions come from Ink's own servers/mcp.json. Auth tokens are never stored:
HTTP headers are fetched at call time via the configured headersHelper.
"""
import atexit
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_CFG = Path(__file__).with_name("mcp.json")
UA = "ink/1.0"


def load_servers() -> dict:
    """Ink's own MCP servers: servers/mcp.json only."""
    servers = {}
    if LOCAL_CFG.exists():
        try:
            servers.update(json.loads(LOCAL_CFG.read_text("utf-8")).get("mcpServers") or {})
        except Exception:
            pass
    return servers


def _headers(cfg: dict) -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "User-Agent": UA}
    helper = cfg.get("headersHelper")
    if helper:
        out = subprocess.run(helper, shell=True, capture_output=True, text=True, timeout=60).stdout.strip()
        h.update(json.loads(out))
    for k, v in (cfg.get("headers") or {}).items():
        h[k] = v
    return h


def _parse(body: str, ctype: str):
    if not body.strip():
        return None
    if "text/event-stream" in ctype or body.lstrip().startswith("event:"):
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return None
    return json.loads(body)


def _post(url: str, payload: dict, headers: dict, timeout: int = 120):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return _parse(r.read().decode("utf-8", "replace"), r.headers.get("Content-Type", ""))


_http_ready = set()
_lock = threading.Lock()


def _http_rpc(cfg: dict, payload: dict, timeout: int = 120):
    return _post(cfg["url"], payload, _headers(cfg), timeout)


def _http_call(name: str, cfg: dict, tool: str, args: dict):
    with _lock:
        if name not in _http_ready:
            _http_rpc(cfg, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "ink-codemode", "version": "1.0"}}})
            _http_rpc(cfg, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            _http_ready.add(name)
    res = _http_rpc(cfg, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": tool, "arguments": args}})
    return _unwrap(res)


def _unwrap(res):
    if res is None:
        return None
    if "error" in res:
        raise RuntimeError(res["error"].get("message") or res["error"])
    result = res.get("result", res)
    if isinstance(result, dict) and result.get("isError"):
        text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        raise RuntimeError(text or "tool returned isError")
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        text = "".join(c.get("text", "") for c in content if c.get("type") == "text")
        try:
            return json.loads(text)
        except Exception:
            return text
    return result


class _Stdio:
    def __init__(self, name: str, cfg: dict):
        cmd = cfg.get("command", "python")
        if cmd in ("python", "python3", "py"):
            cmd = sys.executable
        cwd = cfg.get("cwd") or "."
        cwd = str((PROJECT_ROOT / cwd).resolve()) if not os.path.isabs(cwd) else cwd
        self.name, self.id, self.lock = name, 0, threading.Lock()
        self.p = subprocess.Popen([cmd, *[str(a) for a in cfg.get("args", [])]], cwd=cwd,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "ink-codemode", "version": "1.0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, payload: dict):
        self.p.stdin.write(json.dumps(payload) + "\n")
        self.p.stdin.flush()

    def request(self, method: str, params: dict):
        with self.lock:
            self.id += 1
            self._send({"jsonrpc": "2.0", "id": self.id, "method": method, "params": params})
            while True:
                line = self.p.stdout.readline()
                if not line:
                    raise RuntimeError(f"MCP server {self.name} closed the stream")
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") == self.id:
                    return msg

    def close(self):
        try:
            self.p.stdin.close()
            self.p.terminate()
        except Exception:
            pass


_stdio: dict = {}


def _stdio_session(name: str, cfg: dict) -> _Stdio:
    with _lock:
        if name not in _stdio:
            _stdio[name] = _Stdio(name, cfg)
        return _stdio[name]


def _stdio_call(name: str, cfg: dict, tool: str, args: dict):
    res = _stdio_session(name, cfg).request("tools/call", {"name": tool, "arguments": args})
    return _unwrap(res)


def _proxy_call(url: str, server: str, tool: str, args: dict):
    req = urllib.request.Request(url, data=json.dumps({"server": server, "tool": tool, "args": args}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        body = json.loads(r.read().decode("utf-8"))
    if body.get("error"):
        raise RuntimeError(body["error"])
    return body.get("result")


def call(server: str, tool: str, args: dict | None = None):
    """Call an MCP tool. Inside the code-mode sandbox this goes through the host's proxy, which maps
    [EMAIL_1]-style placeholders in args back to real values and masks personal data in the result."""
    args = {k: v for k, v in (args or {}).items() if v is not None}
    proxy = os.environ.get("INK_MCP_PROXY")
    if proxy:
        return _proxy_call(proxy, server, tool, args)
    cfg = load_servers().get(server)
    if not cfg:
        raise KeyError(f"unknown MCP server {server!r}; configured: {sorted(load_servers())}")
    if cfg.get("type") == "http" or cfg.get("url"):
        return _http_call(server, cfg, tool, args)
    return _stdio_call(server, cfg, tool, args)


def list_tools(server: str) -> list:
    cfg = load_servers().get(server)
    if not cfg:
        raise KeyError(f"unknown MCP server {server!r}")
    if cfg.get("type") == "http" or cfg.get("url"):
        with _lock:
            if server not in _http_ready:
                _http_rpc(cfg, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "ink-codemode", "version": "1.0"}}})
                _http_rpc(cfg, {"jsonrpc": "2.0", "method": "notifications/initialized"})
                _http_ready.add(server)
        res = _http_rpc(cfg, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    else:
        res = _stdio_session(server, cfg).request("tools/list", {})
    return (res.get("result") or {}).get("tools", [])


@atexit.register
def _cleanup():
    for s in _stdio.values():
        s.close()
