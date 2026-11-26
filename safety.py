"""Graded safety for ink: everything inside the project runs automatically (auto mode), but actions that leave
the machine or the project always need the user's OK - even in auto mode - and pushes are scanned for secrets.

outward(cmd) -> reason or None      commands that publish, deploy, push, spend or touch remote state
secret_scan(cwd) -> [findings]      secrets in what a `git push` would upload (+ tracked .env files)
"""
import re, subprocess
from pathlib import Path

OUTWARD = [
    (r"\bgit\s+push\b", "pushes to a remote"),
    (r"\bgit\s+(remote\s+(add|set-url)|reset\s+--hard\s+origin)", "changes git remotes"),
    (r"\bgh\s+(repo\s+(create|delete|edit|rename)|pr\s+(create|merge|close)|release\s+create|secret\s+set|workflow\s+run|api\s+-X\s*(POST|PUT|PATCH|DELETE))", "changes GitHub"),
    (r"\b(npm|pnpm|yarn)\s+publish\b", "publishes a package"),
    (r"\bhardhat\s+(run|deploy|ignition\s+deploy)\b[^\n]*--network\s+(?!hardhat\b|localhost\b)\w+", "deploys to a live network"),
    (r"\bforge\s+(script|create)\b[^\n]*--broadcast", "broadcasts transactions"),
    (r"\bcast\s+send\b", "sends a transaction"),
    (r"\b(render|vercel|netlify|flyctl|fly|railway|wrangler|firebase)\s+(deploy|up|publish|launch)\b", "deploys"),
    (r"\bcurl\b[^\n]*-X\s*(POST|PUT|PATCH|DELETE)[^\n]*https?://(?!127\.0\.0\.1|localhost)", "writes to a remote API"),
    (r"\b(docker\s+push|twine\s+upload)\b", "publishes"),
    (r"\b(npm|pnpm|yarn|pip|pip3|cargo)\s+(install|add|i)\s+-g\b", "installs globally"),
]
_OUT = [(re.compile(p, re.I), why) for p, why in OUTWARD]

SECRETS = [
    (r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----", "private key block"),
    (r"(?i)(private[_-]?key|priv[_-]?key|deployer[_-]?key|secret[_-]?key)\s*[:=]\s*['\"]?(0x)?[0-9a-fA-F]{64}\b", "wallet private key"),
    (r"(?i)\b(mnemonic|seed[_ ]?phrase)\b\s*[:=]\s*['\"]?([a-z]+\s+){11,23}[a-z]+", "wallet seed phrase"),
    (r"\bghp_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{50,}", "GitHub token"),
    (r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{32,}", "API key (sk-...)"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key"),
    (r"\b\d{8,10}:AA[A-Za-z0-9_-]{33}\b", "Telegram bot token"),
    (r"\brnd_[A-Za-z0-9]{20,}\b", "Render API key"),
    (r"(?i)\b(api[_-]?key|apikey|auth[_-]?token|access[_-]?token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{24,}['\"]", "hard-coded API token"),
]
_SEC = [(re.compile(p), why) for p, why in SECRETS]
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".glb", ".gltf", ".bin", ".wasm", ".zip", ".pdf",
            ".mp3", ".ogg", ".wav", ".mp4", ".woff", ".woff2", ".ttf", ".lock"}


def outward(cmd):
    for rx, why in _OUT:
        if rx.search(cmd or ""):
            return why
    return None


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                       timeout=30)
    return r.stdout if r.returncode == 0 else ""


def secret_scan(cwd, limit=12):
    """Files a push would upload: commits not on the upstream yet (or all tracked files if there's no upstream).
    Returns ["path:line: kind", ...]."""
    cwd = Path(cwd)
    if not _git(cwd, "rev-parse", "--is-inside-work-tree").strip():
        return []
    up = _git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").strip()
    files = _git(cwd, "diff", "--name-only", f"{up}..HEAD").split() if up else _git(cwd, "ls-files").splitlines()
    found = []
    for rel in files:
        name = Path(rel).name.lower()
        if name == ".env" or (name.startswith(".env.") and not name.endswith((".example", ".sample", ".template"))):
            found.append(f"{rel}: .env file is tracked by git (it will be uploaded)")
            continue
        p = cwd / rel
        if p.suffix.lower() in SKIP_EXT or not p.is_file() or p.stat().st_size > 1_500_000:
            continue
        try:
            text = p.read_text("utf-8", "replace")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if len(line) > 2000:
                continue
            for rx, why in _SEC:
                if rx.search(line) and not re.search(r"(?i)example|placeholder|your[_-]|xxxx|<[^>]+>|process\.env|os\.environ|getenv", line):
                    found.append(f"{rel}:{i}: {why}")
                    break
            if len(found) >= limit:
                return found
    return found
