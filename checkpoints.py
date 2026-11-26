"""Automatic project checkpoints: a snapshot before every turn, restorable with /rewind.

Snapshots live in a private git repository per project under ~/.ink/checkpoints/ (your own .git is never touched).
The project's .gitignore is respected; build output, dependencies and model files are skipped.
"""
import hashlib
import os
import subprocess
from pathlib import Path

ROOT = Path.home() / ".ink" / "checkpoints"
EXCLUDE = """node_modules/
.git/
__pycache__/
.venv/
venv/
dist/
build/
.next/
.cache/
target/
*.gguf
*.bin
*.safetensors
*.pt
*.onnx
*.zip
*.7z
*.iso
*.exe
*.dll
*.mp4
*.mkv
"""


class Checkpoints:
    def __init__(self, cwd):
        self.cwd = Path(cwd).resolve()
        self.dir = ROOT / hashlib.sha1(str(self.cwd).lower().encode()).hexdigest()[:16]
        home = Path.home().resolve()
        # whole home folders or drive roots are too big to snapshot every turn
        self.enabled = self.cwd != home and self.cwd.parent != self.cwd and self.cwd not in home.parents
        self.error = None if self.enabled else "checkpoints are off in your home folder or a drive root"
        if os.environ.get("INK_NO_CHECKPOINTS"):
            self.enabled, self.error = False, "checkpoints are off (INK_NO_CHECKPOINTS)"

    def _git(self, *args, timeout=60):
        env = dict(os.environ, GIT_AUTHOR_NAME="ink", GIT_AUTHOR_EMAIL="ink@localhost", GIT_COMMITTER_NAME="ink",
                   GIT_COMMITTER_EMAIL="ink@localhost", GIT_TERMINAL_PROMPT="0")
        r = subprocess.run(["git", f"--git-dir={self.dir}", f"--work-tree={self.cwd}", "-c", "core.autocrlf=false",
                            "-c", "core.safecrlf=false", *args], cwd=self.cwd, env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
        if r.returncode:
            raise RuntimeError((r.stderr or r.stdout).strip()[:300])
        return r.stdout

    def _init(self):
        if (self.dir / "HEAD").exists():
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "--bare", str(self.dir)], capture_output=True, check=True)
        (self.dir / "info").mkdir(exist_ok=True)
        (self.dir / "info" / "exclude").write_text(EXCLUDE, "utf-8")
        (self.dir / "ink-project.txt").write_text(str(self.cwd), "utf-8")

    def snapshot(self, label):
        """Commit the current state of the project. Returns the short id, or None if checkpoints are off/failed."""
        if not self.enabled:
            return None
        try:
            self._init()
            self._git("add", "-A", ".", timeout=90)
            self._git("commit", "-q", "--allow-empty", "--no-verify", "-m", label[:200])
            return self._git("rev-parse", "--short", "HEAD").strip()
        except subprocess.TimeoutExpired:
            self.enabled, self.error = False, "project too big to snapshot quickly; checkpoints off for this session"
        except Exception as e:
            self.error = f"checkpoint failed: {e}"
        return None

    def list(self, n=15):
        if not (self.dir / "HEAD").exists():
            return []
        try:
            log = self._git("log", f"-n{n}", "--format=%h\t%cr\t%s")
        except Exception:
            return []
        return [l.split("\t", 2) for l in log.splitlines() if l.count("\t") == 2]

    def diff_since(self, ref, limit=40000):
        """Unified diff of the project now vs checkpoint `ref` (new files included). '' if unavailable."""
        if not self.enabled or not ref:
            return ""
        try:
            self._git("add", "-A", ".", timeout=90)
            return self._git("diff", "--cached", "--no-color", "-U3", ref, timeout=60)[:limit]
        except Exception:
            return ""

    def rewind(self, ref):
        """Restore the project to checkpoint `ref` (an id, or HEAD~n). The current state is saved first, so a rewind
        can itself be undone. Files created after the checkpoint are deleted. Returns (restored, deleted, saved_id)."""
        target = self._git("rev-parse", "--verify", "-q", ref + "^{commit}").strip()
        saved = self.snapshot(f"before rewind to {ref}")
        if not saved:
            raise RuntimeError(self.error or "could not save the current state; nothing was changed")
        added = [l for l in self._git("diff", "--name-only", "--no-renames", "--diff-filter=A", target, "HEAD").splitlines() if l]
        changed = [l for l in self._git("diff", "--name-only", "--no-renames", target, "HEAD").splitlines() if l]
        for rel in added:
            p = self.cwd / rel
            if p.is_file() or p.is_symlink():
                p.unlink()
        restore = [c for c in changed if c not in set(added)]
        if restore:
            self._git("checkout", target, "--", *restore, timeout=120)
        for rel in added:  # drop now-empty folders the turn created
            d = (self.cwd / rel).parent
            while d != self.cwd and d.is_dir() and not any(d.iterdir()):
                d.rmdir()
                d = d.parent
        return len(restore), len(added), saved
