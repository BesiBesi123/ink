"""Copy ink into the public release repo (../ink-release), leaving out private and generated files.
usage: python tools/export_release.py [target_dir]"""
import shutil
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
DST = Path(sys.argv[1]) if len(sys.argv) > 1 else SRC.parent / "ink-release"

INCLUDE = [
    "ink.py", "agents.py", "checkpoints.py", "checks.py", "compress_server.py", "dream.py", "hooks.py",
    "ink_telegram.py", "ink_watch.py", "jobs.py", "lsp_lite.py", "notify.py", "playtest.mjs", "playtest.py",
    "recall.py", "repomap.py", "safety.py", "PROTOCOL.md", "README.md", "models.json", "install.ps1",
    "codemode", "servers", "guides", "templates", "tools", "skills/project_hotspots", "tui",
]
SKIP_DIRS = {"node_modules", "__pycache__", ".venv-compress", "sandbox"}
SKIP_FILES = {".pyc", ".log"}

LAUNCHER = r"""@echo off
rem ink from this folder (install.ps1 also puts an `ink` command on your PATH)
if not defined INK_PY set "INK_PY=python"
where node >nul 2>nul
if errorlevel 1 goto plain
if not exist "%~dp0tui\node_modules" goto plain
node "%~dp0tui\ink-tui.mjs" %*
exit /b %errorlevel%
:plain
"%INK_PY%" -X utf8 "%~dp0ink.py" %*
"""

TELEGRAM = r"""@echo off
rem ink on Telegram: set telegram_token (and telegram_chat_id) in %USERPROFILE%\.ink\config.json first
if not defined INK_PY set "INK_PY=python"
"%INK_PY%" -X utf8 "%~dp0ink_telegram.py" %*
"""


def copy(rel):
    s, d = SRC / rel, DST / rel
    if s.is_dir():
        for f in s.rglob("*"):
            if f.is_file() and not (set(f.relative_to(SRC).parts) & SKIP_DIRS) and f.suffix not in SKIP_FILES:
                t = DST / f.relative_to(SRC)
                t.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, t)
    elif s.is_file():
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
    else:
        print("missing:", rel)


for rel in INCLUDE:
    copy(rel)
(DST / "ink.cmd").write_text(LAUNCHER, "ascii")
(DST / "ink-telegram.cmd").write_text(TELEGRAM, "ascii")
print("exported to", DST)
