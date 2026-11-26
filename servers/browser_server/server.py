"""Ink's browser MCP server (stdio): a real Chrome (via Playwright) that runs JavaScript, so it works on modern
web apps where fetch_url only sees an empty shell. One page stays open across calls.

Uses the installed Chrome (or Edge) with its own profile in ~/.ink/browser - never your personal profile.
Set INK_BROWSER_SHOW=1 to watch it in a window instead of headless.
"""
import json
import os
import sys
from pathlib import Path

PROFILE = Path.home() / ".ink" / "browser"
_pw = _ctx = _page = None


def page():
    global _pw, _ctx, _page
    if _page is None or _page.is_closed():
        if _ctx is None:
            from playwright.sync_api import sync_playwright
            _pw = sync_playwright().start()
            headless = os.environ.get("INK_BROWSER_SHOW") != "1"
            last = None
            for channel in ("chrome", "msedge"):
                try:
                    _ctx = _pw.chromium.launch_persistent_context(str(PROFILE), channel=channel, headless=headless,
                                                                  viewport={"width": 1366, "height": 900})
                    break
                except Exception as e:
                    last = e
            if _ctx is None:
                raise RuntimeError(f"could not start Chrome or Edge: {last}")
        _page = _ctx.pages[0] if _ctx.pages else _ctx.new_page()
        _page.set_default_timeout(20000)
    return _page


def _summary(max_chars=4000):
    p = page()
    try:
        p.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    text = p.inner_text("body") if p.query_selector("body") else ""
    text = "\n".join(l.strip() for l in text.splitlines() if l.strip())
    return {"url": p.url, "title": p.title(), "chars": len(text), "text": text[:max_chars]}


def _loc(target: str):
    """CSS selector if it looks like one, else the first element whose visible text matches."""
    p = page()
    if target.startswith(("#", ".", "[", "//", "css=", "xpath=", "text=", "role=")) or any(c in target for c in ">[]=:"):
        return p.locator(target).first
    for loc in (p.get_by_role("button", name=target), p.get_by_role("link", name=target),
                p.get_by_label(target), p.get_by_placeholder(target), p.get_by_text(target)):
        if loc.count():
            return loc.first
    raise RuntimeError(f"nothing on the page matches {target!r}")


def open_url(url: str, max_chars: int = 4000) -> dict:
    """Go to url and return {url, title, text}."""
    if "://" not in url:
        url = "https://" + url
    page().goto(url, wait_until="domcontentloaded")
    return _summary(max_chars)


def click(target: str, max_chars: int = 2000) -> dict:
    """Click a button/link/element (CSS selector or its visible text); returns the page afterwards."""
    _loc(target).click()
    return _summary(max_chars)


def fill(target: str, text: str, submit: bool = False, max_chars: int = 2000) -> dict:
    """Type text into an input (CSS selector, label, placeholder or text); submit=True presses Enter."""
    loc = _loc(target)
    loc.fill(text)
    if submit:
        loc.press("Enter")
    return _summary(max_chars)


def read(selector: str | None = None, max_chars: int = 8000) -> dict:
    """Readable text of the current page, or of one element."""
    if not selector:
        return _summary(max_chars)
    t = _loc(selector).inner_text()
    return {"url": page().url, "chars": len(t), "text": t[:max_chars]}


def links(contains: str = "", limit: int = 50) -> list[dict]:
    """Links on the page as [{text, href}], optionally only those whose text or href contains `contains`."""
    rows = page().eval_on_selector_all("a[href]", "els => els.map(a => ({text: a.innerText.trim(), href: a.href}))")
    c = contains.lower()
    return [r for r in rows if not c or c in r["text"].lower() or c in r["href"].lower()][:limit]


def screenshot(path: str = "screenshot.png", full_page: bool = False) -> dict:
    """Save a PNG of the page (path relative to the project)."""
    p = Path(os.environ.get("INK_PROJECT") or ".") / path
    p.parent.mkdir(parents=True, exist_ok=True)
    page().screenshot(path=str(p), full_page=full_page)
    return {"saved": str(p)}


def run_js(script: str):
    """Evaluate a JavaScript expression or function in the page and return its (JSON) result."""
    return page().evaluate(script)


def back(max_chars: int = 2000) -> dict:
    """Go back one page; returns the page afterwards."""
    page().go_back()
    return _summary(max_chars)


S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}


def _t(fn, props, req, name=None):
    return name or fn.__name__, {"fn": fn, "description": fn.__doc__.strip().splitlines()[0],
                                 "schema": {"type": "object", "properties": props, "required": req}}


TOOLS = dict([
    _t(open_url, {"url": S, "max_chars": I}, ["url"], "open"),
    _t(click, {"target": S, "max_chars": I}, ["target"]),
    _t(fill, {"target": S, "text": S, "submit": B, "max_chars": I}, ["target", "text"]),
    _t(read, {"selector": S, "max_chars": I}, []),
    _t(links, {"contains": S, "limit": I}, []),
    _t(screenshot, {"path": S, "full_page": B}, []),
    _t(run_js, {"script": S}, ["script"]),
    _t(back, {"max_chars": I}, []),
])


def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                                        "serverInfo": {"name": "ink-browser", "version": "1.0"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": n, "description": t["description"], "inputSchema": t["schema"]} for n, t in TOOLS.items()]}}
    if method == "tools/call":
        params = msg.get("params") or {}
        t = TOOLS.get(params.get("name"))
        if not t:
            return {"jsonrpc": "2.0", "id": mid, "result": {"isError": True, "content": [
                {"type": "text", "text": f"unknown tool {params.get('name')}"}]}}
        try:
            data = t["fn"](**(params.get("arguments") or {}))
            return {"jsonrpc": "2.0", "id": mid, "result": {"isError": False, "content": [
                {"type": "text", "text": json.dumps(data, default=str)}]}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {"isError": True, "content": [
                {"type": "text", "text": f"{type(e).__name__}: {str(e).splitlines()[0][:400]}"}]}}
    if method and method.startswith("notifications/"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}


def main():
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                resp = handle(json.loads(line))
            except Exception as e:
                resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}}
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
    finally:
        try:
            _ctx and _ctx.close()
            _pw and _pw.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
