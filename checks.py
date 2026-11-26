"""Run-based verification for ink.

quick_check(path)      fast syntax check of one file, run automatically after every edit (~50-300 ms)
project_checks(root)   the project's real checks (tests, compile, lint), detected or set in .ink/checks.json
run_checks(root, ...)  run them and return a compact report: failures in full, passes as one line

A small model that sees the real error right after a bad edit fixes it in one step; one that doesn't
builds three more edits on top of a broken file.
"""
import json, os, re, shutil, subprocess, tempfile, time
from pathlib import Path

IS_WIN = os.name == "nt"
NODE = shutil.which("node")
PY = shutil.which("python") or shutil.which("python3")
ESM = re.compile(r"^\s*(import\s|import\(|export\s)", re.M)
SCRIPT_TAG = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)


def _run(argv, cwd=None, stdin=None, timeout=30):
    try:
        r = subprocess.run(argv, cwd=cwd, input=stdin, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, shell=False)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 0, ""  # a slow checker must never block or fail an edit
    except Exception as e:
        return 0, ""


def _node_check(code, module, label, line_offset=0):
    """Syntax-check JS source. Uses stdin, because `node --check file.js` silently passes broken ES modules."""
    if not NODE:
        return None
    argv = [NODE, "--input-type=module" if module else "--input-type=commonjs", "--check"]
    rc, out = _run(argv, stdin=code)
    if rc == 0:
        return None
    m = re.search(r"\[stdin\]:(\d+)", out)
    line = int(m.group(1)) + line_offset if m else None
    msg = next((l for l in out.splitlines() if "Error" in l and ":" in l), out.splitlines()[-1] if out else "syntax error")
    # keep the caret block (source line + ^) - it shows the model exactly where
    lines = out.splitlines()
    caret = ""
    for i, l in enumerate(lines):
        if l.strip().startswith("^") and i > 0:
            caret = lines[i - 1].rstrip() + "\n" + l.rstrip() + "\n"
            break
    return f"{label}:{line or '?'}: {msg.strip()}\n{caret}".rstrip()


def quick_check(path):
    """Returns None if the file looks fine (or can't be checked cheaply), else a short error report."""
    p = Path(path)
    if not p.is_file() or p.stat().st_size > 2_000_000:
        return None
    ext = p.suffix.lower()
    try:
        text = p.read_text("utf-8", "replace")
    except Exception:
        return None
    if ext in (".js", ".mjs", ".cjs"):
        module = ext == ".mjs" or (ext == ".js" and bool(ESM.search(text)))
        return _node_check(text, module, p.name)
    if ext in (".html", ".htm"):
        errs = []
        for m in SCRIPT_TAG.finditer(text):
            attrs, code = m.group(1), m.group(2)
            if "src=" in attrs or not code.strip() or re.search(r"type=['\"](?!module|text/javascript)", attrs):
                continue
            offset = text.count("\n", 0, m.start(2))
            e = _node_check(code, "module" in attrs, p.name, offset)
            if e:
                errs.append(e)
        return "\n".join(errs) or None
    if ext == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as e:
            return f"{p.name}:{e.lineno}: invalid JSON: {e.msg}"
        return None
    if ext == ".py":
        try:
            compile(text, str(p), "exec")
        except SyntaxError as e:
            return f"{p.name}:{e.lineno}: SyntaxError: {e.msg}\n{(e.text or '').rstrip()}"
        return None
    if ext in (".jsx", ".tsx", ".ts"):
        root = _find_up(p.parent, "package.json")
        lint = root and (root / "node_modules" / ".bin" / ("oxlint.cmd" if IS_WIN else "oxlint"))
        if lint and lint.exists():
            rc, out = _run([str(lint), "--quiet", str(p)], cwd=root, timeout=20)
            if rc != 0 and out:
                return _tail(out, 1500)
        return None
    if ext == ".sol":
        return _sol_sanity(text, p.name)
    return None


def _sol_sanity(text, name):
    """No solc here: catch the cheap structural mistakes (unbalanced braces) that break the whole compile."""
    clean = re.sub(r"//[^\n]*|/\*.*?\*/|\"(\\.|[^\"\\])*\"|'(\\.|[^'\\])*'", "", text, flags=re.S)
    depth = 0
    for i, ch in enumerate(clean):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return f"{name}:{clean.count(chr(10), 0, i) + 1}: unbalanced '}}' (closes more blocks than were opened)"
    if depth:
        return f"{name}: {depth} unclosed '{{' block(s) at end of file"
    if not re.search(r"^\s*pragma\s+solidity", text, re.M):
        return f"{name}: missing `pragma solidity` line"
    return None


def _find_up(d, marker):
    d = Path(d).resolve()
    for q in [d, *d.parents]:
        if (q / marker).exists():
            return q
    return None


def _tail(s, n):
    return s if len(s) <= n else "…\n" + s[-n:]


# ---------------------------------------------------------------- project checks
PLACEHOLDER = re.compile(r"no test specified", re.I)


def project_checks(root):
    """[(name, command, cwd)] - from <root>/.ink/checks.json if present, else detected from the project files.
    checks.json: {"checks": [{"name": "unit", "run": "npm test", "cwd": "."}, ...]} or {"checks": ["npm test"]}"""
    root = Path(root)
    cfg = root / ".ink" / "checks.json"
    if cfg.is_file():
        try:
            items = json.loads(cfg.read_text("utf-8")).get("checks", [])
            res = []
            for it in items:
                if isinstance(it, str):
                    it = {"run": it}
                res.append((it.get("name") or it["run"], it["run"], str((root / it.get("cwd", ".")).resolve())))
            return res
        except Exception:
            pass
    found = []
    for d in [root] + [c for c in sorted(root.iterdir()) if c.is_dir() and not c.name.startswith((".", "node_modules"))][:30]:
        found += _detect(d, root)
    seen, out = set(), []
    for c in found:
        if (c[1], c[2]) not in seen:
            seen.add((c[1], c[2]))
            out.append(c)
    return out[:6]


def _detect(d, root):
    res = []
    rel = "." if d == root else d.name
    hh = next((f for f in ("hardhat.config.js", "hardhat.config.ts", "hardhat.config.cjs") if (d / f).exists()), None)
    if hh:
        res.append((f"{rel}: contracts compile", "npx hardhat compile --quiet", str(d)))
        tdir = d / "test"
        if tdir.is_dir() and any(tdir.iterdir()):
            res.append((f"{rel}: contract tests", "npx hardhat test", str(d)))
    if (d / "foundry.toml").exists():
        res.append((f"{rel}: forge test", "forge test", str(d)))
    pj = d / "package.json"
    if pj.is_file():
        try:
            scripts = json.loads(pj.read_text("utf-8")).get("scripts", {}) or {}
        except Exception:
            scripts = {}
        t = scripts.get("test", "")
        if t and not PLACEHOLDER.search(t) and not hh:
            res.append((f"{rel}: tests", "npm test --silent", str(d)))
        for k in ("typecheck", "type-check", "tsc"):
            if k in scripts:
                res.append((f"{rel}: {k}", f"npm run {k} --silent", str(d)))
                break
        else:
            if (d / "tsconfig.json").is_file() and (d / "node_modules" / "typescript").is_dir():
                res.append((f"{rel}: typecheck", "npx tsc --noEmit", str(d)))
        if "lint" in scripts:
            res.append((f"{rel}: lint", "npm run lint --silent", str(d)))
    if (d / "pyproject.toml").exists() or (d / "pytest.ini").exists():
        if any(d.glob("test*")) or (d / "tests").is_dir():
            res.append((f"{rel}: pytest", "python -m pytest -q -x", str(d)))
    return res


FAIL_LINE = re.compile(r"(✖|✗|×|FAIL|not ok|Error|error|failed|failing|AssertionError|expected|Exception|Traceback)", re.I)


def _clip(l, n=220):
    return l if len(l) <= n else l[:n] + "…"


def summarize_failure(out, limit=2500):
    """Keep what a model needs from a failing test run: the failing tests and their errors, not the noise."""
    lines = out.splitlines()
    # node --test (TAP): each failure is `not ok N - name` + an indented YAML block (location, error, expected...)
    if any(re.match(r"\s*not ok \d+", l) for l in lines):
        blocks, counts = [], [l.strip() for l in lines if re.match(r"#\s*(tests|pass|fail)\s+\d+", l.strip())][-3:]
        i = 0
        while i < len(lines):
            if re.match(r"\s*not ok \d+", lines[i]) and "# SKIP" not in lines[i]:
                blk = [_clip(lines[i].strip())]
                j = i + 1
                while j < len(lines) and len(blk) < 14 and not re.match(r"\s*(ok|not ok) \d+|\s*# Subtest|\s*1\.\.\d+|\s*# tests", lines[j]):
                    s = lines[j].rstrip()
                    if s.strip() and s.strip() not in ("---", "...") and not re.search(r"duration_ms|type: 'test'|failureType", s):
                        blk.append(_clip(s))
                    j += 1
                blocks.append("\n".join(blk))
                i = j
            else:
                i += 1
        text = "\n\n".join(blocks[:6]) + (f"\n… {len(blocks) - 6} more failing tests" if len(blocks) > 6 else "")
        text += ("\n" + " · ".join(counts)) if counts else ""
        return text if len(text) <= limit else text[:limit] + "\n…"
    # mocha / hardhat: everything after "N failing" is the useful part
    for k, l in enumerate(lines):
        if re.match(r"\s*\d+ failing", l):
            text = "\n".join(_clip(x) for x in lines[k:] if x.strip() and not re.match(r"\s+at (?!.*(test|contracts)[\\/])", x))
            return text if len(text) <= limit else text[:limit] + "\n…"
    lines = [_clip(l) for l in lines]
    keep = set()
    for i, l in enumerate(lines):
        if FAIL_LINE.search(l):
            keep.update(range(max(0, i - 2), min(len(lines), i + 4)))
    body = "\n".join(lines[i] for i in sorted(keep)) if keep else ""
    tail = "\n".join(lines[-15:])
    text = (body + "\n…\n" + tail) if body and tail not in body else (body or tail)
    return text if len(text) <= limit else text[: limit // 2] + "\n…\n" + text[-limit // 2:]


def relevant(checks, changed):
    """Only the checks that cover the changed files: each file belongs to the deepest check dir containing it
    (an edit in app/frontend runs the frontend lint, not the contract tests). Unchanged subprojects are skipped,
    so failures that were already there aren't blamed on this turn's work."""
    if not changed:
        return checks
    dirs = sorted({Path(c[2]).resolve() for c in checks}, key=lambda d: len(str(d)), reverse=True)
    hit = set()
    for f in changed:
        f = Path(f).resolve()
        for d in dirs:
            if d == f or d in f.parents:
                hit.add(d)
                break
    return [c for c in checks if Path(c[2]).resolve() in hit]


def run_checks(root, shell_argv, timeout=300, only=None, stop=None, changed=None):
    """Run the project checks. shell_argv(cmd) -> argv list for the shell. Returns (report, all_ok, ran).
    changed: files edited this session -> run only the checks covering them."""
    checks = project_checks(root)
    if only:
        checks = [c for c in checks if only.lower() in c[0].lower() or only.lower() in c[1].lower()] or checks
    elif changed is not None:
        checks = relevant(checks, changed)
        if not checks:
            return "No project checks cover the changed files.", True, 0
    if not checks:
        return ("No project checks found (no test script, hardhat/foundry config or .ink/checks.json). "
                "Verify by running the code directly, or create .ink/checks.json with the commands to run."), True, 0
    parts, ok_all = [], True
    for name, cmd, cwd in checks:
        if stop is not None and stop.is_set():
            parts.append(f"- {name}: skipped (interrupted)")
            continue
        t0 = time.time()
        try:
            r = subprocess.run(shell_argv(cmd), cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                               env=dict(os.environ, NO_COLOR="1", FORCE_COLOR="0", CI="1"))
            rc, out = r.returncode, re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", r.stdout + r.stderr)
        except subprocess.TimeoutExpired:
            rc, out = 124, f"timed out after {timeout}s"
        secs = time.time() - t0
        if rc == 0:
            n = re.search(r"(?:# pass|passing)\s*\(?(\d+)", out) or re.search(r"(\d+)\s+passing", out)
            parts.append(f"- PASS {name} ({cmd}, {secs:.0f}s{', ' + n.group(1) + ' passed' if n else ''})")
        else:
            ok_all = False
            parts.append(f"- FAIL {name} ({cmd}, exit {rc}, {secs:.0f}s):\n" + summarize_failure(out))
    return "\n".join(parts), ok_all, len(checks)


TS_ERR = re.compile(r"^(?P<file>[^\s(][^(]*)\((?P<line>\d+),(?P<col>\d+)\): error (?P<code>TS\d+): (?P<msg>.*)$")


def ts_errors(files, timeout=60):
    """tsc --noEmit for the TypeScript project(s) holding `files`. Returns (report, seconds, n_errors_in_files)
    or None when there is nothing to check (no tsconfig / typescript not installed). Errors in the edited files
    come first; others are counted, since edits can break callers."""
    roots = {}
    for f in files:
        f = Path(f).resolve()
        for d in f.parents:
            if (d / "tsconfig.json").is_file():
                roots.setdefault(d, []).append(f)
                break
            if (d / "package.json").is_file() or d.parent == d:
                break
    if not roots:
        return None
    lines, total, t0 = [], 0, time.time()
    for root, fs in roots.items():
        tsc = root / "node_modules" / "typescript" / "bin" / "tsc"
        if not tsc.is_file() or not NODE:
            continue
        r = _run([NODE, str(tsc), "--noEmit", "--pretty", "false", "-p", str(root)], cwd=str(root), timeout=timeout)
        if r is None:
            continue
        code, out = r if isinstance(r, tuple) else (getattr(r, "returncode", 0), getattr(r, "stdout", "") or "")
        errs = [m for m in (TS_ERR.match(l.strip()) for l in str(out).splitlines()) if m]
        mine = {str(f).lower() for f in fs}
        def is_mine(m):
            return str((root / m.group("file")).resolve()).lower() in mine
        errs.sort(key=lambda m: not is_mine(m))
        total += len(errs)
        for m in errs[:12]:
            lines.append(f"{m.group('file')}:{m.group('line')}:{m.group('col')} {m.group('code')} {m.group('msg')[:200]}")
        if len(errs) > 12:
            lines.append(f"... {len(errs) - 12} more")
    return chr(10).join(lines), time.time() - t0, total
