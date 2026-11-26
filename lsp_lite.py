"""LSP-lite for ink: fast semantic diagnostics that a syntax check misses, run after every edit.

diagnose(path, root=None, text=None) -> list[str]
    Short problems like  "src/a.js:12: 'spawnEnemy' is not exported by ./enemies.js (exports: spawn, Enemy)".
    Empty list when fine. Never raises (internal errors -> []). At most MAX_PROBLEMS entries.

JS/TS (.js .mjs .cjs .jsx .ts .tsx, inline <script type=module> in .html):
  - relative imports / export-from / import('lit') / require('lit') that don't resolve to a file
  - named / default imports the target module doesn't export (follows `export * from` chains)
  - calls `name(` to a function that is declared nowhere in the file, not imported and not a known global
  - <Component> in JSX that is declared nowhere and not imported
  - duplicate top-level function/class/const/let declarations in one module
  - (with root) names other files import from this file that it no longer exports
Solidity (.sol):
  - relative imports that don't resolve
  - emit of undeclared events, revert of undeclared custom errors, argument-count mismatch for both
  - undeclared modifiers, internal calls / this.f() to functions that don't exist in the contract or its bases
  Checks needing a base contract or import that can't be resolved are skipped for that contract.

Precision over recall: a false positive makes a small model "fix" correct code, so every check bails out
("unknown") as soon as anything it depends on can't be parsed with confidence.
Stdlib only, regex + a small lexer that blanks comments/strings/templates/regex literals/JSX text.
Parsed files are cached by (path, mtime, size).
"""
import difflib, os, re, time
from bisect import bisect_right
from collections import Counter
from pathlib import Path

MAX_PROBLEMS = 8
MAX_BYTES = 1_500_000
CALL_CHECK_MAX_LINES = 3000
INDEX_MAX_FILES = 400
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", ".cache", "target",
             "artifacts", "cache", "coverage", "typechain-types", "out", "logs", ".ink", "vendor", "third_party",
             "bower_components", "jspm_packages", ".svelte-kit", ".nuxt", ".output", ".turbo", ".parcel-cache"}
JS_EXTS = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts")
RESOLVE_EXTS = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts", ".d.ts", ".json")
NONCODE_EXTS = (".css", ".scss", ".sass", ".less", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif",
                ".ico", ".json", ".wasm", ".glsl", ".vert", ".frag", ".txt", ".md", ".html", ".vue", ".svelte",
                ".woff", ".woff2", ".ttf", ".mp3", ".wav", ".ogg", ".mp4", ".webm", ".csv", ".yaml", ".yml", ".astro")

_NONNL = re.compile(r"[^\n]")
ID = r"[A-Za-z_$][\w$]*"

# ------------------------------------------------------------------------------------------- known globals
JS_GLOBALS = set("""
undefined NaN Infinity globalThis window self document navigator location history screen console performance
localStorage sessionStorage indexedDB crypto caches visualViewport frames parent top opener customElements
alert confirm prompt open close print stop focus blur scroll scrollTo scrollBy moveTo moveBy resizeTo resizeBy
getComputedStyle getSelection matchMedia requestAnimationFrame cancelAnimationFrame requestIdleCallback
cancelIdleCallback setTimeout setInterval clearTimeout clearInterval setImmediate clearImmediate queueMicrotask
structuredClone fetch atob btoa reportError createImageBitmap postMessage importScripts dispatchEvent
addEventListener removeEventListener find showOpenFilePicker showSaveFilePicker showDirectoryPicker
parseInt parseFloat isNaN isFinite encodeURI encodeURIComponent decodeURI decodeURIComponent escape unescape eval
Number String Boolean Symbol BigInt Object Array Date RegExp Error TypeError RangeError SyntaxError ReferenceError
EvalError URIError AggregateError Function Promise Proxy Reflect Map Set WeakMap WeakSet WeakRef JSON Math Intl
ArrayBuffer SharedArrayBuffer DataView Atomics Int8Array Uint8Array Uint8ClampedArray Int16Array Uint16Array
Int32Array Uint32Array Float32Array Float64Array BigInt64Array BigUint64Array FinalizationRegistry Iterator
require module exports __dirname __filename process Buffer global gc
describe it test suite before after beforeEach afterEach beforeAll afterAll expect jest vi context specify xit xdescribe
fit fdescribe assert chai sinon cy Cypress
gtag dataLayer ga fbq jQuery $ define importmap
""".split())
JS_KEYWORDS = set("""
if for while switch catch return function constructor super else do try new typeof await with delete void in of
instanceof yield async import export default case throw class extends static get set let const var this
debugger finally break continue enum implements interface package private protected public declare
keyof infer asserts is satisfies as type namespace module abstract readonly unique never unknown any
true false null
""".split())

SOL_BUILTINS = set("""
require assert revert keccak256 sha256 sha3 ripemd160 ecrecover addmod mulmod blockhash blobhash gasleft selfdestruct
suicide type payable address bool string bytes byte log0 log1 log2 log3 log4 this super now
""".split())
SOL_KEYWORDS = set("""
function modifier event error emit revert new returns return mapping is contract interface library constructor
fallback receive if for while do catch try else struct enum using assembly unchecked memory storage calldata
public private internal external view pure payable virtual override constant immutable anonymous indexed
abstract delete pragma import from as
""".split())
SOL_ELEMENTARY = re.compile(r"^(u?int\d*|bytes\d*|u?fixed[\dx]*|address|bool|string|bytes|byte)$")
SOL_HEADER_WORDS = {"public", "private", "internal", "external", "view", "pure", "payable", "virtual", "override",
                    "returns", "constant", "nonpayable", "anonymous"}


# ------------------------------------------------------------------------------------------- lexer
_RX_JS = re.compile(r"//|/\*|['\"`/{}]")
_RX_JSX = re.compile(r"//|/\*|['\"`/{}<]")
_RX_TMPL = re.compile(r"\\.|`|\$\{", re.S)
_RX_STR = {"'": re.compile(r"'(?:\\.|[^'\\\n])*'", re.S), '"': re.compile(r'"(?:\\.|[^"\\\n])*"', re.S)}
_RX_REGEX = re.compile(r"/(?:\\.|\[(?:\\.|[^\]\\\n])*\]|[^/\\\[\n])+/[A-Za-z]*")
_RX_JSXTAG = re.compile(r"[\"'{>]|/>")
_RX_JSXKIDS = re.compile(r"[<{]")
_REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_KW = {"return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void", "throw", "yield",
             "await", "instanceof"}
_JSX_PREV_KW = {"return", "yield", "default", "case", "else", "do", "await"}


def _prev_sig(text, j):
    k = j - 1
    while k >= 0 and text[k] in " \t\r\n":
        k -= 1
    return k


def _prev_word(text, k):
    e = k + 1
    while k >= 0 and (text[k].isalnum() or text[k] in "_$"):
        k -= 1
    return text[k + 1:e]


def _regex_ok(text, j):
    k = _prev_sig(text, j)
    if k < 0:
        return True
    c = text[k]
    if c in _REGEX_PREV:
        return True
    if c.isalnum() or c in "_$":
        return _prev_word(text, k) in _REGEX_KW
    return False


def _jsx_start(text, j):
    nxt = text[j + 1:j + 2]
    if not (nxt.isalpha() or nxt == ">"):
        return None
    k = _prev_sig(text, j)
    if k >= 0:
        c = text[k]
        if c.isalnum() or c in "_$":
            if _prev_word(text, k) not in _JSX_PREV_KW:
                return None
        elif c not in "(,=:[!&|?{};>}" or (c == ">" and text[k - 1:k] != "="):
            return None
    m = re.compile(r"[A-Za-z][\w$.:-]*").match(text, j + 1)
    if m:
        after = text[m.end():m.end() + 30].lstrip()
        if after.startswith(",") or after.startswith("extends "):
            return None  # TS generic <T,>(x) => ...
        return m
    return "frag"


def mask_js(text, jsx=False):
    """Blank comments, string/template contents, regex literals and JSX text (newlines kept, length kept).
    Returns (masked, jsx_tags) - jsx_tags: [(name, pos)] of element names in JSX."""
    n = len(text)
    out, em = [], 0
    tags = []

    def blank(a, b):
        nonlocal em
        if a > em:
            out.append(text[em:a])
        if b > a:
            out.append(_NONNL.sub(" ", text[a:b]))
        em = max(em, b)

    if text.startswith("#!"):
        e = text.find("\n")
        blank(0, n if e < 0 else e)
    rx = _RX_JSX if jsx else _RX_JS
    stack = [["code", 0]]
    i = em
    while i < n:
        top = stack[-1]
        mode = top[0]
        if mode == "code":
            m = rx.search(text, i)
            if not m:
                break
            j, tok = m.start(), m.group()
            if tok == "//":
                e = text.find("\n", j)
                e = n if e < 0 else e
                blank(j, e)
                i = e
            elif tok == "/*":
                e = text.find("*/", j + 2)
                e = n if e < 0 else e + 2
                blank(j, e)
                i = e
            elif tok in "'\"":
                sm = _RX_STR[tok].match(text, j)
                e = sm.end() if sm else (text.find("\n", j) if text.find("\n", j) >= 0 else n)
                blank(j + 1, e - 1 if sm else e)
                i = e
            elif tok == "`":
                stack.append(["tmpl"])
                i = j + 1
            elif tok == "/":
                if _regex_ok(text, j):
                    rm = _RX_REGEX.match(text, j)
                    if rm:
                        blank(j + 1, rm.end() - 1)
                        i = rm.end()
                        continue
                i = j + 1
            elif tok == "{":
                top[1] += 1
                i = j + 1
            elif tok == "}":
                if top[1] == 0 and len(stack) > 1:
                    stack.pop()
                else:
                    top[1] = max(0, top[1] - 1)
                i = j + 1
            elif tok == "<":
                st = _jsx_start(text, j)
                if st is None:
                    i = j + 1
                elif st == "frag":
                    stack.append(["kids"])
                    i = j + 2
                else:
                    nm = st.group()
                    if nm[:1].isupper() and "." not in nm:
                        tags.append((nm, st.start()))
                    stack.append(["tag"])
                    i = st.end()
        elif mode == "tmpl":
            k = i
            while True:
                m = _RX_TMPL.search(text, k)
                if not m:
                    blank(i, n)
                    i = n
                    break
                if m.group() == "`":
                    blank(i, m.start())
                    stack.pop()
                    i = m.end()
                    break
                if m.group() == "${":
                    blank(i, m.start())
                    stack.append(["code", 0])
                    i = m.end()
                    break
                k = m.end()
        elif mode == "tag":
            m = _RX_JSXTAG.search(text, i)
            if not m:
                break
            tok, j = m.group(), m.start()
            if tok in "'\"":
                e = text.find(tok, j + 1)
                e = n if e < 0 else e
                blank(j + 1, e)
                i = e + 1
            elif tok == "{":
                stack.append(["code", 0])
                i = j + 1
            elif tok == "/>":
                stack.pop()
                i = j + 2
            else:  # '>'
                stack[-1] = ["kids"]
                i = j + 1
        elif mode == "kids":
            m = _RX_JSXKIDS.search(text, i)
            if not m:
                blank(i, n)
                break
            j = m.start()
            blank(i, j)
            if m.group() == "{":
                stack.append(["code", 0])
                i = j + 1
            elif text.startswith("</", j):
                e = text.find(">", j)
                e = n if e < 0 else e
                nm = re.match(r"\s*([A-Za-z][\w$]*)", text[j + 2:e])
                if nm and nm.group(1)[:1].isupper():
                    tags.append((nm.group(1), j + 2 + nm.start(1)))
                stack.pop()
                i = e + 1
            else:
                nm = re.compile(r"[A-Za-z][\w$.:-]*").match(text, j + 1)
                if nm:
                    if nm.group()[:1].isupper() and "." not in nm.group():
                        tags.append((nm.group(), nm.start()))
                    stack.append(["tag"])
                    i = nm.end()
                elif text.startswith("<>", j):
                    stack.append(["kids"])
                    i = j + 2
                else:
                    i = j + 1
    blank(n, n)
    if em < n:
        out.append(text[em:])
    return "".join(out), tags


_RX_SOL_MASK = re.compile(r"//[^\n]*|/\*.*?(?:\*/|$)|\"(?:\\.|[^\"\\\n])*\"?|'(?:\\.|[^'\\\n])*'?", re.S)


def mask_sol(text):
    def rep(m):
        s = m.group()
        if s[0] in "\"'":
            return s[0] + _NONNL.sub(" ", s[1:-1]) + s[-1] if len(s) > 1 else " "
        return _NONNL.sub(" ", s)
    return _RX_SOL_MASK.sub(rep, text)


class _Lines:
    def __init__(self, text):
        self.nl = [m.start() for m in re.finditer("\n", text)]

    def line(self, pos):
        return bisect_right(self.nl, pos - 1) + 1


def _match_close(code, i, open_ch="(", close_ch=")"):
    """i = index of open_ch. Returns index of the matching close (or -1)."""
    depth = 0
    for k in range(i, min(len(code), i + 200000)):
        c = code[k]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return k
    return -1


_RX_BR = re.compile(r"[{}()\[\]]")


def _depth_index(code):
    """(positions, depths) - depth just after each bracket; depth_at(p) via bisect."""
    pos, dep, d = [], [], 0
    for m in _RX_BR.finditer(code):
        c = m.group()
        d += 1 if c in "{([" else -1
        pos.append(m.start())
        dep.append(d)
    return pos, dep


def _depth_at(idx, p):
    pos, dep = idx
    k = bisect_right(pos, p - 1) - 1
    return dep[k] if k >= 0 else 0


def _split_top(s):
    """Split on commas at bracket depth 0."""
    parts, depth, cur = [], 0, []
    for c in s:
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    parts.append("".join(cur))
    return parts


# ------------------------------------------------------------------------------------------- caches
_cache = {}  # (kind, path) -> (stamp, value)


def _stamp(p):
    try:
        st = os.stat(p)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _read(p):
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return None


def _cached(kind, p, fn):
    p = os.path.normcase(os.path.abspath(p))
    st = _stamp(p)
    if st is None:
        return None
    hit = _cache.get((kind, p))
    if hit and hit[0] == st:
        return hit[1]
    if st[1] > MAX_BYTES:
        val = None
    else:
        text = _read(p)
        val = fn(p, text) if text is not None else None
    if len(_cache) > 5000:
        _cache.clear()
    _cache[(kind, p)] = (st, val)
    return val


_isfile_cache = {}


def _isfile(p):
    now = time.monotonic()
    hit = _isfile_cache.get(p)
    if hit and now - hit[1] < 2.0:
        return hit[0]
    r = os.path.isfile(p)
    if len(_isfile_cache) > 20000:
        _isfile_cache.clear()
    _isfile_cache[p] = (r, now)
    return r


def _isdir(p):
    try:
        return os.path.isdir(p)
    except Exception:
        return False


# ------------------------------------------------------------------------------------------- JS parsing
def _is_jsx_file(p, text):
    ext = os.path.splitext(p)[1].lower()
    if ext in (".jsx", ".tsx"):
        return True
    if ext in (".js", ".mjs", ".cjs"):
        return bool(re.search(r"from\s+['\"]react['\"]|(?:return|=>)\s*\(?\s*<[A-Za-z>]", text))
    return False


_RX_IMPORT = re.compile(r"import(?<![\w$.]import)\s+(?:(type)\s+(?=[\w${*]))?([^;'\"`]*?)\s*\bfrom\s*(['\"])")
_RX_IMPORT_SIDE = re.compile(r"import(?<![\w$.]import)\s*(['\"])")
_RX_EXPORT_FROM = re.compile(r"export(?<![\w$.]export)\s+(?:(type)\s+)?(\*(?:\s*as\s+(" + ID + r"))?|\{[^}]*\})\s*from\s*(['\"])")
_RX_DYN = re.compile(r"import(?<![\w$.]import)\s*\(\s*(['\"])")
_RX_REQ = re.compile(r"require(?<![\w$.]require)\s*\(\s*(['\"])")
_RX_CLAUSE = re.compile(r"^(?:(" + ID + r")\s*(?:,\s*|$))?(?:\{([^}]*)\}|\*\s*as\s+(" + ID + r"))?$", re.S)


def _string_at(text, masked, q_pos):
    """Original contents of the string literal whose opening quote is at q_pos (in masked == same offsets)."""
    q = masked[q_pos]
    e = masked.find(q, q_pos + 1)
    if e < 0:
        return None, q_pos
    return text[q_pos + 1:e], e


def _parse_specifiers(body, line):
    """'a, b as c, type d, default as e' -> [(imported, local)]; None if something odd."""
    res = []
    for part in body.split(","):
        part = " ".join(part.split())
        if not part:
            continue
        if part.startswith("type "):
            part = part[5:]
        m = re.fullmatch(r"(" + ID + r")(?:\s+as\s+(" + ID + r"))?", part)
        if not m:
            if re.fullmatch(r"['\"].*['\"](\s+as\s+" + ID + ")?", part) or re.fullmatch(r"\s*", part):
                continue  # string-named import: rare, skip
            return None
        res.append((m.group(1), m.group(2) or m.group(1)))
    return res


def _parse_js(path, text, jsx=None):
    """Everything we need about a JS/TS module, computed once per (path, mtime)."""
    if jsx is None:
        jsx = _is_jsx_file(path, text)
    masked, tags = mask_js(text, jsx)
    return _analyze_js(path, text, masked, tags)


def _analyze_js(path, text, masked, tags):
    L = _Lines(masked)
    imports = []  # dicts: spec, line, pos, kind, names [(imported, local, line)], default (local) , type_only
    for m in _RX_IMPORT.finditer(masked):
        clause = m.group(2).strip()
        spec, _ = _string_at(text, masked, m.start(3))
        if spec is None:
            continue
        cm = _RX_CLAUSE.match(clause)
        names, ok = [], True
        if not cm or not clause:
            ok = False
        else:
            if cm.group(2) is not None:
                names = _parse_specifiers(cm.group(2), 0)
                if names is None:
                    ok, names = False, []
        ln = L.line(m.start())
        # each named specifier's own line (multi-line import lists)
        named = []
        if ok and cm.group(2) is not None:
            base = m.start(2) + m.group(2).find("{")
            for imp, loc in names:
                mm = re.search(r"(?<![\w$])" + re.escape(imp) + r"(?![\w$])", masked[base:m.end()])
                named.append((imp, loc, L.line(base + mm.start()) if mm else ln))
        imports.append(dict(spec=spec, line=ln, pos=m.start(), kind="import", names=named,
                            default=cm.group(1) if ok and cm else None, ns=bool(ok and cm and cm.group(3)),
                            type_only=bool(m.group(1)), parsed=ok))
    for m in _RX_IMPORT_SIDE.finditer(masked):
        spec, _ = _string_at(text, masked, m.start(1))
        if spec is not None:
            imports.append(dict(spec=spec, line=L.line(m.start()), pos=m.start(), kind="side", names=[],
                                default=None, ns=False, type_only=False, parsed=True))
    exp_from = []
    for m in _RX_EXPORT_FROM.finditer(masked):
        spec, _ = _string_at(text, masked, m.start(4))
        if spec is None:
            continue
        what = m.group(2)
        ln = L.line(m.start())
        names, star, star_as = [], False, m.group(3)
        if what.startswith("{"):
            sp = _parse_specifiers(what[1:-1], ln)
            names = [(a, b, ln) for a, b in sp] if sp is not None else None
        else:
            star = star_as is None
        exp_from.append((spec, names, star, star_as))
        imports.append(dict(spec=spec, line=ln, pos=m.start(), kind="export-from", names=names or [],
                            default=None, ns=bool(star_as), type_only=bool(m.group(1)), parsed=names is not None,
                            star=star))
    for rx, kind in ((_RX_DYN, "dynamic"), (_RX_REQ, "require")):
        for m in rx.finditer(masked):
            spec, e = _string_at(text, masked, m.start(1))
            if spec is None or not re.match(r"\s*[),]", masked[e + 1:e + 20]):
                continue
            imports.append(dict(spec=spec, line=L.line(m.start()), pos=m.start(), kind=kind, names=[],
                                default=None, ns=True, type_only=False, parsed=True))

    # ---- exports
    names, fuzzy = set(), False
    has_default = False
    spans = []
    for m in re.finditer(r"export(?<![\w$.]export)\s+default\b", masked):
        has_default = True
    for m in re.finditer(r"export(?<![\w$.]export)\s+(?:declare\s+)?(?:async\s+)?function\s*\*?\s*(" + ID + ")", masked):
        names.add(m.group(1))
        spans.append(m.group())
    for m in re.finditer(r"export(?<![\w$.]export)\s+(?:declare\s+)?(?:abstract\s+)?class\s+(" + ID + ")", masked):
        names.add(m.group(1))
        spans.append(m.group())
    for m in re.finditer(r"export(?<![\w$.]export)\s+(?:declare\s+)?(?:const\s+)?(?:enum|interface|type|namespace|module)\s+(" + ID + ")", masked):
        names.add(m.group(1))
        spans.append(m.group())
    for m in re.finditer(r"export(?<![\w$.]export)\s+import\s+(" + ID + r")\s*=", masked):
        names.add(m.group(1))
    for m in re.finditer(r"export(?<![\w$.]export)\s+(?:declare\s+)?(?:const|let|var|using)\s+", masked):
        got, ok, span = _declarators(masked, m.end())
        names.update(got)
        fuzzy |= not ok
        spans.append(" ".join(sorted(got)) if ok else span)  # unsure parse -> keep the whole text (permissive)
    for m in re.finditer(r"export(?<![\w$.]export)\s+(?:type\s+)?\{([^}]*)\}(?!\s*from\b)", masked):
        sp = _parse_specifiers(m.group(1), 0)
        if sp is None:
            spans.append(m.group())
            fuzzy = True
            continue
        spans.append(" ".join(b for a, b in sp))  # only the exported (outer) names
        for a, b in sp:
            if b == "default":
                has_default = True
            else:
                names.add(b)
    stars = []
    for spec, nm, star, star_as in exp_from:
        if star_as:
            names.add(star_as)
        elif star:
            stars.append(spec)
        elif nm is None:
            fuzzy = True
        else:
            for a, b, _ in nm:
                if b == "default":
                    has_default = True
                else:
                    names.add(b)
    has_esm = bool(re.search(r"export(?<![\w$.]export)[\s{]|import(?<![\w$.]import)\s*[\w${*'\"]", masked))
    is_cjs = bool(re.search(r"exports(?<![\w$]exports)(?:\.[\w$]+\s*=|\[)|module\.exports|export(?<![\w$.]export)\s*=", masked))
    unknown = (not has_esm) or is_cjs
    return dict(path=path, masked=masked, tags=tags, imports=imports, exports=names, has_default=has_default,
                stars=stars, unknown=unknown, fuzzy=fuzzy, spans="\n".join(spans), has_esm=has_esm, is_cjs=is_cjs,
                lines=L)


def _declarators(code, i):
    """Parse `a = ..., {b, c: d} = ..., e` after export const. Returns (names, confident, span_text)."""
    names, ok = set(), True
    n = len(code)
    start = i
    for _ in range(50):
        while i < n and code[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        c = code[i]
        if c in "{[":
            e = _match_close(code, i)
            if e < 0:
                return names, False, code[start:start + 400]
            got = _pattern_names(code[i + 1:e], c)
            if got is None:
                ok = False
            else:
                names.update(got)
            i = e + 1
        else:
            m = re.compile(ID).match(code, i)
            if not m:
                return names, False, code[start:i + 200]
            names.add(m.group())
            i = m.end()
        # optional TS type annotation / initializer, until , ; or end of statement at depth 0
        depth = 0
        while i < n:
            c = code[i]
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
                if depth < 0:
                    return names, ok, code[start:i]
            elif depth == 0 and c == ";":
                return names, ok, code[start:i]
            elif depth == 0 and c == ",":
                break
            elif depth == 0 and c == "\n":
                prev = code[start:i].rstrip()[-1:]
                nxt = code[i + 1:i + 200].lstrip()[:1]
                if prev and prev not in "=,+-*/%&|^<>?:([{!~." and nxt and nxt not in ".,?:+-*/%&|^=<>([`":
                    return names, ok, code[start:i]
            i += 1
        if i >= n:
            break
        i += 1  # past the comma
    return names, ok, code[start:i]


def _pattern_names(body, kind):
    out = set()
    for part in _split_top(body):
        part = part.strip()
        if not part:
            continue
        if part.startswith("..."):
            part = part[3:].strip()
        if "=" in part and not re.search(r"[{\[]", part.split("=", 1)[0]):
            part = part.split("=", 1)[0].strip()
        if kind == "{" and ":" in part:
            part = part.split(":", 1)[1].strip()
            if "=" in part:
                part = part.split("=", 1)[0].strip()
        if part[:1] in "{[":
            e = _match_close(part, 0)
            sub = _pattern_names(part[1:e], part[0]) if e > 0 else None
            if sub is None:
                return None
            out.update(sub)
            continue
        if re.fullmatch(ID, part):
            out.add(part)
        else:
            return None
    return out


def _js_info(p):
    return _cached("js", p, _parse_js)


# ------------------------------------------------------------------------------------------- resolution
def _strip_query(spec):
    return re.split(r"[?#]", spec, 1)[0]


def _resolve(from_dir, spec):
    """Resolve a relative specifier to an existing file path, or None. Returns ('dir', path) for package dirs."""
    s = _strip_query(spec)
    if not s:
        return from_dir
    base = os.path.normpath(os.path.join(from_dir, s))
    if _isfile(base):
        return base
    for ext in RESOLVE_EXTS:
        if _isfile(base + ext):
            return base + ext
    root, ext = os.path.splitext(base)
    if ext.lower() in (".js", ".jsx", ".mjs", ".cjs"):  # TS NodeNext: import './a.js' -> a.ts
        for e2 in (".ts", ".tsx", ".mts", ".cts", ".d.ts"):
            if _isfile(root + e2):
                return root + e2
    if _isdir(base):
        for ext in RESOLVE_EXTS:
            if _isfile(os.path.join(base, "index" + ext)):
                return os.path.join(base, "index" + ext)
        if _isfile(os.path.join(base, "package.json")):
            return base
    return None


def _gitignore_rules(d):
    return _cached("gi", os.path.join(d, ".gitignore"), lambda p, t: [
        l.strip() for l in t.splitlines() if l.strip() and not l.strip().startswith(("#", "!"))])


def _build_artifact(target):
    """A missing file that a build step generates (gitignored, or named *.generated.* / *.gen.*) is not a bug."""
    import fnmatch
    base = os.path.basename(target).lower()
    if re.search(r"[.-](generated|gen|auto)\.", base) or os.path.basename(os.path.dirname(target)).lower() in (
            "generated", "__generated__", "gen"):
        return True
    d = os.path.dirname(target)
    for _ in range(10):
        rules = _gitignore_rules(d)
        if rules:
            rel = os.path.relpath(target, d).replace("\\", "/")
            parts = rel.split("/")
            for r in rules:
                r2 = r.rstrip("/")
                if r2.startswith("/"):
                    r2 = r2[1:]
                if "/" in r2:
                    if fnmatch.fnmatch(rel, r2) or rel.startswith(r2 + "/"):
                        return True
                elif any(fnmatch.fnmatch(p, r2) for p in parts):
                    return True
        if os.path.isdir(os.path.join(d, ".git")):
            break
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return False


def _is_relative(spec):
    return spec.startswith("./") or spec.startswith("../") or spec in (".", "..")


def _module_exports(p, seen=None):
    """(names, has_default, unknown, spans) for module p, following export * chains."""
    info = _js_info(p)
    if info is None:
        return set(), False, True, ""
    if info["unknown"]:
        return set(), False, True, ""
    names, spans = set(info["exports"]), info["spans"]
    unknown = False
    todo = [(os.path.dirname(p), s) for s in info["stars"]]
    seen = {os.path.normcase(p)}
    while todo:
        d, spec = todo.pop()
        if not _is_relative(spec):
            unknown = True
            break
        t = _resolve(d, spec)
        if not t or not t.lower().endswith(JS_EXTS) or os.path.normcase(t) in seen:
            if not t or not t.lower().endswith(JS_EXTS):
                unknown = True
                break
            continue
        seen.add(os.path.normcase(t))
        ti = _js_info(t)
        if ti is None or ti["unknown"]:
            unknown = True
            break
        names |= ti["exports"]
        spans += "\n" + ti["spans"]
        todo += [(os.path.dirname(t), s) for s in ti["stars"]]
        if ti["fuzzy"]:
            info = dict(info, fuzzy=True)
    return names, info["has_default"], unknown, spans


def _maybe_exported(name, spans):
    return re.search(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", spans) is not None


def _fmt_exports(names, has_default, name=None):
    lst = sorted(names, key=lambda s: (s.startswith("_"), s.lower()))
    hint = ""
    if name:
        close = difflib.get_close_matches(name, lst, n=1, cutoff=0.6)
        if close:
            hint = f"; did you mean '{close[0]}'?"
    shown = lst[:10] + (["default"] if has_default else [])
    more = f", … {len(lst) - 10} more" if len(lst) > 10 else ""
    return f"(exports: {', '.join(shown) or 'nothing'}{more}{hint})"


# ------------------------------------------------------------------------------------------- JS checks
def _check_imports(path, info, rel, out):
    d = os.path.dirname(path)
    for imp in info["imports"]:
        spec = imp["spec"]
        if not _is_relative(spec):
            continue
        t = _resolve(d, spec)
        if t is None:
            if not _build_artifact(os.path.normpath(os.path.join(d, _strip_query(spec)))):
                out.append((imp["line"], f"cannot resolve import '{spec}' (no such file)"))
            continue
        if imp["type_only"] or not imp["parsed"] or imp["kind"] in ("side", "dynamic", "require"):
            continue
        if not t.lower().endswith(JS_EXTS) or t.lower().endswith(".d.ts"):
            continue
        names, has_def, unknown, spans = _module_exports(t)
        if unknown:
            continue
        tinfo = _js_info(t) or {}
        for imp_name, local, ln in imp["names"]:
            if imp_name == "default":
                if not has_def:
                    out.append((ln, f"'{spec}' has no default export {_fmt_exports(names, has_def)}"))
                continue
            if imp_name in names or _maybe_exported(imp_name, spans):
                continue
            if tinfo.get("fuzzy") and _maybe_exported(imp_name, tinfo.get("masked", "")):
                continue
            out.append((ln, f"'{imp_name}' is not exported by {spec} {_fmt_exports(names, has_def, imp_name)}"))
        if imp.get("default") and not has_def:
            out.append((imp["line"], f"'{spec}' has no default export, but is imported as `import {imp['default']} from` "
                                     f"{_fmt_exports(names, has_def)}"))


_RX_CALL = re.compile(r"(?<![\w$#@.])(" + ID + r")\s*\(")
_RX_IDENT = re.compile(r"(?<![\w$#])" + ID)
_RX_CALL_ANY = re.compile(r"(?<![\w$#])(" + ID + r")\s*\(")


def _global_directive(text):
    out = set()
    for m in re.finditer(r"/\*\s*(?:global|globals|eslint-env)\s+([^*]*)\*/", text):
        out.update(re.findall(ID, m.group(1)))
    return out


def _check_calls(info, out, extra_masked="", extra_globals=()):
    """Calls to names that occur in the file only as calls (never declared/imported/assigned/passed)."""
    code = info["masked"]
    if code.count("\n") > CALL_CHECK_MAX_LINES:
        return
    called = {}
    for m in _RX_CALL.finditer(code):
        nm = m.group(1)
        if nm in JS_KEYWORDS or nm in JS_GLOBALS or nm in extra_globals:
            continue
        called.setdefault(nm, []).append(m.start(1))
    if not called:
        return
    total = Counter(_RX_IDENT.findall(code))
    calls_any = Counter(_RX_CALL_ANY.findall(code))  # incl. member calls
    extra = Counter(_RX_IDENT.findall(extra_masked)) if extra_masked else Counter()
    for nm, poss in called.items():
        if total[nm] != calls_any[nm] or extra[nm]:
            continue  # appears other than as a call -> declared / imported / assigned / a param / a key
        bare = []
        declared = False
        for o in poss:
            k = _prev_sig(code, o)
            if k >= 0 and code[k] == "." and code[max(0, k - 2):k + 1] != "...":
                continue  # member call (multi-line chain)
            pw = _prev_word(code, k) if k >= 0 and (code[k].isalnum() or code[k] in "_$") else ""
            if pw in ("function", "get", "set", "static", "async", "class", "interface", "type", "declare",
                      "abstract", "public", "private", "protected", "override", "readonly"):
                declared = True
                break
            if k >= 0 and code[k] == "*":
                declared = True  # generator function/method
                break
            if pw == "new":
                continue
            par = code.find("(", o)
            e = _match_close(code, par)
            if e < 0:
                declared = True  # can't tell
                break
            after = code[e + 1:e + 40].lstrip()
            if after.startswith("{") or after.startswith(":") or after.startswith("=>"):
                declared = True  # method/function definition (incl. TS return type)
                break
            bare.append(o)
        if declared or not bare:
            continue
        out.append((info["lines"].line(bare[0]), f"'{nm}' is called but not defined or imported in this file"))


def _member_before(code, o):
    k = _prev_sig(code, o)
    return k >= 0 and code[k] == "." and code[max(0, k - 2):k + 1] != "..."


def _check_jsx_tags(info, out, extra_globals=()):
    code = info["masked"]
    seen = set()
    for nm, pos in info["tags"]:
        if nm in seen or nm in JS_GLOBALS or nm in extra_globals:
            continue
        seen.add(nm)
        tagpos = {p for n, p in info["tags"] if n == nm}
        occ = [m.start() for m in re.finditer(r"(?<![\w$#])" + re.escape(nm) + r"(?![\w$])", code)]
        if any(o not in tagpos for o in occ):
            continue
        out.append((info["lines"].line(pos), f"<{nm}> is used in JSX but not defined or imported in this file"))


_RX_TOP_DECLS = [
    ("function", re.compile(r"function(?<![\w$.]function)\s*\*?\s*(" + ID + ")")),
    ("class", re.compile(r"class(?<![\w$.]class)\s+(" + ID + ")")),
    ("const", re.compile(r"const(?<![\w$.]const)\s+(" + ID + r")\s*[=:;,\n]")),
    ("let", re.compile(r"let(?<![\w$.]let)\s+(" + ID + r")\s*[=:;,\n]")),
]
_DECL_PREFIX = {"export", "default", "async", "declare", "abstract"}
_EXPR_PREV_KW = {"return", "yield", "await", "typeof", "void", "new", "case", "in", "of", "instanceof", "delete", "throw"}


def _is_statement_start(code, pos):
    """True when the keyword at pos starts a declaration statement (not `x = function f(){}` / `(class A {})`)."""
    k = _prev_sig(code, pos)
    while k >= 0 and (code[k].isalnum() or code[k] in "_$"):
        w = _prev_word(code, k)
        if w in _DECL_PREFIX:
            k = _prev_sig(code, k - len(w) + 1)
            continue
        return w not in _EXPR_PREV_KW and "\n" in code[k + 1:pos]
    return k < 0 or code[k] in ";{}" or (code[k] in ")]" and "\n" in code[k + 1:pos])


def _check_duplicates(info, out, is_module, is_ts):
    code = info["masked"]
    idx = _depth_index(code)
    seen = {}
    found = sorted(((m.start(), kind, m) for kind, rx in _RX_TOP_DECLS for m in rx.finditer(code)),
                   key=lambda x: x[0])
    for _, kind, m in found:
        if _depth_at(idx, m.start()) != 0 or not _is_statement_start(code, m.start()):
            continue
        name = m.group(1)
        if kind == "function":
            if not is_module:
                continue  # sloppy scripts may redeclare functions
            # TS overload signatures have no body
            par = code.find("(", m.end())
            e = _match_close(code, par) if par >= 0 else -1
            if e < 0:
                continue
            tail = code[e + 1:e + 300]
            bm = re.match(r"\s*(?::[^{;\n]*)?\s*\{", tail)
            if not bm:
                continue
        prev = seen.get(name)
        ln = info["lines"].line(m.start())
        if prev:
            out.append((ln, f"duplicate top-level declaration of '{name}' (first declared at line {prev})"))
        else:
            seen[name] = ln
    # imported names re-declared at top level
    if is_module:
        imported = {}
        for imp in info["imports"]:
            if imp["kind"] != "import" or imp["type_only"]:
                continue
            for _, loc, ln in imp["names"]:
                imported[loc] = ln
            if imp["default"]:
                imported[imp["default"]] = imp["line"]
        for name, ln in seen.items():
            if name in imported and not is_ts:
                out.append((ln, f"'{name}' is declared here but also imported at line {imported[name]}"))


# ------------------------------------------------------------------------------------------- reverse index (check 5)
_walk_cache = {}
_RX_RAW_SPEC = re.compile(r"""['"](\.{1,2}/[^'"\n]*)['"]""")


def _walk(root):
    key = os.path.normcase(root)
    now = time.monotonic()
    hit = _walk_cache.get(key)
    if hit and now - hit[0] < 10.0:
        return hit[1]
    files = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in fns:
            low = fn.lower()
            if low.endswith(JS_EXTS + (".html", ".htm")) and not low.endswith((".min.js", ".bundle.js", ".d.ts")):
                files.append(os.path.join(dp, fn))
        if len(files) >= INDEX_MAX_FILES:
            break
    files = files[:INDEX_MAX_FILES]
    _walk_cache[key] = (now, files)
    return files


def _raw_specs(p, text):
    return {m.group(1) for m in _RX_RAW_SPEC.finditer(text)}


def _spec_targets(from_dir, spec, target_nc):
    """Does a relative spec from from_dir (probably) point at target? String-only, no stat calls."""
    s = _strip_query(spec)
    base = os.path.normcase(os.path.normpath(os.path.join(from_dir, s)))
    if base == target_nc:
        return True
    troot, text = os.path.splitext(target_nc)
    if text == ".ts" and troot.endswith(".d"):
        troot = troot[:-2]
    broot, bext = os.path.splitext(base)
    if base == troot:
        return True
    if bext in (".js", ".jsx", ".mjs", ".cjs") and broot == troot:
        return True
    d, fn = os.path.split(troot)
    if fn == "index" and base == d:
        return True
    return False


def _check_importers(path, info, root, rel_of, out):
    if info["unknown"] or not info["has_esm"]:
        return
    # the in-memory exports of this (possibly unsaved) version
    names, has_def = set(info["exports"]), info["has_default"]
    spans = info["spans"]
    if info["stars"]:
        return  # re-exports: leave it to the forward check
    if info["fuzzy"]:
        spans += "\n" + info["masked"]
    target_nc = os.path.normcase(os.path.abspath(path))
    stem = os.path.splitext(os.path.basename(path))[0]
    if stem == "index":
        stem = os.path.basename(os.path.dirname(path))
    found = 0
    for f in _walk(root):
        if os.path.normcase(f) == target_nc:
            continue
        specs = _cached("raw", f, _raw_specs)
        if not specs or not any(stem in s for s in specs):
            continue
        fd = os.path.dirname(f)
        hits = [s for s in specs if _spec_targets(fd, s, target_nc)]
        if not hits:
            continue
        if f.lower().endswith((".html", ".htm")):
            continue
        fi = _js_info(f)
        if not fi:
            continue
        for imp in fi["imports"]:
            if imp["spec"] not in hits or imp["type_only"] or not imp["parsed"]:
                continue
            if imp["kind"] not in ("import", "export-from"):
                continue
            for imp_name, _, ln in imp["names"]:
                if imp_name == "default":
                    if not has_def and not info["has_default"]:
                        out.append((None, f"default export is imported by {rel_of(f)}:{ln} but this file has none"))
                    continue
                if imp_name in names or _maybe_exported(imp_name, spans):
                    continue
                close = difflib.get_close_matches(imp_name, sorted(names), n=1, cutoff=0.6)
                hint = f" (did you mean '{close[0]}'?)" if close else ""
                out.append((None, f"'{imp_name}' is imported by {rel_of(f)}:{ln} but no longer exported{hint}"))
                found += 1
            if imp.get("default") and not info["has_default"]:
                out.append((None, f"default export is imported by {rel_of(f)}:{imp['line']} but this file has none"))
        if found >= MAX_PROBLEMS:
            break


# ------------------------------------------------------------------------------------------- HTML
_RX_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.S | re.I)


def _html_diagnose(path, text, out):
    d = os.path.dirname(path)
    blocks = []
    classic_external = False
    # html comments can hold commented-out <script> tags: blank them (keeping offsets)
    text = re.sub(r"<!--.*?-->", lambda mm: _NONNL.sub(" ", mm.group()), text, flags=re.S)
    for m in _RX_SCRIPT.finditer(text):
        attrs, code = m.group(1), m.group(2)
        is_mod = re.search(r"type\s*=\s*['\"]?module", attrs, re.I) is not None
        typ = re.search(r"type\s*=\s*['\"]?([\w/+-]+)", attrs, re.I)
        if typ and not is_mod and typ.group(1).lower() not in ("text/javascript", "application/javascript"):
            continue  # importmap, json, shaders, templates
        src = re.search(r"\bsrc\s*=\s*(['\"])([^'\"]+)\1", attrs, re.I)
        if src:
            s = src.group(2).strip()
            if not is_mod:
                classic_external = True
            if not re.match(r"^(?:[a-z][\w+.-]*:|//|/|#|\{|\$|<)", s, re.I) and "{{" not in s and "${" not in s:
                if _resolve(d, s) is None and not _isfile(os.path.normpath(os.path.join(d, _strip_query(s)))):
                    out.append((text.count("\n", 0, m.start()) + 1, f"<script src=\"{s}\"> does not exist"))
            continue
        blocks.append((m.start(2), m.end(2), is_mod))
    if not blocks:
        return
    masked_all = []
    infos = []
    for a, b, is_mod in blocks:
        virt = _NONNL.sub(" ", text[:a]) + text[a:b]  # keep offsets -> line numbers match the html
        masked, tags = mask_js(virt, False)
        masked_all.append(masked[a:])
        infos.append((_analyze_js(path, virt, masked, tags), is_mod))
    for i, (info, is_mod) in enumerate(infos):
        if not is_mod:
            continue
        _check_imports(path, info, None, out)
        _check_duplicates(info, out, True, False)
        if not classic_external:
            others = "\n".join(masked_all[:i] + masked_all[i + 1:])
            _check_calls(info, out, extra_masked=others, extra_globals=_global_directive(text))


# ------------------------------------------------------------------------------------------- Solidity
_RX_SOL_CONTRACT = re.compile(r"(?<![\w$])(?:abstract\s+)?(contract|interface|library)\s+(\w+)\s*(?:is\s+([^{]*))?\{")
_RX_SOL_IMPORT = re.compile(r"(?<![\w$])import\s+([^;]*);")


def _sol_params(code, i):
    """i at '(' -> number of params / args (top-level commas), or None for named args `({a: 1})`."""
    e = _match_close(code, i)
    if e < 0:
        return None, i
    inner = code[i + 1:e]
    if not inner.strip():
        return 0, e
    if inner.strip().startswith("{"):
        return None, e
    return len(_split_top(inner)), e


def _parse_sol(path, text):
    code = mask_sol(text)
    L = _Lines(code)
    imports = []
    for m in _RX_SOL_IMPORT.finditer(code):
        body = m.group(1)
        q = re.search(r"['\"]", body)
        if not q:
            continue
        qpos = m.start(1) + q.start()
        spec, _ = _string_at(text, code, qpos)
        if spec is None:
            continue
        aliases, ns = {}, None
        sel = re.match(r"\s*\{([^}]*)\}", body)
        if sel:
            for part in sel.group(1).split(","):
                pm = re.match(r"\s*(\w+)(?:\s+as\s+(\w+))?\s*$", part)
                if pm and pm.group(2):
                    aliases[pm.group(2)] = pm.group(1)
        nm = re.search(r"\*\s*as\s+(\w+)|['\"]\s*as\s+(\w+)", body)
        if nm:
            ns = nm.group(1) or nm.group(2)
        imports.append(dict(spec=spec, line=L.line(m.start()), aliases=aliases, ns=ns))
    # blank assembly blocks (Yul has its own builtins) for the call checks
    asm_masked = code
    for m in re.finditer(r"(?<![\w$])assembly\s*(?:\"[^\"]*\"\s*)?(?:\([^)]*\)\s*)?\{", code):
        e = _match_close(asm_masked, m.end() - 1)
        if e > 0:
            asm_masked = asm_masked[:m.end()] + _NONNL.sub(" ", asm_masked[m.end():e]) + asm_masked[e:]
    contracts = {}
    spans = []
    for m in _RX_SOL_CONTRACT.finditer(code):
        bo = m.end() - 1
        e = _match_close(code, bo)
        if e < 0:
            continue
        if spans and spans[-1][0] < m.start() < spans[-1][1]:
            continue
        spans.append((m.start(), e))
        parents, parents_ok = [], True
        if m.group(3):
            for part in _split_top(m.group(3)):
                part = part.strip()
                pm = re.match(r"([\w.]+)\s*(\(.*\))?\s*$", part, re.S)
                if not pm:
                    parents_ok = False
                    continue
                parents.append(pm.group(1))
        body = code[bo + 1:e]
        c = dict(name=m.group(2), kind=m.group(1), parents=parents, parents_ok=parents_ok, start=bo + 1, end=e,
                 line=L.line(m.start()))
        c.update(_sol_members(body, bo + 1, code))
        contracts[c["name"]] = c
    # file level (outside contracts)
    outside = list(code)
    for a, b in spans:
        outside[a:b + 1] = " " * (b + 1 - a)
    outside = "".join(outside)
    top = _sol_members(outside, 0, outside, file_level=True)
    return dict(path=path, code=code, asm=asm_masked, lines=L, imports=imports, contracts=contracts, top=top,
                text=text)


def _sol_members(body, off, code, file_level=False):
    res = dict(funcs=set(), mods=set(), events={}, errors={}, types=set(), vars=set(), headers=[])
    for m in re.finditer(r"(?<![\w$])(function|modifier)\s+(\w+)\s*\(", body):
        res["funcs" if m.group(1) == "function" else "mods"].add(m.group(2))
    for m in re.finditer(r"(?<![\w$])modifier\s+(\w+)\s*[{;]", body):  # modifier without ()
        res["mods"].add(m.group(1))
    for m in re.finditer(r"(?<![\w$])(event|error)\s+(\w+)\s*\(", body):
        cnt, _ = _sol_params(body, m.end() - 1)
        res["events" if m.group(1) == "event" else "errors"].setdefault(m.group(2), set()).add(cnt)
    for m in re.finditer(r"(?<![\w$])(?:struct|enum)\s+(\w+)|(?<![\w$])type\s+(\w+)\s+is\b", body):
        res["types"].add(m.group(1) or m.group(2))
    # state variables / constants: last identifier before = or ; in depth-0 statements
    depth, stmt_start = 0, 0
    for k, ch in enumerate(body):
        if ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
            if depth == 0 and ch == "}":
                stmt_start = k + 1
        elif depth == 0 and ch == ";":
            stmt = body[stmt_start:k]
            stmt_start = k + 1
            s = re.split(r"=(?!>)", stmt, 1)[0]  # not the => of mapping(a => b)
            ws = re.findall(r"\w+", s)
            if ws and ws[0] not in ("using", "import", "pragma", "function", "event", "error", "modifier"):
                res["vars"].add(ws[-1])
    # function / constructor / modifier headers: modifiers used
    # (a bare `function (` is a function *type* in a parameter / variable - not a header)
    for m in re.finditer(r"(?<![\w$])(function\s+\w+|constructor|modifier\s+\w+|fallback|receive)\s*\(", body):
        p = m.end() - 1
        e = _match_close(body, p)
        if e < 0:
            continue
        hm = re.compile(r"[^{;]*").match(body, e + 1)
        header = hm.group()
        res["headers"].append((off + e + 1, header, m.group(1).split()[0]))
    return res


def _sol_info(p):
    return _cached("sol", p, _parse_sol)


def _sol_resolve(from_dir, spec, file_path):
    if spec.startswith("./") or spec.startswith("../"):
        t = os.path.normpath(os.path.join(from_dir, spec))
        return t if _isfile(t) else None
    # remappings / node_modules / foundry lib
    d = from_dir
    for _ in range(12):
        rm = os.path.join(d, "remappings.txt")
        if _isfile(rm):
            for line in (_read(rm) or "").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.split(":")[-1].strip()
                    if k and spec.startswith(k):
                        t = os.path.normpath(os.path.join(d, v.strip() + spec[len(k):]))
                        if _isfile(t):
                            return t
        t = os.path.join(d, "node_modules", spec)
        if _isfile(t):
            return os.path.normpath(t)
        t = os.path.join(d, spec)  # absolute-from-project-root style imports
        if _isfile(t) and (_isfile(os.path.join(d, "hardhat.config.js")) or _isfile(os.path.join(d, "foundry.toml"))
                           or _isfile(os.path.join(d, "hardhat.config.ts"))):
            return os.path.normpath(t)
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return None


def _sol_universe(path, info):
    """All files reachable through imports. Returns (files [info], aliases, complete)."""
    files, aliases, complete = [], {}, True
    seen = set()
    todo = [(path, info)]
    while todo:
        p, inf = todo.pop()
        key = os.path.normcase(p)
        if key in seen:
            continue
        seen.add(key)
        files.append(inf)
        if len(seen) > 120:
            complete = False
            break
        for imp in inf["imports"]:
            aliases.update(imp["aliases"])
            t = _sol_resolve(os.path.dirname(p), imp["spec"], p)
            if t is None:
                complete = False
                continue
            ti = _sol_info(t)
            if ti is None:
                complete = False
                continue
            todo.append((t, ti))
    return files, aliases, complete


def _sol_diagnose(path, info, out):
    d = os.path.dirname(path)
    for imp in info["imports"]:
        s = imp["spec"]
        if (s.startswith("./") or s.startswith("../")) and _sol_resolve(d, s, path) is None:
            out.append((imp["line"], f"cannot resolve import \"{s}\" (no such file)"))
    files, aliases, complete = _sol_universe(path, info)
    if not complete:
        return
    all_contracts = {}
    for f in reversed(files):
        all_contracts.update(f["contracts"])
    for c in info["contracts"].values():
        all_contracts[c["name"]] = c
    for a, orig in aliases.items():
        if orig in all_contracts:
            all_contracts.setdefault(a, all_contracts[orig])
    # file-level symbols of the whole universe
    U = dict(funcs=set(), events={}, errors={}, types=set(all_contracts), vars=set())
    for f in files:
        t = f["top"]
        U["funcs"] |= t["funcs"]
        U["types"] |= t["types"]
        U["vars"] |= t["vars"]
        for k in ("events", "errors"):
            for n, cnts in t[k].items():
                U[k].setdefault(n, set()).update(cnts)
        for c in f["contracts"].values():
            U["types"] |= c["types"]  # qualified use C.S is a member; bare use inside C is via chain anyway
    U["types"] |= set(aliases)
    code, asm, L = info["code"], info["asm"], info["lines"]
    universe_text = "\n".join(f["code"] for f in files)
    for c in info["contracts"].values():
        chain, ok = _sol_chain(c, all_contracts)
        if not ok:
            continue
        S = dict(funcs=set(U["funcs"]), mods=set(), events={}, errors={}, types=set(U["types"]), vars=set(U["vars"]))
        for k in ("events", "errors"):
            for n, cnts in U[k].items():
                S[k].setdefault(n, set()).update(cnts)
        for cc in chain:
            S["funcs"] |= cc["funcs"]
            S["mods"] |= cc["mods"]
            S["types"] |= cc["types"]
            S["vars"] |= cc["vars"]
            for k in ("events", "errors"):
                for n, cnts in cc[k].items():
                    S[k].setdefault(n, set()).update(cnts)
        a, b = c["start"], c["end"]
        body = asm[a:b]
        # emit / revert
        for m in re.finditer(r"(?<![\w$])(emit|revert)\s+(\w+)\s*\(", body):
            kind, nm = m.group(1), m.group(2)
            table = S["events" if kind == "emit" else "errors"]
            pos = a + m.start(2)
            if nm not in table:
                if kind == "revert" and (nm in S["funcs"] or nm in S["types"]):
                    continue
                what = "event" if kind == "emit" else "custom error"
                known = sorted(table)[:8]
                close = difflib.get_close_matches(nm, sorted(table), n=1, cutoff=0.6)
                hint = f"; did you mean '{close[0]}'?" if close else (f" (known: {', '.join(known)})" if known else "")
                out.append((L.line(pos), f"{kind} {nm}(...): {what} '{nm}' is not declared in {c['name']} or its bases{hint}"))
                continue
            cnt, _ = _sol_params(asm, a + m.end() - 1)
            cnts = table[nm]
            if cnt is not None and None not in cnts and cnt not in cnts:
                exp = "/".join(str(x) for x in sorted(cnts))
                out.append((L.line(pos), f"{kind} {nm}: {cnt} argument(s) given, but {nm} is declared with {exp}"))
        # modifiers in headers
        for hpos, header, kind in c["headers"]:
            if not (a <= hpos <= b):
                continue
            h = header
            sp = lambda mm: " " * len(mm.group())  # keep offsets for line numbers
            for _ in range(6):  # drop returns(..), override(..), modifier / base-constructor args
                h2 = re.sub(r"\([^()]*\)", sp, h)
                if h2 == h:
                    break
                h = h2
            for m in re.finditer(r"(?<![\w$.])(\w+)\s*(\()?", h):
                w = m.group(1)
                if w in SOL_HEADER_WORDS or w in SOL_KEYWORDS or w.isdigit():
                    continue
                if w in S["mods"] or w in S["types"] or w in all_contracts:
                    continue
                pw = h[:m.start()].rstrip()
                if pw.endswith("."):
                    continue
                out.append((L.line(hpos + m.start()), f"modifier '{w}' is not declared in {c['name']} or its bases"))
        # internal calls and this.f()
        for m in re.finditer(r"(?<![\w$])(this\s*\.\s*)?(\w+)\s*\(", body):
            nm = m.group(2)
            pos = a + m.start(2)
            if m.group(1):
                if nm in S["funcs"] or nm in S["vars"]:
                    continue
                out.append((L.line(pos), f"this.{nm}(): no function '{nm}' in {c['name']} or its bases"))
                continue
            k = _prev_sig(asm, a + m.start())
            if k >= 0 and asm[k] == ".":
                continue
            pw = _prev_word(asm, k) if k >= 0 and (asm[k].isalnum() or asm[k] == "_") else ""
            if pw in SOL_KEYWORDS or pw in ("emit", "revert", "new", "catch", "function", "modifier", "event", "error",
                                            "returns", "is", "constructor", "contract", "interface", "library"):
                continue
            if nm in SOL_KEYWORDS or nm in SOL_BUILTINS or SOL_ELEMENTARY.match(nm):
                continue
            if nm in S["funcs"] or nm in S["mods"] or nm in S["types"] or nm in S["vars"] or nm in S["events"] \
                    or nm in S["errors"] or nm in all_contracts:
                continue
            # declared anywhere (function-typed locals/params etc.)?
            occ = len(re.findall(r"(?<![\w$])" + nm + r"(?![\w$])", universe_text))
            calls = len(re.findall(r"(?<![\w$])" + nm + r"\s*\(", universe_text))
            if occ != calls:
                continue
            if any(o == nm for o in ("_",)):
                continue
            out.append((L.line(pos), f"'{nm}(...)' is not a function of {c['name']} or its bases (nor imported)"))


def _sol_chain(c, all_contracts):
    chain, seen, todo = [], set(), [c]
    while todo:
        x = todo.pop()
        if x["name"] in seen:
            continue
        seen.add(x["name"])
        if not x["parents_ok"]:
            return chain, False
        chain.append(x)
        for p in x["parents"]:
            if "." in p or p not in all_contracts:
                return chain, False
            todo.append(all_contracts[p])
    return chain, True


# ------------------------------------------------------------------------------------------- entry point
def diagnose(path, root=None, text=None):
    """Semantic problems in one file: ["rel/path.js:12: message", ...] (at most MAX_PROBLEMS), [] when fine."""
    try:
        return _diagnose(path, root, text)
    except Exception:
        return []


def _diagnose(path, root, text):
    p = os.path.abspath(str(path))
    ext = os.path.splitext(p)[1].lower()
    if ext not in JS_EXTS + (".html", ".htm", ".sol"):
        return []
    if text is None:
        if not os.path.isfile(p) or os.path.getsize(p) > MAX_BYTES:
            return []
        text = _read(p)
        if text is None:
            return []
    rootp = os.path.abspath(str(root)) if root else None

    def rel_of(f):
        if rootp:
            try:
                r = os.path.relpath(f, rootp)
                if not r.startswith(".."):
                    return r.replace("\\", "/")
            except ValueError:
                pass
        return os.path.basename(f)

    out = []
    if ext == ".sol":
        info = _parse_sol(p, text)
        _sol_diagnose(p, info, out)
    elif ext in (".html", ".htm"):
        _html_diagnose(p, text, out)
    else:
        info = _parse_js(p, text)
        is_ts = ext in (".ts", ".tsx", ".mts", ".cts")
        is_module = info["has_esm"] or ext in (".mjs", ".mts") or is_ts or ext in (".jsx", ".tsx")
        _check_imports(p, info, rootp, out)
        is_node_script = ext in (".cjs", ".cts") or (bool(re.search(r"require(?<![\w$.]require)\s*\(", info["masked"]))
                                                     and info["is_cjs"])
        if is_module or is_node_script:
            g = _global_directive(text)
            _check_calls(info, out, extra_globals=g)
            if info["tags"]:
                _check_jsx_tags(info, out, extra_globals=g)
        _check_duplicates(info, out, is_module and not info["is_cjs"], is_ts)
        if rootp and info["has_esm"]:
            _check_importers(p, info, rootp, rel_of, out)
    me = rel_of(p)
    res, seen = [], set()
    for ln, msg in sorted(out, key=lambda x: (x[0] is None, x[0] or 0)):
        s = f"{me}:{ln}: {msg}" if ln else f"{me}: {msg}"
        if s not in seen:
            seen.add(s)
            res.append(s)
        if len(res) >= MAX_PROBLEMS:
            break
    return res


if __name__ == "__main__":
    import sys
    t0 = time.perf_counter()
    target = sys.argv[1]
    r = sys.argv[2] if len(sys.argv) > 2 else None
    for line in diagnose(target, r):
        print(line)
    print(f"--- {(time.perf_counter() - t0) * 1000:.0f} ms", file=sys.stderr)
