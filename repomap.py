"""Repo map for ink: a compact table of contents of a codebase - files, their symbols and line numbers.

repo_map(root, path=".", query=None, budget=7000) -> (text, summary)

Without a query: files ranked by how many other files import them (the core modules first), each with its
top-level symbols. With a query: only symbols/files matching the words, best matches first.
For a single file: its full outline (every function/class/method/event with its line), so the model can
read_file just the range it needs instead of the whole file.
Stdlib only, regex based: fast (~0.2 s for a few hundred files), good enough for navigation.
"""
import os, re
from pathlib import Path

SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", ".cache", "target",
        "artifacts", "cache", "coverage", "typechain-types", "out", "logs", ".ink", "vendor", "third_party"}
EXTS = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".sol", ".py", ".gd", ".cs", ".lua", ".go", ".rs", ".html"}
MAX_FILES = 1500
MAX_BYTES = 600_000

JS = [
    (re.compile(r"^\s*export\s+default\s+(?:async\s+)?(?:function\*?|class)\s*([A-Za-z_$][\w$]*)?"), "default"),
    (re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\*?\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)?"), "fn"),
    (re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)(\s+extends\s+[\w$.]+)?"), "class"),
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\(([^)]*)\)|[A-Za-z_$][\w$]*)\s*=>"), "fn"),
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Z][A-Z0-9_]{2,})\s*="), "const"),
    (re.compile(r"^\s*export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*="), "const"),
    (re.compile(r"^\s*(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)"), "type"),
    # methods inside classes: 2-4 space indent, name(args) {  (not control keywords)
    (re.compile(r"^\s{2,4}(?:static\s+)?(?:async\s+)?(?:get\s+|set\s+)?(?!if\b|for\b|while\b|switch\b|catch\b|return\b|function\b)([A-Za-z_$#][\w$]*)\s*\(([^)]*)\)\s*\{"), "method"),
]
SOL = [
    (re.compile(r"^\s*(?:abstract\s+)?(contract|interface|library)\s+(\w+)([^{]*)"), "contract"),
    (re.compile(r"^\s*function\s+(\w+)\s*\(([^)]*)\)([^{;]*)"), "fn"),
    (re.compile(r"^\s*(constructor|receive|fallback)\s*\(([^)]*)\)([^{;]*)"), "fn"),
    (re.compile(r"^\s*modifier\s+(\w+)"), "modifier"),
    (re.compile(r"^\s*event\s+(\w+)\s*\(([^)]*)\)"), "event"),
    (re.compile(r"^\s*error\s+(\w+)\s*\(([^)]*)\)"), "error"),
    (re.compile(r"^\s*(?:struct|enum)\s+(\w+)"), "type"),
    (re.compile(r"^\s*mapping\s*\(.*\)\s*(?:public|private|internal)?\s*(\w+)\s*;"), "state"),
    (re.compile(r"^\s{4}(?:uint\d*|int\d*|address|bool|bytes\d*|string|I?[A-Z]\w*)(?:\[\])?\s+(?:public|private|internal|immutable|constant|\s)+(\w+)\s*[;=]"), "state"),
]
PY = [
    (re.compile(r"^(\s*)class\s+(\w+)"), "class"),
    (re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)?"), "fn"),
]
GD = [(re.compile(r"^\s*(?:static\s+)?func\s+(\w+)\s*\(([^)]*)\)"), "fn"),
      (re.compile(r"^\s*class_name\s+(\w+)"), "class"), (re.compile(r"^\s*signal\s+(\w+)"), "event")]
CS = [(re.compile(r"^\s*(?:public|private|protected|internal)?\s*(?:static\s+|abstract\s+|sealed\s+|partial\s+)*(class|struct|interface|enum)\s+(\w+)"), "class"),
      (re.compile(r"^\s*(?:public|private|protected|internal)\s+(?:static\s+|override\s+|virtual\s+|async\s+)*[\w<>\[\],]+\s+(\w+)\s*\(([^)]*)\)"), "fn")]
LUA = [(re.compile(r"^\s*(?:local\s+)?function\s+([\w.:]+)\s*\(([^)]*)\)"), "fn")]
GO = [(re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)\s*\(([^)]*)\)"), "fn"), (re.compile(r"^type\s+(\w+)\s+(struct|interface)"), "type")]
RS = [(re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)"), "fn"),
      (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait)\s+(\w+)"), "type")]
LANG = {".js": JS, ".mjs": JS, ".cjs": JS, ".jsx": JS, ".ts": JS, ".tsx": JS, ".html": JS, ".sol": SOL, ".py": PY,
        ".gd": GD, ".cs": CS, ".lua": LUA, ".go": GO, ".rs": RS}

IMPORT_RX = [
    re.compile(r"""(?:import|export)\s[^'"]*?from\s+['"]([^'"]+)['"]"""),
    re.compile(r"""import\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""^\s*import\s+(?:\{[^}]*\}\s+from\s+)?['"]([^'"]+)['"]""", re.M),  # solidity / side-effect js
    re.compile(r"""<script[^>]+src=['"]([^'"]+)['"]"""),
]
KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "function", "constructor", "super", "else", "do",
            "try", "new", "typeof", "await", "with"}


def _files(base):
    out = []
    for dp, dns, fns in os.walk(base):
        dns[:] = [d for d in dns if d not in SKIP and not d.startswith(".")]
        for fn in fns:
            p = Path(dp) / fn
            if p.suffix.lower() in EXTS and not fn.endswith((".min.js", ".map", ".bundle.js")):
                try:
                    if p.stat().st_size <= MAX_BYTES:
                        out.append(p)
                except OSError:
                    pass
            if len(out) >= MAX_FILES:
                return out
    return out


def _sig(args, n=60):
    a = " ".join((args or "").split())
    return a if len(a) <= n else a[: n - 1] + "…"


def outline(p, text=None):
    """[(line, kind, label, indent)] for one file."""
    ext = p.suffix.lower()
    rules = LANG.get(ext)
    if not rules:
        return []
    if text is None:
        try:
            text = p.read_text("utf-8", "replace")
        except Exception:
            return []
    syms = []
    in_script = ext != ".html"
    for i, line in enumerate(text.splitlines(), 1):
        if ext == ".html":
            if re.search(r"<script\b(?![^>]*\bsrc=)", line, re.I):
                in_script = True
            if re.search(r"</script>", line, re.I):
                in_script = False
            if not in_script:
                continue
        if len(line) > 300:
            continue
        for rx, kind in rules:
            m = rx.match(line)
            if not m:
                continue
            g = m.groups()
            if rules is PY:
                indent, name = len(g[0]), g[1]
                label = f"{'class ' if kind == 'class' else 'def '}{name}" + (f"({_sig(g[2])})" if kind == "fn" else "")
                syms.append((i, kind if indent == 0 else "method", label, indent))
            elif rules is SOL:
                if kind == "contract":
                    label = f"{g[0]} {g[1]}{(' ' + ' '.join(g[2].split())) if g[2].strip() else ''}"
                    syms.append((i, kind, label, 0))
                elif kind == "fn":
                    vis = " ".join(w for w in (g[2] or "").split() if w in ("external", "public", "internal", "private",
                                                                            "view", "pure", "payable") or w.startswith(("only", "nonReentrant", "returns")))
                    syms.append((i, kind, f"{g[0]}({_sig(g[1], 70)}) {vis}".rstrip(), 4))
                else:
                    syms.append((i, kind, f"{kind} {g[0]}" + (f"({_sig(g[1], 50)})" if len(g) > 1 and g[1] else ""), 4))
            else:
                name = g[0] if kind != "class" or rules is not CS else g[1]
                if kind == "class" and rules is CS:
                    name = g[1]
                if not name or name in KEYWORDS:
                    if kind != "default":
                        break
                    name = name or "(anonymous)"
                args = g[1] if len(g) > 1 and kind in ("fn", "method") else None
                if kind == "method" and len(line) - len(line.lstrip()) > 4:
                    break
                label = {"class": "class ", "type": "type ", "const": "", "default": "export default "}.get(kind, "")
                label += name + (f"({_sig(args)})" if kind in ("fn", "method") else "")
                if kind == "class" and len(g) > 1 and g[1] and rules is JS:
                    label += " " + " ".join(g[1].split())
                syms.append((i, kind, label, 4 if kind == "method" else 0))
            break
    return syms


def _imports(p, text, all_files_by_stem):
    hits = set()
    for rx in IMPORT_RX:
        for m in rx.finditer(text):
            spec = m.group(1)
            if not spec.startswith((".", "/")) and not spec.endswith(".sol"):
                continue
            stem = Path(spec.split("?")[0]).stem
            for cand in all_files_by_stem.get(stem, ()):
                if cand != p:
                    hits.add(cand)
    return hits


def repo_map(root, path=".", query=None, budget=7000):
    root = Path(root).resolve()
    base = (root / path).resolve() if path else root
    if base.is_file():
        syms = outline(base)
        n = len(base.read_text("utf-8", "replace").splitlines())
        rel = _rel(base, root)
        if not syms:
            return f"{rel} ({n} lines): no symbols found", "0 symbols"
        lines = [f"{rel} ({n} lines)"] + [f"{s[0]:>6}  {' ' * s[3]}{s[2]}" for s in syms]
        if query:
            words = [w.lower() for w in re.findall(r"\w+", query)]
            lines = lines[:1] + [l for l in lines[1:] if any(w in l.lower() for w in words)]
        text = "\n".join(lines)
        return _cap(text, budget * 2), f"{len(syms)} symbols"
    if not base.is_dir():
        return f"Not found: {path}", "not found"
    files = _files(base)
    texts, by_stem = {}, {}
    for f in files:
        try:
            texts[f] = f.read_text("utf-8", "replace")
        except Exception:
            continue
        by_stem.setdefault(f.stem, []).append(f)
    indeg = {f: 0 for f in texts}
    for f, t in texts.items():
        for g in _imports(f, t, by_stem):
            if g in indeg:
                indeg[g] += 1
    outlines = {f: outline(f, t) for f, t in texts.items()}
    words = [w.lower() for w in re.findall(r"\w{3,}", query or "")]

    def score(f):
        rel = _rel(f, root).lower()
        s = indeg[f] * 3 + min(len(outlines[f]), 20) / 10
        if any(seg in rel for seg in ("test", "spec", "probe", "mock", "example", "demo", "_old", "backup", "fixture")):
            s = s * 0.15 - 5  # test helpers are imported by every test: don't let them outrank the real code
        if words:
            name_hits = sum(w in rel for w in words)
            sym_hits = sum(any(w in lab.lower() for w in words) for _, _, lab, _ in outlines[f])
            s = name_hits * 20 + sym_hits * 6 + s / 5
        return s

    ranked = sorted(texts, key=score, reverse=True)
    if words:
        ranked = [f for f in ranked if score(f) >= 6]
    out_lines, used, shown = [], 0, 0
    for f in ranked:
        rel = _rel(f, root)
        syms = outlines[f]
        if words:
            syms = [s for s in syms if any(w in s[2].lower() for w in words)] or syms[:3]
        nl = texts[f].count("\n") + 1
        head = f"{rel} ({nl} lines" + (f", imported by {indeg[f]}" if indeg[f] else "") + ")"
        per = 14 if not words else 25
        body = [f"{s[0]:>6}  {' ' * s[3]}{s[2]}" for s in syms[:per]]
        if len(syms) > per:
            body.append(f"        … {len(syms) - per} more (map the file for its full outline)")
        block = "\n".join([head] + body)
        if used + len(block) > budget and shown:
            break
        out_lines.append(block)
        used += len(block) + 1
        shown += 1
    rest = len(ranked) - shown
    if rest > 0:
        others = ", ".join(_rel(f, root) for f in ranked[shown:shown + 40])
        out_lines.append(f"… {rest} more files: {others}" + (" …" if rest > 40 else ""))
    if not out_lines:
        return f"No files or symbols match '{query}'. Try other words, or grep.", "0 matches"
    return "\n".join(out_lines), f"{shown} of {len(texts)} files" + (f" matching '{query}'" if words else "")


def _rel(f, root):
    try:
        return str(Path(f).relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(f)


def _cap(s, n):
    return s if len(s) <= n else s[:n] + f"\n… [cut at {n} chars: pass a query to filter]"


if __name__ == "__main__":
    import sys, time
    t = time.time()
    txt, summ = repo_map(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ".", sys.argv[3] if len(sys.argv) > 3 else None)
    print(txt)
    print(f"--- {summ} in {time.time() - t:.2f}s, {len(txt)} chars")
