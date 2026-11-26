"""Ink's own web MCP server (stdio): web search and page reading, no accounts or API keys."""
import html
import json
import re
import sys
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"


def _get(url: str, timeout: int = 30, data: bytes | None = None) -> tuple[str, str]:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(5_000_000)
        charset = r.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, "replace"), r.geturl()


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def web_search(query: str, limit: int = 8) -> list[dict]:
    """[{title, url, snippet}] from Bing's RSS search feed; DuckDuckGo's HTML results if Bing returns nothing."""
    feed, _ = _get("https://www.bing.com/search?format=rss&count=20&q=" + urllib.parse.quote(query))
    results = []
    for item in re.findall(r"<item>(.*?)</item>", feed, re.S):
        get = lambda tag: (re.search(rf"<{tag}>(.*?)</{tag}>", item, re.S) or [None, ""])[1]
        results.append({"title": _text(get("title")), "url": html.unescape(get("link")), "snippet": _text(get("description"))})
        if len(results) >= limit:
            return results
    return results or _ddg(query, limit)


def _ddg(query: str, limit: int) -> list[dict]:
    page, _ = _get("https://html.duckduckgo.com/html/", data=urllib.parse.urlencode({"q": query}).encode())
    results = []
    for block in re.split(r'<div class="result results_links', page)[1:]:
        a = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not a:
            continue
        url = html.unescape(a.group(1))
        if "uddg=" in url:  # DuckDuckGo redirect link -> real target
            url = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("uddg", [url])[0])
        if "duckduckgo.com/y.js" in url:  # ads
            continue
        snip = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
        results.append({"title": _text(a.group(2)), "url": url, "snippet": _text(snip.group(1)) if snip else ""})
        if len(results) >= limit:
            break
    if not results and "anomaly" in page.lower():
        raise RuntimeError("no results: Bing returned none and DuckDuckGo is rate-limiting this IP right now")
    return results


def fetch_url(url: str, max_chars: int = 20000) -> dict:
    """Download a page and return {url, title, text} with scripts, styles and tags stripped."""
    page, final = _get(url)
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    body = re.sub(r"<!--.*?-->|<(script|style|noscript|svg|head|template)\b[^>]*>.*?</\1\s*>", " ", page, flags=re.S | re.I)
    body = re.sub(r"<(br|p|div|li|h[1-6]|tr|section|article|header|footer)\b[^>]*>", "\x00", body, flags=re.S | re.I)
    body = html.unescape(re.sub(r"<[^>]*>", " ", body, flags=re.S))  # tags may span lines: strip before splitting
    text = "\n".join(l for l in (" ".join(x.split()) for x in body.split("\x00")) if l)
    return {"url": final, "title": _text(title.group(1)) if title else "", "chars": len(text), "text": text[:max_chars]}


TOOLS = {
    "web_search": {
        "description": "Search the web (Bing, DuckDuckGo fallback). Returns up to `limit` results with title, url and snippet.",
        "fn": web_search,
        "schema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                   "required": ["query"]},
    },
    "fetch_url": {
        "description": "Download a web page and return its title and readable text (tags, scripts and styles removed).",
        "fn": fetch_url,
        "schema": {"type": "object", "properties": {"url": {"type": "string"}, "max_chars": {"type": "integer"}},
                   "required": ["url"]},
    },
}


def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                                        "serverInfo": {"name": "ink-web", "version": "1.0"}}}
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
                {"type": "text", "text": json.dumps(data)}]}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {"isError": True, "content": [
                {"type": "text", "text": f"{type(e).__name__}: {e}"}]}}
    if method and method.startswith("notifications/"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}


def main():
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


if __name__ == "__main__":
    main()
