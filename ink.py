"""ink - a local coding agent for the terminal, tuned for RTX 4060 8GB + Ryzen 7 5700X3D + 32GB.

Brain: Inkling 2 Mini (smart, 20/20 on the coding eval) or Ornith 35B MiniPlus (fast), served by llama.cpp.
Speed design for a ~44 tok/s prefill model:
  * lean system prompt + short tool schemas; tool output is capped
  * history is append-only and reasoning is sent back verbatim, so every turn reuses the server's KV cache
  * reasoning effort is fixed per conversation (changing it re-reads the whole history)
  * the system prompt KV is pre-computed while you type and saved to disk (instant first turn next time)
usage: ink [prompt] [-p prompt] [--fast] [--effort low] [--yolo] [--continue] [--stop]
"""
import os, sys, json, re, time, shutil, subprocess, threading, queue, hashlib, difflib, http.client
import datetime, platform, fnmatch, argparse, socket
from pathlib import Path

from checkpoints import Checkpoints
from recall import search as recall_search, index_session
from checks import quick_check, run_checks, project_checks, ts_errors
from repomap import repo_map
from safety import outward, secret_scan
from hooks import Hooks
try:  # semantic diagnostics after edits: broken imports/exports, undefined calls, Solidity events/modifiers
    from lsp_lite import diagnose as lsp_diagnose
except Exception:
    lsp_diagnose = None
try:  # background processes: dev servers, hardhat node, watchers
    from jobs import Jobs, JOBS_SCHEMA, run_job_tool
except Exception:
    Jobs, JOBS_SCHEMA, run_job_tool = None, None, None
import tempfile

def tempfile_dir():
    return tempfile.gettempdir()
try:  # browser-game playtesting (headless Chrome over CDP); optional
    from playtest import run_playtest, TOOL_SCHEMA as PLAYTEST_SCHEMA
except Exception:
    run_playtest, PLAYTEST_SCHEMA = None, None

IS_WIN = os.name == "nt"
if IS_WIN:
    import msvcrt
    os.system("")  # enable ANSI escape processing
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

APP_DIR = Path(__file__).resolve().parent
TUNE = APP_DIR.parent
STATE = Path.home() / ".ink"
SLOT_DIR = STATE / "slots"
SESS_DIR = STATE / "sessions"
BACKUP_DIR = STATE / "backups"
UNDO_DIR = STATE / "undo"
LOG_FILE = STATE / "ink.log"
CFG_FILE = STATE / "config.json"
MEMORY_DIR = STATE / "memory"
RECALL_DB = STATE / "recall.db"

def _config():
    try:
        return json.loads(CFG_FILE.read_text("utf-8")) if CFG_FILE.exists() else {}
    except Exception:
        return {}

CONFIG = _config()

def _cfg(key, default):
    """env INK_<KEY> or ~/.ink/config.json key or default - lets you retune paths/flags without editing code."""
    return os.environ.get("INK_" + key.upper()) or CONFIG.get(key) or default

def log(msg):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception:
        pass

def _first(*paths):
    """The first path that exists (installer layout first, older manual setups after), else the first one."""
    for c in paths:
        if c and Path(c).exists():
            return str(c)
    return str(paths[0])

_EXE_NAME = "llama-server.exe" if os.name == "nt" else "llama-server"
LLAMA_DIR = Path(_cfg("llama_dir", str(Path.home() / ".ink" / "bin" / "llama")))   # install.ps1 puts ink's build here
MODELS_DIR = Path(_cfg("models_dir", str(Path.home() / ".ink" / "models")))
_HERMES = Path.home() / "AppData" / "Local" / "hermes"
LLAMA_EXE = _first(LLAMA_DIR / _EXE_NAME, TUNE / "llamacpp-b12728" / "bin" / _EXE_NAME)
HERMES_EXE = _cfg("fast_exe", _first(LLAMA_DIR / _EXE_NAME, _HERMES / "tools" / "llamacpp-cuda-11671-win32-x64" / _EXE_NAME))
OFFICIAL_EXE = _cfg("smart_exe", LLAMA_EXE)

# context caps: prefill dominates latency here (~77 tok/s for Inkling), so every tool-result token costs ~13 ms
READ_LINES = int(_cfg("read_lines", 150))
READ_CHARS = int(_cfg("read_chars", 16000))
GREP_MATCHES = int(_cfg("grep_matches", 40))
GREP_PER_FILE = int(_cfg("grep_per_file", 20))
GLOB_SHOW = int(_cfg("glob_show", 80))
RUN_CHARS = int(_cfg("run_chars", 5000))
MAX_TOKENS = int(_cfg("max_tokens", 16384))
COMPACT_TOKENS = int(_cfg("compact_tokens", 1200))
# pinned brief: goal + key points + the user's requests + plan, re-shown so they survive long work and compaction
PIN_EVERY = int(_cfg("pin_every_steps", 8))         # re-show the brief after this many tool rounds...
PIN_TOKENS = int(_cfg("pin_every_tokens", 6000))    # ...or after the context grew this much
VERIFY = bool(_cfg("verify", True))                 # self-check against the goal before finishing a turn with edits
DREAM = bool(_cfg("dream", True))                   # reflect on a session after /exit and write lessons to memory
# LLMLingua-2 prose compression (CPU) for long non-code tool output; needs .venv-compress (see compress_server.py)
COMPRESS_PORT = int(_cfg("compress_port", 18620))
COMPRESS_MIN = int(_cfg("compress_min_chars", 6000))
COMPRESS_RATE = float(_cfg("compress_rate", 0.45))
COMPRESS_PY = APP_DIR / ".venv-compress" / "Scripts" / "python.exe"
# run-based verification: syntax-check every edited file at once; before finishing a turn with edits, run the
# project's real checks (tests / hardhat compile+test / lint) for the dirs that changed and make the model fix failures
AUTO_CHECKS = bool(_cfg("auto_checks", True))
CHECK_ROUNDS = int(_cfg("check_rounds", 3))         # max check->fix rounds per turn
CHECK_TIMEOUT = int(_cfg("check_timeout", 300))
GUIDES_DIR = APP_DIR / "guides"
AUTO_REVIEW = str(_cfg("auto_review", "on"))      # on | off: fresh-context review of each turn's diff
REVIEW_MIN_LINES = int(_cfg("review_min_lines", 12))
DREAM_EVERY = int(_cfg("dream_every_turns", 10))   # Hermes-style memory review during long sessions (when idle)

COMMON = ["--host", "127.0.0.1", "--jinja", "-np", "1", "--no-webui", "--load-mode", "none",
          "-ctk", "q8_0", "-ctv", "q8_0", "-fa", "on", "-ngl", "99", "--slot-save-path", str(SLOT_DIR)]
PROFILES = {
    "smart": dict(
        title="Inkling 2 Mini", port=18610, ctx=int(_cfg("smart_ctx", 32768)), exe=OFFICIAL_EXE, effort=str(_cfg("smart_effort", "medium")),
        # APEX-I MiniPlus V2.1 (IsValorum, 13.0 GB): looped-layer experts are 430 MiB/layer, so ALL of layers 6-17
        # (36 of 48 layer visits per token) fit on an 8 GB card. Measured 2026-11-26 (b12728, 32k, -ub 256):
        # 27.5 gen / 139-145 prefill tok/s vs IQ4_NL 19.3 / 76 (and 85 at -ub 256); coding eval 20/20 at effort low,
        # same as IQ4_NL. With all looped layers on the GPU op-offload no longer hits bug #32910, but it measured slower
        # (88 tok/s: copying the CPU-resident experts per batch costs more than CPU compute), so it stays off.
        # DSpark draft head: 13.7 tok/s (60% accepted, verification with CPU experts too expensive) -> off.
        model=_cfg("smart_model", _first(MODELS_DIR / "Inkling-2-Mini-APEX-I-MiniPlus-V2.1.gguf",
                                          TUNE / "models" / "inkling2-mini-apex" / "Inkling-2-Mini-APEX-I-MiniPlus-V2.1.gguf",
                                          MODELS_DIR / "Inkling-2-Mini-IQ4_NL.gguf",
                                          TUNE / "models" / "inkling2-mini" / "Inkling-2-Mini-IQ4_NL.gguf")),
        # Older notes (IQ4_NL) - llama.cpp bug #32910 workaround: CPU prefill (--no-op-offload).
        # Layers 6-17 run 3x per token, so their experts are worth 3x per GB of VRAM. 32k ctx + -ub 64 shrink KV and
        # compute buffers enough to fit looped layers 6-13 on the GPU (~7.5 of 8.2 GB; 6-14 spills to shared memory).
        # Measured 2026-11-25 (b12728): 21.0 gen / 77 prefill tok/s vs 14.2 / 45 with 64k, -ub 256, layer 6 only.
        # Tried and rejected: IQ3_XXS core (27 tok/s but failed eval problems), DSpark drafter (no gain: 19.0 vs 19.0).
        # --ctx-checkpoints 4: each SWA checkpoint is ~88MB of RAM (and disk in saved sessions); the default kept 26
        # in one short session (2.3GB). An append-only agent only ever rolls back to the last message or two.
        args=["--alias", "inkling-local", "--ctx-checkpoints", "4", "-t", "8", "-tb", "16", "-b", "2048", "-ub", str(_cfg("smart_ubatch", 256)), "--no-op-offload",
              # No n-gram speculation: +46% on a temp-0 copy benchmark, but in real agent runs at temp 1.0 only
              # 20% of drafts were accepted (they fire during reasoning, not just copying) -> 10.8 vs 12.8 tok/s.
              "--temp", "1.0", "--top-p", "1.0", "--top-k", "0", "--min-p", "0"],
        # Memory layouts, fastest first; ensure_server tries them until one loads without spilling VRAM and remembers
        # the winner (~/.ink/fit.json). KV is ~17 KB/token (only 4 of 24 layers are full-attention, x3 for the looped
        # ones): 0.57 GB at 32k fits next to experts 6-13; ~4.2 GB at 244k does not -> KV in RAM (-nkvo), which frees
        # VRAM for more experts but makes generation slow down as the context fills (CPU attention).
        fits=[([], 32768, r"6-17"), ([], 32768, r"6-15"), ([], 32768, r"6-13"), ([], 32768, r"6-11"),
              (["-nkvo"], None, r"6-17"), (["-nkvo"], None, r"6-14"), (["-nkvo"], None, r"6-13"), (["-nkvo"], None, r"6-11")]),
    "fast": dict(
        title="Ornith 35B MiniPlus", port=18600, ctx=int(_cfg("fast_ctx", 249856)), exe=HERMES_EXE, effort=str(_cfg("fast_effort", "high")),
        model=_cfg("fast_model", _first(MODELS_DIR / "Ornith-1.5-35B-A3B.APEX-I-MiniPlus-V2.1.gguf",
                                         _HERMES / "models" / "Ornith-1.5-35B-A3B.APEX-I-MiniPlus-V2.1.gguf")),
        # measured on this box at 128k: ~1,186 tok/s prefill, ~36 tok/s gen (ncmoe33-ub4k), 16/20 on the coding eval
        args=["--alias", "ornith-local", "--chat-template-file", _first(APP_DIR / "templates" / "ornith.jinja", TUNE / "ornith-template.jinja"), "-t", "8",
              "--temp", "0.6", "--top-k", "20", "--top-p", "0.95", "--min-p", "0"],
        # KV ~11 KB/token (10 of 41 layers are attention): 1.4 GB at 128k, 2.7 GB at 244k, plus a bigger -ub compute
        # buffer -> more expert layers on the CPU, or a smaller ubatch, or KV in RAM as the last resort.
        fits=[(["--n-cpu-moe", "33", "-b", "4096", "-ub", "4096"], 131072, None),
              (["--n-cpu-moe", "37", "-b", "4096", "-ub", "4096"], None, None),
              (["--n-cpu-moe", "37", "-b", "2048", "-ub", "2048"], None, None),
              (["--n-cpu-moe", "40", "-b", "2048", "-ub", "2048"], None, None),
              (["-nkvo", "--n-cpu-moe", "33", "-b", "2048", "-ub", "2048"], None, None)]),
}
# 244k for Inkling needs the KV in RAM (-nkvo): measured 4-5 tok/s gen / 16-55 prefill vs ~20 / 73 at 32k on GPU.
PROFILES["smart"]["ctx"] = int(_cfg("smart_ctx", 32768))
FIT_FILE = STATE / "fit.json"
VRAM_SPARE_MB = int(_cfg("vram_spare_mb", 150))  # less free VRAM than this after loading = spilled to shared memory
WRITE_IN_HOME = bool(_cfg("write_anywhere_in_home", True))
HOME_PRIVATE = {"appdata", "ntuser.dat"}  # plus every dot-folder (.ssh, .aws, .claude, .ink config ...)
EFFORTS = ["none", "minimal", "low", "medium", "high", "max"]

# ---------------------------------------------------------------- terminal styling
def _c(code):
    return lambda s: f"\x1b[{code}m{s}\x1b[0m"
DIM, BOLD, ITAL = _c("2"), _c("1"), _c("3")
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, GRAY = _c("31"), _c("32"), _c("33"), _c("34"), _c("35"), _c("36"), _c("90")
ACCENT = _c("38;5;209")
CLR = "\r\x1b[2K"

ANSI_RX = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\r")

class RpcIO:
    """--rpc mode (see PROTOCOL.md): events out as JSON lines on the real stdout; stray prints go to stderr.
    Plain out() text becomes `status` (spinner frames) or `notice` lines - or, while a tool runs, is captured
    as that tool's summary/preview."""
    def __init__(self):
        self.real = sys.stdout
        sys.stdout = sys.stderr  # nothing but protocol on the pipe
        # the protocol's stdin must not leak into child processes (git, npm, hooks would block on it or eat
        # UI commands): keep a private handle for the reader, give fd 0 to /dev/null
        try:
            self.inp = os.fdopen(os.dup(0), "r", encoding="utf-8", errors="replace")
            nul = os.open(os.devnull, os.O_RDONLY)
            os.dup2(nul, 0)
            os.close(nul)
            sys.stdin = open(os.devnull, encoding="utf-8")
        except Exception:
            self.inp = sys.stdin
        self.lock = threading.Lock()
        self.capture = None      # list collecting plain lines during a tool call
        self.last_status = 0.0

    def send(self, type_, **kw):
        kw["type"] = type_
        line = json.dumps(kw, ensure_ascii=False)
        with self.lock:
            try:
                self.real.write(line + "\n")
                self.real.flush()
            except Exception:
                pass

    def plain(self, s, end):
        status = s.startswith(CLR) or (end == "" and "\n" not in s)
        text = ANSI_RX.sub("", s)
        if status:
            now = time.time()
            if text.strip() and now - self.last_status < 0.25:
                return
            self.last_status = now
            self.send("status", text=text.strip())
            return
        for line in text.splitlines() or [""]:
            if self.capture is not None:
                self.capture.append(line)
            elif line.strip():
                lvl = "error" if "\x1b[31m" in s else "warn" if "\x1b[33m" in s else "info"
                self.send("notice", level=lvl, text=line.rstrip())

RPC = None  # RpcIO in --rpc mode

TRACE_DIR = Path.home() / ".ink" / "trace"
TRACE = {"stem": None}
TRACE_SKIP = {"text_delta", "thinking_delta", "status", "completions", "diff", "hello"}

def trace(ev):
    """Live trace of a session (~/.ink/trace/<session>.jsonl) for watching ink work (ink_watch.py, INK-track.md)."""
    if not TRACE["stem"] or not TRACE_ON:
        return
    try:
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        with open(TRACE_DIR / f"{TRACE['stem']}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.strftime("%H:%M:%S"), **ev}, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass

def emit(type_, **kw):
    if type_ not in TRACE_SKIP:
        trace({"type": type_, **kw})
    if RPC:
        RPC.send(type_, **kw)

def out(s="", end="\n"):
    if RPC:
        RPC.plain(s, end)
        return
    sys.stdout.write(s + end)
    sys.stdout.flush()

def short(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"

def ktok(n):
    return f"{n/1000:.1f}k" if n >= 1000 else str(n)

SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# ---------------------------------------------------------------- server management
def health(port):
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        c.request("GET", "/health")
        r = c.getresponse()
        ok = r.status == 200 and json.loads(r.read() or b"{}").get("status") == "ok"
        c.close()
        return ok
    except Exception:
        return False

def port_busy(port):
    s = socket.socket()
    s.settimeout(0.3)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()

def find_servers(model_path):
    """PIDs of llama-server processes that serve the given model file."""
    if not IS_WIN:
        return []
    name = Path(model_path).name
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" | "
          f"Where-Object {{ $_.CommandLine -like '*{name}*' }} | ForEach-Object {{ $_.ProcessId }}")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=20)
        return [int(x) for x in r.stdout.split() if x.strip().isdigit()]
    except Exception:
        return []

def kill(pid):
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)

DREAM_PID = Path.home() / ".ink" / "dream.pid"

def stop_dream(why=""):
    """A background dream (memory review after a session) occupies the single model slot; anything the user
    is waiting for comes first. Killing it closes its HTTP request, so llama-server cancels the task."""
    try:
        pid = int(DREAM_PID.read_text().strip())
    except Exception:
        return False
    if pid == os.getpid():
        return False
    alive = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    if str(pid) in alive and "python" in alive.lower():
        kill(pid)
        log(f"stopped background dream {pid} ({why})")
        time.sleep(0.5)
    DREAM_PID.unlink(missing_ok=True)
    return True

def stop_ollama():
    try:
        c = http.client.HTTPConnection("127.0.0.1", 11434, timeout=2)
        c.request("GET", "/api/ps")
        for m in json.loads(c.getresponse().read()).get("models", []):
            subprocess.run(["ollama", "stop", m["name"]], capture_output=True, timeout=30)
    except Exception:
        pass

def server_slot(port):
    """(n_ctx, busy) of the running server's slot 0, or (None, False)."""
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        c.request("GET", "/slots")
        j = json.loads(c.getresponse().read() or b"[]")
        c.close()
        return (j[0].get("n_ctx"), bool(j[0].get("is_processing"))) if j else (None, False)
    except Exception:
        return None, False

def vram_free_mb():
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
        used, total = [int(x) for x in r.stdout.strip().splitlines()[0].split(",")]
        return total - used
    except Exception:
        return None

def _experts_ot(rng):
    a, b = (int(x) for x in rng.split("-"))
    return ["-ot", r"blk\.(" + "|".join(str(i) for i in range(a, b + 1)) + r")\.ffn_.*_exps.*=CUDA0,ffn_.*_exps.*=CPU"]

def fit_candidates(key):
    """Memory layouts that can hold this profile's context, the remembered winner first."""
    p = PROFILES[key]
    cands = []
    for extra, max_ctx, rng in p.get("fits", [([], None, None)]):
        if max_ctx is None or p["ctx"] <= max_ctx:
            cands.append(list(extra) + (_experts_ot(rng) if rng else []))
    try:
        won = json.loads(FIT_FILE.read_text("utf-8")).get(f"{key}|{Path(p['model']).name}|{p['ctx']}")
        if won in cands:
            cands.remove(won)
            cands.insert(0, won)
    except Exception:
        pass
    return cands

def remember_fit(key, args):
    p = PROFILES[key]
    try:
        d = json.loads(FIT_FILE.read_text("utf-8")) if FIT_FILE.exists() else {}
        d[f"{key}|{Path(p['model']).name}|{p['ctx']}"] = args
        FIT_FILE.write_text(json.dumps(d, indent=1), "utf-8")
    except Exception:
        pass

def ensure_server(key):
    """Start (or reuse) the model server. Self-tuning: tries memory layouts until one loads without spilling VRAM,
    remembers it, and falls back to a smaller context if nothing fits - no flags or commands needed."""
    p = PROFILES[key]
    if health(p["port"]):
        n_ctx, busy = server_slot(p["port"])
        if n_ctx in (None, p["ctx"]):
            return True
        if busy:  # another ink session is mid-request on it: don't pull the rug, use it as it is
            out(GRAY(f"(server is busy with another session; using its {ktok(n_ctx)} context for now)"))
            p["ctx"] = n_ctx
            return True
        out(GRAY(f"restarting {p['title']}: context {ktok(n_ctx)} → {ktok(p['ctx'])}"))
        for pid in find_servers(p["model"]):
            kill(pid)
        time.sleep(2)
    if not Path(p["exe"]).is_file():
        out(f"{RED('llama-server not found:')} {p['exe']}")
        out(GRAY(f"  run install.ps1 in {APP_DIR} to install it, or set smart_exe/fast_exe in {CFG_FILE}"))
        return False
    if not Path(p["model"]).is_file():
        out(f"{RED('model not found:')} {p['model']}")
        out(GRAY(f"  run install.ps1 in {APP_DIR} to download the models, or set smart_model/fast_model in {CFG_FILE}"))
        return False
    if port_busy(p["port"]):  # our server is still loading (started by another ink): wait for it
        return _start_server(key, None)
    cands = fit_candidates(key)
    for i, extra in enumerate(cands):
        last = i == len(cands) - 1
        if _start_server(key, extra):
            free = vram_free_mb()
            if free is None or free >= VRAM_SPARE_MB or last:
                remember_fit(key, extra)
                if i > 0 or len(cands) > 1 and not FIT_FILE.exists():
                    log(f"fit {key} ctx={p['ctx']}: {extra} (free VRAM {free} MB)")
                return True
            out(GRAY(f"  layout {i + 1}/{len(cands)} spills VRAM ({free} MB free) - trying a lighter one"))
            log(f"fit {key} ctx={p['ctx']}: {extra} spilled, {free} MB free")
        else:
            out(GRAY(f"  layout {i + 1}/{len(cands)} failed to load - trying the next"))
        for pid in find_servers(p["model"]):
            kill(pid)
        time.sleep(2)
    fallback = 32768 if key == "smart" else 131072
    if p["ctx"] > fallback:
        out(YELLOW(f"no layout fits {ktok(p['ctx'])} context - falling back to {ktok(fallback)}"))
        p["ctx"] = fallback
        return ensure_server(key)
    return False

def _start_server(key, extra):
    p = PROFILES[key]
    if extra is not None:
        for q in PROFILES.values():  # only one model fits in 8GB VRAM; also stops copies on other ports
            for pid in find_servers(q["model"]):
                kill(pid)
        time.sleep(1)
        stop_ollama()
        SLOT_DIR.mkdir(parents=True, exist_ok=True)
        logf = open(STATE / f"server-{key}.log", "wb")
        cmd = [p["exe"], "-m", p["model"], "--port", str(p["port"]), "-c", str(p["ctx"])] + COMMON + p["args"] + extra
        flags = 0
        if IS_WIN:
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(cmd, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    else:
        proc = None
    t0 = time.time()
    i = 0
    while time.time() - t0 < 400:
        if health(p["port"]):
            out(CLR, end="")
            return True
        if proc is not None and proc.poll() is not None:  # crashed (e.g. out of memory)
            out(CLR, end="")
            return False
        out(f"{CLR}{ACCENT(SPIN[i % 10])} loading {p['title']} ({ktok(p['ctx'])} ctx)… {int(time.time()-t0)}s", end="")
        i += 1
        time.sleep(0.25)
    out(f"{CLR}{RED('server did not start')} - see {STATE / f'server-{key}.log'}")
    return False

def spawn_detached(cmd, logname):
    """Start a background process that outlives ink (used for the dream step and the compressor)."""
    flags = 0
    if IS_WIN:
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    logf = open(STATE / logname, "ab")
    subprocess.Popen(cmd, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)

def start_compressor():
    """Start the LLMLingua-2 service once (it stays up between ink runs; ~1 GB RAM, no VRAM)."""
    if COMPRESS_PY.is_file() and not port_busy(COMPRESS_PORT):
        spawn_detached([str(COMPRESS_PY), str(APP_DIR / "compress_server.py"), str(COMPRESS_PORT)], "compress.log")

def server_post(port, path, body, timeout=600):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    c.request("POST", path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    r = c.getresponse()
    data = r.read()
    c.close()
    try:
        return r.status, json.loads(data or b"{}")
    except Exception:
        return r.status, {"raw": data[:300]}

# ---------------------------------------------------------------- tools
SHELL_BASH = r"C:\Program Files\Git\bin\bash.exe"
HAVE_BASH = Path(SHELL_BASH).exists() if IS_WIN else True
RG = shutil.which("rg")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", ".cache", "target"}

def tool(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters":
            {"type": "object", "properties": props, "required": req}}}

S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
TOOLS = [
    # Parameter names are chosen so alphabetical order (Inkling's template sorts keys) is the natural writing
    # order: file first, then find, then replace. With path/old_string/new_string the model wrote new before old
    # and sometimes forgot the path after the long strings.
    tool("read_file", "Read a text file (numbered lines). Use offset/limit for big files.",
         {"file": S, "offset": I, "limit": I}, ["file"]),
    tool("edit_file", "In file, replace the exact text `find` with `replace`. `find` must match exactly once "
         "(include surrounding lines to make it unique) unless replace_all is true. OR give lines (e.g. \"40-52\", "
         "the numbers read_file showed) instead of find to replace exactly those lines - refused if the file changed "
         "since you read it. Read the file first.",
         {"file": S, "find": S, "lines": S, "replace": S, "replace_all": B}, ["file", "replace"]),
    tool("write_file", "Create a file, or overwrite it completely, with text.", {"file": S, "text": S}, ["file", "text"]),
    tool("run", ("Run a bash command (Git Bash on Windows; cwd persists between calls). " if HAVE_BASH else
                 "Run a PowerShell command. ") + "Output is truncated. Default timeout 120s.",
         {"command": S, "timeout": I}, ["command"]),
    tool("grep", "Search file contents with a regex (ripgrep). Returns path:line:text.",
         {"pattern": S, "path": S, "glob": S, "ignore_case": B}, ["pattern"]),
    tool("glob", "Find files by glob pattern, e.g. **/*.py. Newest first.", {"pattern": S, "path": S}, ["pattern"]),
    tool("map", "Map the code: files ranked by importance with their functions/classes/contracts and line numbers. "
         "path = a dir (default .) or ONE file for its full outline. query = words to find related files and symbols, "
         "e.g. 'reward stake'. Use it first in unfamiliar code, then read_file only the line ranges you need.",
         {"path": S, "query": S}, []),
    tool("check", "Run the project's checks (tests, contract compile+tests, lint - detected, or .ink/checks.json) and "
         "get failures only. only = run just the checks whose name/command contains this word, e.g. 'contract'.",
         {"only": S}, []),
    tool("web", "Search the web (query) or read a page (url). Returns titles/urls/snippets, or the page as text "
         "(long prose is compressed). Use it for docs, error messages, library APIs you are unsure about.",
         {"query": S, "url": S}, []),
    tool("plan", "Write or revise your task checklist; send the whole list each time. Use it for any task with 3+ "
         "steps, BEFORE starting work. goal: what the user wants and what counts as done. key_points: the user's "
         "requirements, constraints and decisions you must not forget (short, one per item). These are pinned: you "
         "will be reminded of them as you work. Revise as you go: mark steps done, add steps you discover, rewrite "
         "or drop steps when findings change the approach, add key points when you decide something important. "
         "Exactly one step should be 'doing' while working. The last step should verify the result.",
         {"goal": S, "key_points": {"type": "array", "items": S},
          "steps": {"type": "array", "items": {"type": "object", "properties": {
             "step": S, "status": {"type": "string", "enum": ["todo", "doing", "done", "dropped"]}},
             "required": ["step", "status"]}}}, ["steps"]),
    tool("recall", "Search your memory of past sessions and saved notes (keyword search). Use it when the user "
         "refers to earlier work, or you need a detail that was lost when the conversation was compacted.",
         {"query": S}, ["query"]),
    tool("ask_claude", "Ask Claude (a much stronger model, runs via the user's Claude Code) for help with a hard "
         "problem: a bug you can't find, a design decision, code review, unfamiliar tech. It can read the project's "
         "files itself. Give it the full context: goal, what you tried, exact errors, relevant paths. It answers; you "
         "make the changes. Takes 30s-several minutes.", {"question": S}, ["question"]),
    tool("code", "Run Python (full access: files, subprocess, network) with MCP tools as functions. Only "
         "what you print() comes back, with emails/phones/names masked as [EMAIL_1] etc. Preloaded: search_tools(q), "
         "save_skill(name, code, description, usage), list_skills().", {"code": S, "timeout": I}, ["code"]),
]
if PLAYTEST_SCHEMA:
    TOOLS.insert(6, PLAYTEST_SCHEMA)
if JOBS_SCHEMA:
    TOOLS.insert(4, JOBS_SCHEMA)
try:  # subagents: built-in explore/review/test + ~/.ink/agents/*.md + <project>/.ink/agents/*.md
    from agents import agent_schema, load_agents
    TOOLS.append(agent_schema(load_agents(Path.cwd())))
except Exception as _e:
    log(f"agents unavailable: {_e!r}")
# Fully local (user's rule): no Claude or other outside models. ask_claude is only offered if explicitly enabled.
if not _cfg("allow_claude", False):
    TOOLS = [t for t in TOOLS if t["function"]["name"] != "ask_claude"]
READ_ONLY = {"read_file", "grep", "glob", "plan", "ask_claude", "recall", "map", "playtest", "web", "agent"}

# expert guides (guides/*.md): suggested once per session when the work touches their area
GUIDE_TRIGGERS = [
    ("solidity-security.md", lambda path, text: path.endswith(".sol") or "hardhat" in path),
    ("web3-frontend.md", lambda path, text: bool(re.search(r"from ['\"](wagmi|viem|ethers)", text or ""))),
    ("game-architecture.md", lambda path, text: bool(re.search(r"from ['\"][^'\"]*three|requestAnimationFrame|THREE\.", text or ""))),
    ("typescript.md", lambda path, text: path.endswith((".ts", ".tsx")) and not path.endswith(".d.ts")),
    ("real-money-games.md", lambda path, text: bool(re.search(r"\b(bet|wager|payout|stake|jackpot|cashout|cash.?mode)\b", text or "", re.I))),
]
REQUEST_TRIGGERS = [
    ("game-design.md", re.compile(r"\b(mechanic|game ?feel|juice|gameplay|level design|difficulty|progression|combo|"
                                  r"controls?|fun|player experience|onboarding)", re.I)),
    ("new-project.md", re.compile(r"(\bfrom scratch\b|\bnew\b(?:\s+[\w-]+){0,3}\s+(project|game|app|site|website|dapp)\b|"
                                  r"\b(build|create|make|start|scaffold|set ?up)\b(?:\s+[\w-]+){0,5}\s+"
                                  r"(project|game|app|site|website|dapp|platform|clone|mvp)\b)", re.I)),
    ("stack.md", re.compile(r"(\bfrom scratch\b|\bnew\b(?:\s+[\w-]+){0,3}\s+(project|game|app|site|dapp)\b|\b(which|what) (library|framework|stack)\b|"
                            r"\b(latest|modern|best) (tech|technolog|stack|librar|framework)|\bupgrade\b|\bmigrate\b)", re.I)),
    ("frontend-ui.md", re.compile(r"\b(ui|ux|screen|page|menu|hud|lobby|landing|dashboard|design|restyle|style|css|"
                                  r"layout|responsive|animation|modal|button|theme|frontend|front-end)\b", re.I)),
    ("backend-api.md", re.compile(r"\b(server|api|backend|back-end|websocket|realtime|multiplayer|database|db|endpoint|"
                                  r"auth|login|siwe|ledger|postgres|redis|cash ?mode)\b", re.I)),
    ("testing.md", re.compile(r"\b(tests?|testing|coverage|e2e|playwright|vitest|fuzz|flaky)\b", re.I)),
    ("delivery.md", re.compile(r"\b(deploy|deployment|readme|ci|release|ship|publish|render|vercel|production|docker)\b", re.I)),
    ("real-money-games.md", re.compile(r"\b(casino|blackjack|poker|roulette|slots?|dice|crash game|bet|betting|"
                                       r"wager|cash ?mode|real.money|provably.fair|escrow|jackpot|lottery)", re.I)),
    ("game-economy.md", re.compile(r"\b(economy|tokenomics|emission|apr|apy|reward rate|sink|faucet|inflation|"
                                   r"play.to.earn|p2e|staking reward)", re.I)),
]
SAFE_CMD = re.compile(r"^\s*(ls|pwd|cat|head|tail|wc|echo|which|type|file|stat|du|df|tree|rg|grep|find|"
                      r"git (status|diff|log|show|branch|remote|rev-parse|ls-files|blame)|"
                      r"python3? --version|node --version|npm (ls|--version)|dir|Get-ChildItem|Get-Content)\b")
# stricter subset allowed in plan mode: inspection only, no find -exec/-delete, no redirection/pipes
PLAN_SAFE = re.compile(r"^\s*(ls|pwd|cat|head|tail|wc|which|stat|du|df|tree|rg|grep|"
                       r"git (status|diff|log|show|branch|remote|rev-parse|ls-files|blame)|"
                       r"dir|Get-ChildItem|Get-Content)\b")

_SRC = r"[\w./\\*-]+\.(py|pyi|js|jsx|ts|tsx|mjs|cjs|json|md|txt|toml|ya?ml|ini|cfg|html|css|scss|c|h|cc|cpp|hpp|rs|go|java|kt|cs|rb|php|sh|ps1|sql|vue|svelte|lua|swift)\b"
SHELL_WRITE = re.compile(r"(cat|echo|printf|tee)\b[^|]*(>>?|\btee\b)\s*['\"]?" + _SRC + r"|<<-?\s*['\"]?\w+['\"]?[^\n]*\n[\s\S]*(open\(|write)|"
                         r"\bsed\s+(-\w*\s+)*-i|\bperl\s+-[\w]*i|python3?\s+-c\s+.*(open\(|\.write)|>\s*['\"]?" + _SRC + r"\s*<<")
SHELL_READ = re.compile(r"(^|&&|;|\|\||\n)\s*(cat|head|tail|less|more|type)\s+(-\S+\s+)*['\"]?" + _SRC + r"['\"]?(\s+['\"]?" + _SRC + r"['\"]?)*\s*($|&&|;|\n|2>)")

def shell_misuse(cmd):
    """Steer the model to the dedicated tools (they show diffs, ask permission and keep encodings)."""
    if SHELL_WRITE.search(cmd):
        return ("Error: don't modify files through the shell. Use edit_file for changes (or write_file for new files) "
                "so the user sees a diff. Nothing was run.")
    return None

def shell_read_files(cmd):
    """Files a cat/head/tail command reads: counted as read, so edits after a shell read don't bounce."""
    if not SHELL_READ.search(cmd):
        return []
    return [m.group(0) for m in re.finditer(_SRC, cmd)]

# Refused even with auto-approve on: these destroy data irreversibly, so a mistaken model call must not run them.
DANGER_PATTERNS = [
    (re.compile(r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+/(?:\s|$|\*)"), "recursive delete of /"),
    (re.compile(r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+~(?:\s|$|/(?:\s|$))"), "recursive delete of your home directory"),
    (re.compile(r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+[a-zA-Z]:[\\/](?:\s|$|\*)"), "recursive delete of a drive"),
    (re.compile(r"\bmkfs(?:\.\w+)?\b"), "filesystem format"),
    (re.compile(r"\b(?:diskpart|fdisk)\b"), "disk partitioning"),
    (re.compile(r"\bformat\s+[a-zA-Z]:"), "drive format"),
    (re.compile(r"\bdd\b[^\n]*\bof\s*=\s*/dev/"), "raw write to a device"),
    (re.compile(r"\bdel\s+(?:/[a-zA-Z]\s+)*[a-zA-Z]:[\\/]?(?:\s|$|\*)", re.I), "delete from a drive root"),
    (re.compile(r"\brd\s+/s\s+/q\s+[a-zA-Z]:[\\/]?(?:\s|$|\*)", re.I), "remove a drive"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r"\b(?:shutdown|reboot)\b", re.I), "power off the machine"),
]

def dangerous_cmd(cmd):
    for rx, why in DANGER_PATTERNS:
        if rx.search(cmd):
            return why
    return None

ERR_LINE = re.compile(r"error|exception|traceback|failed|fatal|warning", re.I)

def prose_like(s):
    """True for natural-language text (docs, web pages); False for code, logs, tables, JSON and test output,
    which lossy compression would break."""
    sample = s[:20000]
    symbols = sum(sample.count(c) for c in "{}[]();=<>_/\\|$#*`\"'")
    words = re.findall(r"[A-Za-z]{2,}", sample)
    return symbols / max(len(sample), 1) < 0.025 and len(" ".join(words)) / max(len(sample), 1) > 0.6

def trunc_middle(s, limit):
    if len(s) <= limit:
        return s
    head = limit // 3
    tail = limit - head
    return s[:head] + f"\n\n… [{len(s) - limit} chars truncated] …\n\n" + s[-tail:]

class Tools:
    def __init__(self, cwd):
        self.cwd = Path(cwd)
        self.read_paths = set()
        self.read_hash = {}  # path -> sha1 of the file when the model last read it (line edits check staleness)
        self.shift = {}      # path -> (after_line, delta) from the model's own line edits since that read
        self.seen = {}  # dedup: (path, offset, limit) -> (content hash, times stubbed) of reads still in the context

    def forget(self):
        """The model's context lost the file contents (compaction / rewind): reads must return full text again."""
        self.read_paths.clear()
        self.seen.clear()
        self.read_hash.clear()
        self.shift.clear()

    def path(self, p):
        p = os.path.expanduser(str(p).strip().strip('"'))
        if IS_WIN and re.match(r"^/[a-zA-Z]/", p):  # git-bash style /c/Users/...
            p = p[1] + ":" + p[2:]
        q = Path(p)
        return (q if q.is_absolute() else self.cwd / q).resolve()

    def rel(self, p):
        try:
            return str(Path(p).relative_to(self.cwd))
        except ValueError:
            return str(p)

    # -- read
    def read_file(self, path, offset=1, limit=READ_LINES):
        p = self.path(path)
        if p.is_dir():
            items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:200]
            return "\n".join(x.name + ("/" if x.is_dir() else "") for x in items), f"{len(items)} entries"
        if not p.exists():
            raise ToolError(f"File not found: {p}")
        raw = p.read_bytes()
        if b"\x00" in raw[:8000]:
            raise ToolError(f"{p} is a binary file")
        lines = raw.decode("utf-8", "replace").splitlines()
        offset = max(1, int(offset or 1))
        limit = max(1, int(limit or READ_LINES))
        chunk = lines[offset - 1: offset - 1 + limit]
        body, size = [], 0
        for i, l in enumerate(chunk, offset):
            l = l if len(l) <= 400 else l[:400] + "…"
            size += len(l) + 8
            if size > READ_CHARS:
                body.append(f"… stopped at line {i} (output cap); continue with offset={i}")
                break
            body.append(f"{i:>5}\t{l}")
        self.read_paths.add(str(p))
        self.read_hash[str(p)] = hashlib.sha1(raw).hexdigest()
        self.shift.pop(str(p), None)
        end = offset + len(chunk) - 1
        if end < len(lines) and not body[-1].startswith("…"):
            body.append(f"… {len(lines) - end} more lines (file has {len(lines)}); continue with offset={end + 1}")
        if not lines:
            body = ["(empty file)"]
        text = "\n".join(body)
        # dedup: the same unchanged range is already in the context -> a one-line stub instead of the whole text
        # (append-only, so the KV cache stays valid). Asked again right after a stub -> full text, never stuck.
        key = (str(p), offset, limit)
        h = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
        prev = self.seen.get(key)
        if prev and prev[0] == h and prev[1] == 0 and len(text) > 600:
            self.seen[key] = (h, 1)
            return (f"[unchanged since you last read {self.rel(p)} lines {offset}-{end}: that content is already above "
                    "in this conversation, so it was not sent again. If you can't find it, call read_file again "
                    "and you will get the full text.]"), "unchanged (dedup)"
        self.seen[key] = (h, 0)
        return text, f"Read {len(chunk)} lines" + (f" (of {len(lines)})" if len(chunk) < len(lines) else "")

    def grep(self, pattern, path=".", glob=None, ignore_case=False):
        p = self.path(path or ".")
        lines = []
        if RG:
            cmd = [RG, "--line-number", "--no-heading", "--color", "never", "--max-columns", "200",
                   "--max-columns-preview", "--max-count", str(GREP_PER_FILE)]
            if ignore_case:
                cmd.append("-i")
            if glob:
                cmd += ["--glob", glob]
            cmd += ["-e", pattern, str(p)]
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=self.cwd)
            if r.returncode == 2 and r.stderr:
                raise ToolError(r.stderr.strip()[:500])
            lines = r.stdout.splitlines()
        else:
            rx = re.compile(pattern, re.I if ignore_case else 0)
            files = [p] if p.is_file() else (f for f in p.rglob("*") if f.is_file() and not (set(f.parts) & SKIP_DIRS))
            for f in files:
                if glob and not fnmatch.fnmatch(f.name, glob):
                    continue
                try:
                    for n, l in enumerate(f.read_text("utf-8", "strict").splitlines(), 1):
                        if rx.search(l):
                            lines.append(f"{f}:{n}:{l[:300]}")
                except Exception:
                    continue
                if len(lines) > 400:
                    break
        lines = [self._relline(l) for l in lines]
        n = len(lines)
        if n > GREP_MATCHES:
            lines = lines[:GREP_MATCHES] + [f"… {n - GREP_MATCHES} more matches; narrow the pattern or path"]
        return ("\n".join(lines) or "No matches."), f"{n} matches"

    def _relline(self, l):
        cwd = str(self.cwd) + os.sep
        return l[len(cwd):] if l.startswith(cwd) else l

    def glob(self, pattern, path="."):
        p = self.path(path or ".")
        pat = pattern.replace("\\", "/")
        hits = []
        for f in p.glob(pat):
            if set(f.relative_to(p).parts) & SKIP_DIRS:
                continue
            hits.append(f)
            if len(hits) > 2000:
                break
        hits.sort(key=lambda f: f.stat().st_mtime if f.exists() else 0, reverse=True)
        n = len(hits)
        res = [self.rel(f) + ("/" if f.is_dir() else "") for f in hits[:GLOB_SHOW]]
        if n > GLOB_SHOW:
            res.append(f"… {n - GLOB_SHOW} more")
        return ("\n".join(res) or "No files found."), f"{n} files"

    # -- write
    def _check_read(self, p):
        if p.exists() and str(p) not in self.read_paths:
            raise ToolError(f"Read {p.name} with read_file before modifying it.")

    def line_edit(self, path, lines, new_string):
        """Replace lines a-b (1-based, inclusive) - hashline-style: refused if the file changed since the read."""
        p = self.path(path)
        if not p.exists():
            raise ToolError(f"File not found: {p}")
        self._check_read(p)
        m = re.fullmatch(r"\s*(\d+)\s*(?:[-:]\s*(\d+))?\s*", str(lines))
        if not m:
            raise ToolError('lines must look like "40-52" (or "40" for one line)')
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        raw = p.read_bytes()
        key = str(p)
        if self.read_hash.get(key) not in (None, hashlib.sha1(raw).hexdigest()):
            raise ToolError(f"{p.name} changed since you read it (by a command, a hook or the user). read_file it "
                            "again and use the new line numbers.")
        sh = self.shift.get(key)
        if sh and a > sh[0]:
            raise ToolError(f"Your earlier edit moved the lines below {sh[0]} by {sh[1]:+d}: lines {a}-{b} from your "
                            f"read are now {a + sh[1]}-{b + sh[1]}. Re-read, or use lines={a + sh[1]}-{b + sh[1]}.")
        text = raw.decode("utf-8", "replace")
        nl = "\r\n" if "\r\n" in text else "\n"
        rows = text.split(nl)
        if a < 1 or b < a or b > len(rows):
            raise ToolError(f"lines {a}-{b} out of range (file has {len(rows)} lines)")
        new_rows = new_string.replace("\r\n", "\n").split("\n") if new_string != "" else []
        if new_rows and new_rows[-1] == "" and new_string.endswith("\n"):
            new_rows = new_rows[:-1]
        out_rows = rows[:a - 1] + new_rows + rows[b:]
        delta = len(new_rows) - (b - a + 1)
        self._pending_shift = (key, b, delta)
        return p, text, nl.join(out_rows)

    def edit_preview(self, path, old_string, new_string, replace_all=False):
        p = self.path(path)
        if not p.exists():
            if old_string == "":
                return p, None, new_string
            raise ToolError(f"File not found: {p}")
        self._check_read(p)
        text = p.read_bytes().decode("utf-8", "replace")
        crlf = "\r\n" in text
        old, new = old_string, new_string
        if crlf:
            old, new = old.replace("\r\n", "\n").replace("\n", "\r\n"), new.replace("\r\n", "\n").replace("\n", "\r\n")
        if old == new:
            raise ToolError("find and replace are identical.")
        n = text.count(old) if old else 0
        if n == 0:
            fixed = self._fuzzy(text, old)
            if fixed is None:
                raise ToolError("`find` text not found in the file. Re-read the file and copy the exact text "
                                "(including indentation)." + self._hint(text, old))
            old, n = fixed, 1
        if n > 1 and not replace_all:
            raise ToolError(f"`find` text occurs {n} times; add more surrounding context or set replace_all.")
        new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        return p, text, new_text

    def _fuzzy(self, text, old):
        """Match ignoring trailing whitespace on each line (models often drop it). Returns exact original span."""
        want = [l.rstrip() for l in old.strip("\r\n").splitlines()]
        if not want:
            return None
        lines = text.splitlines(keepends=True)
        found = []
        for i in range(len(lines) - len(want) + 1):
            if all(lines[i + j].rstrip() == want[j] for j in range(len(want))):
                found.append(i)
        if len(found) != 1:
            return None
        i = found[0]
        span = "".join(lines[i: i + len(want)])
        return span.rstrip("\r\n") if not old.endswith("\n") else span

    def _hint(self, text, old):
        first = next((l.strip() for l in old.splitlines() if l.strip()), "")
        if not first:
            return ""
        lines = text.splitlines()
        m = difflib.get_close_matches(first, [l.strip() for l in lines], n=1, cutoff=0.6)
        if not m:
            return ""
        idx = [l.strip() for l in lines].index(m[0])
        snippet = "\n".join(f"{k + 1:>5}\t{lines[k]}" for k in range(max(0, idx - 2), min(len(lines), idx + 3)))
        return f"\nClosest match:\n{snippet}"

    def apply_write(self, p, new_text):
        p.parent.mkdir(parents=True, exist_ok=True)
        data = new_text.encode("utf-8")
        p.write_bytes(data)
        self.read_paths.add(str(p))
        key, ps = str(p), getattr(self, "_pending_shift", None)
        self._pending_shift = None
        self.read_hash[key] = hashlib.sha1(data).hexdigest()  # the model knows what it wrote
        if ps and ps[0] == key and ps[2]:
            old = self.shift.get(key)
            self.shift[key] = (min(ps[1], old[0]) if old else ps[1], ps[2] + (old[1] if old else 0))

    def write_preview(self, path, content):
        p = self.path(path)
        self._check_read(p)
        old = p.read_bytes().decode("utf-8", "replace") if p.exists() else None
        if old is not None and "\r\n" in old and "\r\n" not in content:
            content = content.replace("\n", "\r\n")
        return p, old, content

    # -- shell
    def run(self, command, timeout=120, stop=None):
        timeout = max(int(timeout or 120), 1)
        marker = "__INK_CWD__"
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", GIT_PAGER="cat", PAGER="cat",
                   NO_COLOR="1", TERM="dumb")
        if HAVE_BASH:
            script = f"trap 'printf \"\\n{marker}%s\\n\" \"$(pwd -W 2>/dev/null || pwd)\"' EXIT\n{command}\n"
            argv = [SHELL_BASH if IS_WIN else "bash", "-c", script]
        else:
            script = f"{command}\n$__ec=$LASTEXITCODE\nWrite-Output \"`n{marker}$((Get-Location).Path)\"\nexit $__ec"
            argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WIN else 0
        proc = subprocess.Popen(argv, cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, creationflags=flags)
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
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"] if IS_WIN else ["kill", "-9", str(proc.pid)],
                               capture_output=True)
                reader.join(3)
                break
        proc.wait(5) if not killed else None
        text = (chunks[0] if chunks else b"").decode("utf-8", "replace").replace("\r\n", "\n")
        m = re.search(rf"\n?{marker}(.*)\n?$", text)
        if m:
            newcwd = m.group(1).strip()
            text = text[: m.start()]
            if newcwd and Path(newcwd).is_dir():
                self.cwd = Path(newcwd)
        text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text).rstrip()
        code = proc.returncode
        res = trunc_middle(text, RUN_CHARS) or "(no output)"
        if killed:
            res += f"\n[{killed}]"
        elif code:
            res += f"\n[exit code {code}]"
        return res, (killed or (f"exit {code}" if code else "ok"))

class ToolError(Exception):
    pass

def web_tool(args):
    """search (query) or fetch (url) through ink's own web server functions (no keys needed)."""
    sys.path.insert(0, str(APP_DIR / "servers" / "web_server")) if str(APP_DIR / "servers" / "web_server") not in sys.path else None
    import server as websrv  # servers/web_server/server.py: web_search(), fetch_url()
    url, q = str(args.get("url") or "").strip(), str(args.get("query") or "").strip()
    try:
        if url:
            r = websrv.fetch_url(url, 20000)
            text = r.get("text") if isinstance(r, dict) else str(r)
            title = r.get("title", "") if isinstance(r, dict) else ""
            return (f"{title}\n{r.get('url', url) if isinstance(r, dict) else url}\n\n{text}"), f"read {len(text or '')} chars"
        if q:
            rows = websrv.web_search(q, 8)
            return ("\n\n".join(f"{i + 1}. {r['title']}\n{r['url']}\n{r['snippet']}" for i, r in enumerate(rows))
                    or "No results."), f"{len(rows)} results"
    except Exception as e:
        raise ToolError(f"web failed: {e}")
    raise ToolError("give query (to search) or url (to read a page)")

ALIASES = {"file": "path", "file_path": "path", "filepath": "path", "filename": "path",
           "find": "old_string", "old": "old_string", "old_str": "old_string", "search": "old_string",
           "replace": "new_string", "new": "new_string", "new_str": "new_string", "replacement": "new_string",
           "text": "content", "contents": "content", "data": "content"}

def normalize_args(name, args):
    """Map public tool parameter names (and common variants models use) to the internal ones."""
    if name not in ("read_file", "edit_file", "write_file"):
        return args
    return {ALIASES.get(k, k): v for k, v in args.items()}

# ---------------------------------------------------------------- prompt
def system_prompt(cwd, plan_mode=False):
    shell = "Git Bash (POSIX paths like /c/Users/... also work)" if HAVE_BASH else "PowerShell"
    s = f"""You are Ink, an expert software engineer working as a coding agent in the user's terminal. You complete coding tasks by reading code, editing files and running commands with your tools.

Environment: {platform.system()} {platform.release()} | working directory: {cwd} | `run` uses {shell} | date: {datetime.date.today()}

How to work:
- Act, don't narrate. Investigate, make the change, then verify it by running the code, tests or build. Ask the user only when a decision is genuinely theirs.
- Navigate precisely: in unfamiliar code call map (with a query) to find the files and symbols, then read_file just those line ranges. Before changing a function, grep its name to find every caller.
- Verify with real runs: every edit is syntax-checked automatically (fix any reported error immediately). `check` runs the project's tests/compile/lint; the harness also runs them before you finish and shows you failures. For logic you add, add or extend a test. Never weaken, skip or delete a test to make it pass unless the user asked."""
    if PLAYTEST_SCHEMA:
        s += "\n- Browser games/pages: use playtest to load the page in headless Chrome, press keys/click, and assert on game state (window.* values); it reports exceptions, console errors, failed requests and FPS. Playtest after gameplay changes."
    s += """
- Work loop, for every task (no mode needed):
  1. Orient: read the existing code you will touch and the current plan before deciding anything.
  2. Plan: for 3+ steps call plan with the goal, the key points (every requirement and constraint the user gave, in their words) and small steps - each one piece you can run or test on its own (one module, one feature, one screen).
  3. Build one step: write a small piece (about 100 lines at most), then run it at once - typecheck, its test, or playtest. Write the test together with the code.
  4. When it works, mark the step done (the harness then runs the project checks; a failing step goes back to doing). Add steps you discover, split steps that turn out big, and move on to the next one.
  5. Repeat until every step is done, then check the result against the goal and each key point.
  Never shrink the goal: if the user asked for a whole game, a finished engine is one step, not the end. If you stop before the plan is done, say exactly what is left.
- Quality bar: work like a senior full-stack engineer at a top product company. Finish whole features with every state and edge case (no TODO, stub or placeholder in delivered code); strict types and validated inputs; designed error handling; server-authoritative and secret-free; modern maintained libraries (check versions with `npm view <pkg> version`, never from memory) - in an existing project, its stack and style win; tests for every rule; README/scripts that work from a clean clone. Playbooks in the guides below hold the details.
- Use the dedicated tools, not shell equivalents: read_file (not cat/head), grep (not grep/findstr), glob (not find/ls -R), edit_file/write_file (never sed, echo > or scripts to modify files). `run` is for building, testing, git and running programs.
- Read a file before editing it. Change existing files with edit_file: `find` must match the file exactly (indentation included) and be unique. Use write_file only for new files or full rewrites. You can call several tools at once when they are independent.
- Follow the existing code style. Don't add unrequested features, files or explanatory comments.
- Keep tool output small: grep to locate code, read_file with offset/limit on large files.
- When something fails, read the error and fix the cause. Don't repeat an identical failing call.
- Stuck after two real attempts? Use agent with kind "debug": a fresh debugger with a clean context investigates the failure and reports the root cause; then you apply the fix.
- Never say something works unless you ran it and saw it work. Report failures honestly.
- Don't run destructive commands (rm -rf, git reset --hard, force push, deleting data) unless the user asked for it.
- Replies: short and direct, GitHub markdown, reference code as path:line. No preamble and no step-by-step recap; say what changed and what the user must know.

Code mode (the `code` tool) - for MCP tools, big data and anything repetitive:
- MCP tools are Python functions, one file each under servers/<server>/<tool>.py. Don't guess them: print(search_tools("keywords")) first, then import only what you need (the result gives the import line).
- Saved skills come first in search_tools results. Reuse one before writing new code; after new code works, save_skill(...) it.
- Filter, aggregate, loop, retry and poll in code; print only the rows or fields you need, never whole tables, logs or documents.
- Personal data comes back as [EMAIL_1], [PHONE_1], [NAME_1]. Pass those placeholders as-is to tools; the real values are filled in outside your view."""
    skills = []
    for md in sorted((APP_DIR / "skills").glob("*/SKILL.md"))[:25]:
        desc = next((l.strip() for l in md.read_text("utf-8", "replace").splitlines()[1:] if l.strip()), "")
        skills.append(f"- {md.parent.name}: {short(desc, 140)} (see skills/{md.parent.name}/SKILL.md)")
    if skills:
        s += "\n\nSaved skills - when one fits the task, use it in `code` instead of writing new code:\n" + "\n".join(skills)
    # Claude/agentskills.io-style skills: SKILL.md with `name:`/`description:` front matter; only the one-line
    # description is always loaded, the model reads the file when the task matches (progressive disclosure)
    std = []
    for root in (STATE / "skills", Path.home() / ".claude" / "skills", Path(cwd) / ".ink" / "skills",
                 Path(cwd) / ".claude" / "skills"):
        for md in sorted(root.glob("*/SKILL.md"))[:20] if root.is_dir() else []:
            try:
                head = md.read_text("utf-8", "replace")[:1500]
            except Exception:
                continue
            fm = re.match(r"^---\s*\n(.*?)\n---", head, re.S)
            meta = dict(re.findall(r"^(\w+):\s*(.+)$", fm.group(1), re.M)) if fm else {}
            desc = meta.get("description") or next((l.strip("# ").strip() for l in head.splitlines()[1:] if l.strip()), "")
            std.append(f"- {meta.get('name') or md.parent.name}: {short(desc, 140)} ({md})")
    if std:
        s += ("\n\nSkills - step-by-step procedures; when the task matches one, read_file its SKILL.md first and "
              "follow it:\n" + "\n".join(std[:25]))
    guides = []
    for md in sorted(GUIDES_DIR.glob("*.md")) if GUIDES_DIR.is_dir() else []:
        if md.name.upper() == "INDEX.MD":
            continue
        first = next((l.strip().lstrip("#> ").strip() for l in md.read_text("utf-8", "replace").splitlines() if l.strip()), "")
        guides.append(f"- {md.name}: {short(first, 150)}")
    if guides:
        s += (f"\n\nExpert guides in {GUIDES_DIR} - checklists and patterns written by a senior expert. When the task "
              "touches one of these areas, read_file the guide BEFORE designing or writing the code, follow it, and run "
              "its Checklist before you finish:\n" + "\n".join(guides))
    s += f"""

Memory ({MEMORY_DIR}) - what you know from earlier sessions:
- When you learn something worth keeping (the user's preferences or corrections, project facts, where things are, what worked or failed), save it: one fact per file `{MEMORY_DIR}/<short-name>.md`, plus a one-line `- [title](file.md) - hook` entry in MEMORY.md. Update an existing file instead of duplicating; delete facts that became wrong.
- Don't save what the code or git history already shows, or one-off details of the current task.
- recall(query) searches past sessions and these notes: use it when the user refers to earlier work."""
    index = MEMORY_DIR / "MEMORY.md"
    if index.is_file():
        s += "\n\nMEMORY.md (read the linked file when an entry is relevant):\n" + index.read_text("utf-8", "replace")[:4000]
    for name in ("INK.md", "CLAUDE.md", "AGENTS.md"):
        f = Path(cwd) / name
        if f.is_file():
            s += f"\n\nProject instructions ({name}):\n" + f.read_text("utf-8", "replace")[:6000]
            break
    if plan_mode:
        s += """

PLAN MODE is ON - you are planning, not building:
- Research with read_file, grep and glob. edit_file, write_file and run are disabled and will be rejected.
- Investigate enough to produce a concrete, correct plan: identify the exact files, functions and changes needed.
- Record the plan with the `plan` tool: ordered steps with status "todo" (the first may be "doing"). Include a verification step.
- Then stop and present a short summary: the approach, files you would change, and any risks or open questions. Never claim anything was changed. The user will review the plan and switch to build mode to execute it."""
    return s

# ---------------------------------------------------------------- the agent
class Interrupted(Exception):
    pass

MAX_STEPS = float("inf")  # no cap on tool-use rounds per user message
BIG_WRITE = int(_cfg("big_write_lines", 150))       # nudge to run code after writing this many untested lines
STEP_CHECKS = bool(_cfg("step_checks", True))      # run the project checks when a plan step is marked done
TS_AFTER_EDIT = bool(_cfg("ts_after_edit", True))  # tsc --noEmit after a step that edited TypeScript
FOLLOW_PROJECT = bool(_cfg("follow_project", True))  # move the session into the project folder being built
ORIENT = bool(_cfg("orient", True))                # repo map with the first prompt of a session
ORIENT_TOKENS = int(_cfg("orient_tokens", 1200))
SHELL_EDIT = re.compile(r"\b(sed|gsed)\s+(-[a-zA-Z]*i|--in-place)|\bperl\s+-[a-zA-Z]*i|"
                        r"\b(Set-Content|Add-Content|Out-File)\b[^|]*\.(js|mjs|ts|tsx|jsx|py|sol|html|css|json)\b")
CHURN_AT = int(_cfg("churn_edits", 6))             # edits to one file without a passing check -> step back
STUCK_AT = int(_cfg("stuck_after_failures", 3))     # same check failing this often -> guided step back
ESCALATE = str(_cfg("escalate", "local"))          # "local": a fresh-context debug subagent (same local model); "off"
ESCALATE_MAX = int(_cfg("escalate_max_per_session", 3))
STEP_BUDGET_MIN = float(_cfg("step_budget_minutes", 30))  # one plan step taking longer -> re-plan
FAIL_NAME = re.compile(r"^\s*(?:FAIL\s+|×\s+|✗\s+|✖\s+|not ok \d+ - |FAILED\s+|\d+\) )(.{6,200}?)\s*$")
TEST_CMD = re.compile(r"\b(vitest|jest|mocha|node --test|npm (run )?test|npx hardhat test|forge test|pytest|playwright)\b")
SCRATCH = re.compile(r"^(dbg|debug|tmp|temp|scratch|chk|check_tmp|try|playground|test_tmp)[\w.-]*\.(ts|tsx|js|mjs|cjs|py|txt|json|log)$|"
                     r"^[\w-]*_(tmp|debug|scratch)\.\w+$", re.I)
KEEP_REASONING = int(_cfg("keep_reasoning", 1))    # past assistant messages that keep their thinking (-1 = all)
COMPACT_AT = {"fast": int(_cfg("fast_compact_tokens", 120000)), "smart": int(_cfg("smart_compact_tokens", 10 ** 9))}
TRACE_ON = bool(_cfg("trace", True))               # live session trace in ~/.ink/trace (for ink_watch.py)
TRACE_CHARS = int(_cfg("trace_chars", 1500))
AUTO_BUILD = bool(_cfg("auto_build", True))        # plan mode: build by itself once the plan is checked and solid
MAX_BRIEFS = int(_cfg("max_briefs", 3))            # playbooks inlined per request
CODE_EXT = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".py", ".sol", ".rs", ".go", ".java", ".cs", ".html"}
READ_SHELL = re.compile(r"^\s*(cd\s+[^&;|]+(&&|;)\s*)?(cat|ls|find|head|tail|tree|dir|type)\b[^|>]*$")
PLAN_STALE = 6     # remind the model to revise its plan after this many steps without an update
KV_BUDGET = float(_cfg("kv_budget_gb", 12)) * 1e9  # saved session KV caches kept on disk (newest first); ~4 GB per full 244k session

KV_MAX_DAYS = float(_cfg("kv_max_days", 3))
# seconds a full re-read may take before ink compacts first (switching model/effort/mode re-reads everything)
REREAD_MAX_S = float(_cfg("reread_max_seconds", 180))
PREFILL_TPS = {"smart": float(_cfg("smart_prefill_tps", 140)), "fast": float(_cfg("fast_prefill_tps", 1100))}

def prune_kv():
    """Saved conversation caches are ~17 KB/token and don't compress (measured: zstd saves 4%), so keep only the
    ones that can be resumed: the newest session per directory (auto-continue only resumes that one), younger than
    kv_max_days, within kv_budget_gb. System-prompt caches (small) are kept by the budget rule only."""
    files = sorted(SLOT_DIR.glob("*.bin"), key=lambda f: f.stat().st_mtime, reverse=True)
    newest_per_dir, keep = {}, []
    for f in files:
        m = re.match(r"sess-(\d{8}-\d{6})-(\w+)\.bin$", f.name)
        if m:
            if time.time() - f.stat().st_mtime > KV_MAX_DAYS * 86400:
                f.unlink(missing_ok=True)
                continue
            try:
                with open(SESS_DIR / f"{m.group(1)}.json", encoding="utf-8") as fh:
                    cwd = re.search(r'"cwd":\s*"((?:[^"\\]|\\.)*)"', fh.read(4096)).group(1)
            except Exception:
                cwd = None
            if cwd is not None:
                stem = newest_per_dir.setdefault(cwd, m.group(1))
                if stem != m.group(1):  # an older session in a dir that has a newer one
                    f.unlink(missing_ok=True)
                    continue
        keep.append(f)
    total = 0
    for i, f in enumerate(keep):
        total += f.stat().st_size
        if i > 0 and total > KV_BUDGET:
            f.unlink(missing_ok=True)

class Agent:
    def __init__(self, profile, effort=None, yolo=False, show_thinking=True, plan_mode=False, split=False):
        self.cwd = Path.cwd()
        self.tools = Tools(self.cwd)
        self.profile = profile
        self.effort = effort or PROFILES[profile]["effort"]
        self.yolo = yolo
        self.show_thinking = show_thinking
        self.plan_mode = plan_mode
        self.split = split  # phase-split: plan on smart (Inkling), build on fast (Ornith)
        self.allowed = set()  # tool names / command prefixes allowed for this session
        self.undo_stack = []  # file edits this session, newest last (backed up on disk)
        self.stop = threading.Event()
        self.cp = Checkpoints(self.cwd)
        self.t_session = getattr(self, "t_session", None) or time.time()
        self.fail_counts, self.fail_text, self.stuck_noted = {}, {}, {}
        self.churn, self.churn_noted = {}, {}
        self.hooks = Hooks(self.cwd, log)
        self.jobs = Jobs(lambda: self.tools.cwd, STATE / "jobs") if Jobs else None
        self.answers = queue.Queue()   # --rpc: answers to `ask`
        self.model_lock = threading.RLock()  # one model call at a time (turns, idle memory review)
        self.pending = []              # context for the next prompt (! output, background agent reports)
        self.turns_since_dream = 0
        self.steer = []                # --rpc: guidance typed while a turn runs
        self.reset()

    @property
    def P(self):
        return PROFILES[self.profile]

    def reset(self):
        self.messages = [{"role": "system", "content": system_prompt(self.cwd, self.plan_mode)}]
        self.ctx_used = 0
        self.plan = []
        self.goal, self.key_points = "", []  # pinned brief (set through the plan tool)
        self.requests = []                    # the user's own messages, kept verbatim through compaction
        self.changed = []                     # files written this session
        self.pin_step, self.pin_ctx = 0, 0
        self.hinted, self.notes = set(), []   # expert guides already suggested; harness notes for the next result
        self.turn_files = set()
        self.kv_tokens = {}                   # profile -> conversation tokens in its saved KV cache
        self.loaded_tools_sig = None          # tool schema of a resumed session (a change invalidates its cache)
        self.dreamed_at = None
        self.session_file = SESS_DIR / f"{datetime.datetime.now():%Y%m%d-%H%M%S}.json"

    def count_failures(self, text):
        """Remember which tests/checks keep failing this turn (from test runs and harness checks)."""
        names = []
        for l in re.sub(r"\x1b\[[0-9;]*m", "", text or "").splitlines():
            m = FAIL_NAME.match(l)
            if m and not re.match(r"^(Tests?|Test Files)\b", m.group(1)):
                n = re.sub(r"\s+\d+(\.\d+)?\s?m?s$", "", m.group(1).strip())  # vitest timing suffix
                names.append(n.split(" - ")[0] if "::" in n else n)       # pytest "FAILED a::b - msg"
        for n in dict.fromkeys(names):
            self.fail_counts[n] = self.fail_counts.get(n, 0) + 1
            self.fail_text[n] = (text or "")[-3500:]
        return names

    def current_step(self):
        return next((s["step"] for s in self.plan if s["status"] == "doing"),
                    next((s["step"] for s in self.plan if s["status"] == "todo"), None))

    def scratch_files(self):
        """Debug/experiment files the model created in the project and left behind (they end up committed)."""
        found = []
        t0 = getattr(self, "t_session", time.time()) - 60
        for d in [self.cwd] + [c for c in self.cwd.iterdir() if c.is_dir() and c.name in ("src", "tests", "test", "scripts")]:
            try:
                for f in d.iterdir():
                    if f.is_file() and SCRATCH.match(f.name) and f.stat().st_mtime >= t0:
                        found.append(str(f.relative_to(self.cwd)))
            except Exception:
                continue
        return found[:10]

    def escalate(self, name):
        """The model is stuck on a check: ink writes the question (goal, step, failure, diff) and a fresh debugger -
        the same local model with a clean, small context - investigates. Not anchored on the failed attempts, and
        fast because its context is small. Fully local."""
        if ESCALATE == "off" or getattr(self, "escalations", 0) >= ESCALATE_MAX:
            return ""
        self.escalations = getattr(self, "escalations", 0) + 1
        try:
            diff = self.cp.diff_since(self.turn_cp) if getattr(self, "turn_cp", None) else ""
        except Exception:
            diff = ""
        q = (f"The main agent is stuck: the check below has failed {self.fail_counts.get(name)} times "
             "in a row and it keeps trying fixes.\n"
             f"Project: {self.cwd}\nGoal: {self.goal or short(self.requests[-1] if self.requests else '', 400)}\n"
             f"Current plan step: {self.current_step() or '-'}\nFailing check: {name}\n\n"
             f"Latest failure output:\n{self.fail_text.get(name, '')}\n\n"
             f"Files changed in this task: {', '.join(self.changed[-25:])}\n\n"
             f"Diff of this task so far (truncated):\n{diff[:9000]}\n\n"
             "Read the relevant code and tests. Decide which is wrong - the code, the test's expected value, or "
             "the test's assumptions - and give the exact fix (file:line and code). If an expected value is "
             "unjustified, say what it should be and why. Keep it short and concrete.")
        try:
            text = self.run_subagent("debug", q)
        except Exception as e:
            log(f"escalation failed: {e!r}")
            return ""
        if not text or text.startswith(("Subagent failed", "Subagent interrupted", "(the subagent")):
            return ""
        return text

    def trace_messages(self):
        TRACE["stem"] = self.session_file.stem
        start = getattr(self, "_traced", None)
        if start is None:
            start = 1
        elif start > len(self.messages):  # compacted meanwhile: the summary + what follows
            trace({"type": "compacted", "msgs": len(self.messages)})
            start = max(1, len(self.messages) - 2)
        for m in self.messages[start:]:
            ev = {"type": "msg", "role": m["role"], "content": str(m.get("content") or "")[:TRACE_CHARS]}
            if m.get("tool_calls"):
                ev["calls"] = [{"name": c["function"]["name"], "args": c["function"].get("arguments", "")[:400]}
                               for c in m["tool_calls"]]
            if m.get("reasoning_content"):
                ev["thinking_chars"] = len(m["reasoning_content"])
            trace(ev)
        self._traced = len(self.messages)

    def compact_limit(self):
        return min(0.85 * self.P["ctx"], COMPACT_AT.get(self.profile, 10 ** 9))

    def plan_is_solid(self):
        """Cheap structural check after the model's own critique: a goal, at least two concrete open steps."""
        open_steps = [s for s in self.plan if s["status"] in ("todo", "doing")]
        return bool(self.goal) and len(open_steps) >= 2 and all(len(s["step"]) >= 12 for s in open_steps)

    def set_mode(self, mode):
        plan = mode == "plan"
        if plan == self.plan_mode:
            out(GRAY(f"already in {'plan' if plan else 'build'} mode"))
            return
        # a new system prompt invalidates the whole cache (and split mode also switches model)
        self.reread_guard("smart" if (self.split and plan) else "fast" if self.split else self.profile,
                          "changing mode", cached=False)
        self.plan_mode = plan
        self.messages[0] = {"role": "system", "content": system_prompt(self.cwd, self.plan_mode)}
        if plan:
            out(GRAY("plan mode ON · read-only research · edits and commands disabled · /plan off or /build to execute"))
        else:
            out(GRAY("build mode ON · edits and commands enabled"))
        switched = False
        if self.split:
            want = "smart" if plan else "fast"
            if want != self.profile:
                switch(self, want)  # reloads the model for this phase (KV cache keeps the conversation)
                switched = True
        if len(self.messages) > 1 and not switched:
            threading.Thread(target=self.warm, daemon=True).start()
        emit("stats", mode=self.mode_name(), model=self.P["title"], effort=self.effort,
             ctx_used=self.ctx_used, ctx=self.P["ctx"])

    # -- session KV cache on the SSD: resuming a 30k-token session takes seconds instead of ~12 minutes
    def kv_name(self, profile=None):
        return f"sess-{self.session_file.stem}-{profile or self.profile}.bin"

    def persist_kv(self, wait=False):
        prof, toks = self.profile, self.ctx_used

        def job():
            try:
                st, _ = server_post(PROFILES[prof]["port"], "/slots/0?action=save", {"filename": self.kv_name(prof)}, timeout=180)
                if st == 200:
                    self.kv_tokens[prof] = toks
                prune_kv()
            except Exception:
                pass
        threading.Thread(target=job, daemon=True).start() if not wait else job()

    def reread_guard(self, target, why, cached=True):
        """Before something that makes `target` re-read the conversation (another model, another effort or mode):
        if that would take longer than reread_max_seconds, compact first - with the current model, whose cache is
        warm, so the summary is quick - and the re-read shrinks from up to 200k tokens to a few thousand."""
        if len(self.messages) < 4 or not self.ctx_used:
            return
        have = self.kv_tokens.get(target, 0) if cached and (SLOT_DIR / self.kv_name(target)).exists() else 0
        need = max(0, self.ctx_used - have)
        secs = need / PREFILL_TPS.get(target, 73)
        if secs > REREAD_MAX_S:
            out(GRAY(f"{why}: re-reading {ktok(need)} tokens would take ~{secs / 60:.0f} min - compacting first"))
            self.compact()

    def cold_compact(self, keep_tokens=None):
        """Compaction without the model, for a big session whose saved cache is gone: rebuilding it would mean
        re-reading everything (~45 min for 200k on Inkling). Keeps the verbatim brief (requests, goal, key points,
        plan, changed files), the last summary, and the most recent messages that fit in keep_tokens."""
        keep_tokens = keep_tokens or int(_cfg("cold_keep_tokens", 12000))
        msgs = self.messages[1:]
        tail, size = [], 0
        for m in reversed(msgs):
            n = len(json.dumps(m)) // 4
            if size + n > keep_tokens and tail:
                break
            tail.insert(0, m)
            size += n
        while tail and tail[0]["role"] != "user":  # never start inside a tool-call/result pair
            tail.pop(0)
        last_summary = next((m["content"] for m in msgs if m["role"] == "user" and isinstance(m.get("content"), str)
                             and m["content"].startswith("[Conversation summary")), "")
        kept = []
        if last_summary:
            kept.append(last_summary.split("\n", 1)[-1][:6000])
        if self.goal or self.key_points or self.plan:
            kept.append(self.brief_text())
        else:
            reqs = [r for r in self.requests if not r.startswith("[")]
            if reqs:
                kept.append("The user's requests, verbatim (oldest first):\n" + "\n".join(f"- {short(r, 400)}" for r in reqs[-12:]))
        if self.changed:
            kept.append("Files changed this session: " + ", ".join(self.changed[-40:]))
        kept.append("Older details can be searched with recall(query). The most recent messages follow verbatim.")
        self.messages = [self.messages[0], {"role": "user", "content": "[Conversation summary - this session was resumed "
                         "and earlier messages were compacted]\n" + "\n\n".join(kept)},
                         {"role": "assistant", "content": "Understood. I'll continue from this summary."}] + tail
        self.ctx_used = sum(len(json.dumps(m)) for m in self.messages) // 4
        self.tools.forget()
        out(GRAY(f"compacted without the model: kept the brief + last {ktok(size)} tokens (older details via recall)"))

    def restore_kv(self):
        f = SLOT_DIR / self.kv_name()
        if not f.exists():
            return False
        try:
            st, j = server_post(self.P["port"], "/slots/0?action=restore", {"filename": f.name}, timeout=300)
            return st == 200
        except Exception:
            return False

    def kwargs(self):
        if self.profile == "smart":
            return {"reasoning_effort": self.effort}
        return {"enable_thinking": self.effort != "none"}

    # -- prompt cache warm-up / persistence
    def cache_key(self):
        h = hashlib.sha1(json.dumps([self.messages[0], TOOLS, self.kwargs(), self.P["model"]]).encode()).hexdigest()
        return f"{self.profile}-{h[:16]}.bin"

    def warm(self):
        """Run in background: restore the saved system-prompt KV, or compute and save it."""
        port, key = self.P["port"], self.cache_key()
        try:
            if (SLOT_DIR / key).exists():
                st, _ = server_post(port, "/slots/0?action=restore", {"filename": key})
                if st == 200:
                    return
            body = {"model": "local", "messages": [self.messages[0], {"role": "user", "content": "hi"}],
                    "tools": TOOLS, "max_tokens": 1, "chat_template_kwargs": self.kwargs()}
            st, _ = server_post(port, "/v1/chat/completions", body, timeout=900)
            if st == 200:
                server_post(port, "/slots/0?action=save", {"filename": key})
        except Exception:
            pass

    # -- streaming
    def _stream(self, messages, tools=TOOLS, max_tokens=MAX_TOKENS):
        if KEEP_REASONING >= 0:
            # Past thinking was 40% of a long session's context (INK-track O9). Keep it on the last KEEP_REASONING
            # assistant messages only; dropping it from the one before costs a re-read of just the last step or two.
            idx = [i for i, m in enumerate(messages) if m["role"] == "assistant" and m.get("reasoning_content")]
            drop = set(idx[:-KEEP_REASONING] if KEEP_REASONING else idx)
            if drop:
                messages = [{k: v for k, v in m.items() if k != "reasoning_content"} if i in drop else m
                            for i, m in enumerate(messages)]
        body = {"model": "local", "messages": messages, "stream": True,
                "stream_options": {"include_usage": True}, "return_progress": True,
                "max_tokens": max_tokens, "parallel_tool_calls": True, "chat_template_kwargs": self.kwargs()}
        if tools:
            body["tools"] = tools
        conn = http.client.HTTPConnection("127.0.0.1", self.P["port"], timeout=3600)
        q = queue.Queue()

        def reader():
            try:
                conn.request("POST", "/v1/chat/completions", json.dumps(body).encode(),
                             {"Content-Type": "application/json"})
                r = conn.getresponse()
                if r.status != 200:
                    q.put(("error", f"HTTP {r.status}: {r.read()[:500].decode('utf-8', 'replace')}"))
                    return
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("data:"):
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        q.put(("data", json.loads(data)))
                    elif line.startswith("error:") or line.startswith('{"error'):
                        q.put(("error", line))
            except Exception as e:
                q.put(("error", repr(e)))
            finally:
                q.put(("end", None))

        threading.Thread(target=reader, daemon=True).start()
        try:
            while True:
                try:
                    item = q.get(timeout=0.1)
                except queue.Empty:
                    if self.stop.is_set():
                        raise Interrupted()
                    yield ("tick", None)
                    continue
                if item[0] == "end":
                    return
                yield item
                if self.stop.is_set():
                    raise Interrupted()
        finally:
            try:
                conn.sock and conn.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            conn.close()

    def complete(self, messages, live=True, tools=TOOLS, max_tokens=MAX_TOKENS):
        """One model call. Returns assistant message dict and stats."""
        content, reasoning, calls = [], [], {}
        mode = None  # 'think' | 'text'
        t0, t_first, i = time.time(), None, 0
        t_think = t0
        stats = {"prompt": 0, "cached": 0, "gen": 0, "tps": 0.0, "pps": 0.0}
        status = ""

        def set_status(s):
            nonlocal status
            if live:
                out(CLR + s, end="")
            status = s

        def clear_status():
            nonlocal status
            if status and live:
                out(CLR, end="")
            status = ""

        for kind, j in self._stream(messages, tools, max_tokens):
            i += 1
            if kind == "tick":
                if t_first is None or mode is None:
                    el = time.time() - t0
                    prog = stats.get("prog")
                    if RPC and live:
                        if prog and prog != getattr(self, "_last_prog", None):
                            self._last_prog = prog
                            emit("progress", done=prog[0], total=prog[1])
                        continue
                    msg = f"reading {prog[0]}/{prog[1]} tok" if prog else "thinking"
                    set_status(f"{ACCENT(SPIN[i % 10])} {GRAY(msg + f' · {el:.0f}s · esc to interrupt')}")
                continue
            if kind == "error":
                clear_status()
                raise RuntimeError(j)
            if "prompt_progress" in j:
                pp = j["prompt_progress"]
                stats["prog"] = (pp.get("processed", 0) - pp.get("cache", 0), pp.get("total", 0) - pp.get("cache", 0))
            if j.get("usage"):
                stats["prompt"] = j["usage"].get("prompt_tokens", 0)
                stats["gen"] = j["usage"].get("completion_tokens", 0)
            if j.get("timings"):
                tm = j["timings"]
                stats["cached"] = tm.get("cache_n", 0)
                stats["tps"] = tm.get("predicted_per_second", 0) or 0
                stats["pps"] = tm.get("prompt_per_second", 0) or 0
                stats["pn"] = tm.get("prompt_n", 0)
            for ch in j.get("choices") or []:
                d = ch.get("delta") or {}
                if t_first is None and (d.get("reasoning_content") or d.get("content") or d.get("tool_calls")):
                    t_first = time.time()
                if d.get("reasoning_content"):
                    reasoning.append(d["reasoning_content"])
                    if live and RPC:
                        if mode != "think":
                            t_think = time.time()
                        mode = "think"
                        emit("thinking_delta", text=d["reasoning_content"])
                    elif live:
                        if mode != "think":
                            clear_status()
                            if self.show_thinking:
                                out(GRAY("∴ "), end="")
                            mode = "think"
                        if self.show_thinking:
                            out(GRAY(ITAL(d["reasoning_content"])), end="")
                        else:
                            n = len("".join(reasoning)) // 4
                            set_status(f"{ACCENT(SPIN[i % 10])} {GRAY(f'thinking… ~{n} tok · esc to interrupt')}")
                if d.get("content"):
                    content.append(d["content"])
                    if live and RPC:
                        if mode == "think":
                            emit("thinking_end", secs=round(time.time() - t_think, 1), tokens=len("".join(reasoning)) // 4)
                        if mode != "text" and not "".join(content).strip():
                            content = []
                        else:
                            mode = "text"
                            emit("text_delta", text=d["content"])
                    elif live:
                        if mode != "text":
                            clear_status()
                            if mode == "think" and self.show_thinking:
                                out("\n")
                            mode = "text"
                        if not "".join(content).strip():
                            content = []  # drop leading whitespace before any real text
                        else:
                            out(d["content"], end="")
                for tc in d.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                    slot["id"] = tc.get("id") or slot["id"]
                    f = tc.get("function") or {}
                    slot["name"] += f.get("name") or ""
                    slot["args"] += f.get("arguments") or ""
                    if live and RPC and mode == "think":
                        emit("thinking_end", secs=round(time.time() - t_think, 1), tokens=len("".join(reasoning)) // 4)
                    if live:
                        if mode == "think" and self.show_thinking and not RPC:
                            out("")
                        mode = "tool"
                        set_status(f"{ACCENT(SPIN[i % 10])} {GRAY('writing ' + slot['name'] + f' call… {len(slot["args"])} chars')}")
                if ch.get("finish_reason"):
                    stats["finish"] = ch["finish_reason"]
        clear_status()
        if live and RPC:
            if mode == "think":
                emit("thinking_end", secs=round(time.time() - t_think, 1), tokens=len("".join(reasoning)) // 4)
            if content:
                emit("text_end")
        elif live and mode in ("text", "think"):
            out("")
        stats["secs"] = time.time() - t0
        msg = {"role": "assistant", "content": "".join(content).strip()}
        if reasoning:
            msg["reasoning_content"] = "".join(reasoning)
        if calls:
            msg["tool_calls"] = [{"id": c["id"] or f"call_{k}_{int(time.time()*1000)}", "type": "function",
                                  "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                                 for k, c in sorted(calls.items())]
        return msg, stats

    # -- esc watcher
    def _watch_keys(self, active):
        if RPC:  # the UI owns the console; it sends {"type":"interrupt"} instead of us reading Esc
            return
        while active.is_set():
            if IS_WIN and msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch == "\x1b":
                    self.stop.set()
            time.sleep(0.05)

    def rewind(self, arg=""):
        """/rewind -> files as they were before the last turn; /rewind 3 -> before the 3rd-last turn; /rewind <id>."""
        if not self.cp.enabled:
            out(YELLOW(self.cp.error or "checkpoints are off"))
            return
        turns = [c for c in self.cp.list(200) if c[2].startswith("turn: ")]
        if arg and not arg.isdigit():
            ref = arg
        else:
            n = int(arg or 1)
            if n > len(turns):
                out(GRAY(f"only {len(turns)} checkpoint(s) here - /checkpoints lists them"))
                return
            ref = turns[n - 1][0]
        try:
            restored, deleted, saved = self.cp.rewind(ref)
        except Exception as e:
            out(RED(f"rewind failed: {e}"))
            return
        self.tools.forget()
        out(GREEN(f"rewound to {ref}: {restored} file(s) restored, {deleted} new file(s) removed") +
            GRAY(f" · undo this with /rewind {saved}"))
        self.messages.append({"role": "user", "content": f"[The user rewound the project files to checkpoint {ref}. "
                              "Files may differ from what you saw earlier: re-read before editing.]"})
        self.messages.append({"role": "assistant", "content": "Understood - I'll re-read files before changing them."})

    def turn(self, text):
        stop_dream("user turn")
        TRACE["stem"] = self.session_file.stem
        cp = self.cp.snapshot("turn: " + " ".join(text.split())[:100])
        self.turn_cp = cp
        if cp is None and self.cp.error and not getattr(self, "_cp_warned", False):
            self._cp_warned = True
            out(GRAY(f"({self.cp.error})"))
        blocked, extra = self.hooks.prompt(text) if self.hooks else (None, "")
        if blocked:
            out(YELLOW(f"prompt blocked by a UserPromptSubmit hook: {short(blocked, 200)}"))
            emit("turn_end", secs=0, steps=0, interrupted=True)
            return
        emit("turn_start", id=len(self.requests) + 1, text=getattr(self, "display_prompt", None) or text)
        self.display_prompt = None
        if getattr(self, "_traced", None) is None:  # trace this session from its next message on
            self._traced = len(self.messages)
        orient = (self.orientation() if not self.requests else "") + self.request_briefs(text)
        self.messages.append({"role": "user", "content": text + (f"\n\n[UserPromptSubmit hook]\n{extra}" if extra else "")
                              + orient})
        self.requests.append(text)
        self.stop.clear()
        t_turn = time.time()
        stop_hooks = 0
        steps, nudged, verified, critiques = 0, False, False, 0
        self._auto_build = False
        check_rounds, checked_at, check_report = 0, -1, ""
        self.turn_changes, self.pin_step = 0, 0
        self.turn_files = set()
        self._reviewed = False
        self.sigs, self.fail_streak, self.since_plan, self.plan_hinted = {}, 0, 0, bool(self.plan)
        self.fail_counts, self.fail_text, self.stuck_noted = {}, {}, {}
        self.churn, self.churn_noted = {}, {}
        self.last_sig, self.last_failed = "", False
        while True:
            steps += 1
            active = threading.Event()
            active.set()
            w = threading.Thread(target=self._watch_keys, args=(active,), daemon=True)
            w.start()
            try:
                self.trace_messages()
                t_step = time.time()
                msg, st = self.complete(self.messages)
                trace({"type": "model", "secs": round(time.time() - t_step, 1), **{k: st.get(k) for k in
                       ("prompt", "gen", "tps", "cached", "pps", "finish") if k in st}})
            except (Interrupted, KeyboardInterrupt):
                active.clear()
                out(f"{CLR}{YELLOW('⏹ interrupted')} {GRAY('- tell ink what to do instead')}")
                self.messages.append({"role": "user", "content": "[The user interrupted your previous response.]"})
                self.save()
                emit("turn_end", secs=round(time.time() - t_turn, 1), steps=steps, interrupted=True)
                return
            except Exception as e:
                active.clear()
                out(f"{CLR}{RED('error:')} {e}")
                log(f"turn error: {e!r}")
                if "exceed" in str(e) and "context" in str(e):
                    out(GRAY("context is full - run /compact or /clear"))
                self.messages.pop() if self.messages[-1]["role"] == "user" else None
                self.save()
                emit("turn_end", secs=round(time.time() - t_turn, 1), steps=steps, interrupted=True)
                return
            finally:
                active.clear()
            self.messages.append(msg)
            self.ctx_used = st["prompt"] + st["gen"]
            self.last_stats = st
            self.emit_stats(st, time.time() - t_turn, steps)
            if st.get("finish") == "length":
                # Cut off at max_tokens (INK-track O18: two 16k-token replies in one run, ~25 min wasted). A
                # half-written tool call would fail and be regenerated whole; drop it and say how to do it in parts.
                out(YELLOW("[reply cut off at the token limit - asking for smaller pieces]"))
                partial = [c["function"]["name"] for c in msg.get("tool_calls") or []]
                msg.pop("tool_calls", None)
                self.messages.append({"role": "user", "content": "[harness] Your last reply was cut off at the "
                    f"{max(st.get('gen') or 0, MAX_TOKENS)}-token limit" + (f" while writing a {partial[-1]} call, so "
                    "it was not executed" if partial else "") + ". Work in smaller pieces: think briefly, then act. "
                    "Write a big file in parts - create it with the first ~150 lines, then add the rest with "
                    "edit_file - and run it before writing more."})
                continue
            if not msg.get("tool_calls") and self.plan_mode and AUTO_BUILD and self.plan:
                said = (msg.get("content") or "").strip()
                if said.endswith("?"):
                    pass  # it needs a decision from the user: stop and wait for the answer
                elif critiques < 2 and "PLAN SOLID" not in said.upper():
                    critiques += 1
                    out(GRAY("  checking the plan before building…"))
                    self.messages.append({"role": "user", "content": "[harness] Before building, check the plan is "
                        "solid:\n1. Every key point / requirement of the user is covered by a step.\n2. No open "
                        "unknowns: you have read the code each step will touch; the stack and approach are decided.\n"
                        "3. Every step is small, testable on its own, and says how it will be verified.\n4. Order: "
                        "foundations first; each step leaves the project working.\nIf something is missing, fix it "
                        "with the plan tool. If a decision is genuinely the user's (an ambiguous requirement, money, "
                        "deploying, deleting data), ask them - end your reply with the question. Otherwise reply "
                        "exactly: PLAN SOLID\n\n" + self.brief_text()})
                    continue
                elif self.plan_is_solid():
                    self._auto_build = True
                    break
            if not msg.get("tool_calls"):
                # don't let it quit with an unfinished plan (once per turn), unless it is asking the user something
                open_steps = [s for s in self.plan if s.get("status") in ("todo", "doing")]
                if open_steps and not nudged and not self.plan_mode and not (msg.get("content") or "").rstrip().endswith("?") and steps < MAX_STEPS:
                    nudged = True
                    self.messages.append({"role": "user", "content": "[harness] Your plan still has open steps: " +
                                          "; ".join(s["step"] for s in open_steps) + ". Continue working on them, "
                                          "or update the plan (mark dropped with a reason) if they are no longer needed."})
                    continue
                asking = (msg.get("content") or "").rstrip().endswith("?")
                # run-based verification: the project's real checks for the dirs this turn changed. Failures go back
                # to the model (max CHECK_ROUNDS times); re-run only if it edited something since the last run.
                if (AUTO_CHECKS and self.turn_files and not self.plan_mode and not asking
                        and check_rounds < CHECK_ROUNDS and checked_at != self.turn_changes):
                    check_rounds += 1
                    checked_at = self.turn_changes
                    rep, ok, n = self.project_check(changed=sorted(self.turn_files), label="Auto-check")
                    if self.stop.is_set():
                        out(f"{CLR}{YELLOW('⏹ interrupted')}")
                        break
                    if n and not ok:
                        self.count_failures(rep)
                        last = check_rounds >= CHECK_ROUNDS
                        self.messages.append({"role": "user", "content": "[harness] Automatic checks after your "
                                              "changes FAILED:\n" + rep + "\n\nFind the root cause and fix it. Don't "
                                              "weaken, skip or delete tests. If a failure is clearly unrelated to your "
                                              "changes (it was already failing before), say so instead of changing "
                                              "unrelated code." + (" This is the last automatic check round." if last else "")})
                        continue
                    check_report = rep if n else ""
                # plan -> execute -> check: before finishing a turn that changed files, check against the goal once
                big = self.turn_changes >= 2 or len(self.plan) >= 3
                if VERIFY and big and self.turn_changes and not verified and not self.plan_mode and not asking:
                    verified = True
                    out(GRAY("  checking the work against the goal…"))
                    self.messages.append({"role": "user", "content": "[harness] Before you finish, check your work "
                                          "against the goal" + (" and every key point" if self.key_points else "") +
                                          ":\n" + self.brief_text(plan=False) +
                                          ("\nAutomatic checks passed:\n" + check_report if check_report else "") +
                                          "\nDefinition of done: every requirement implemented (no TODO, stub "
                                          "or placeholder), typecheck/lint/tests pass and the thing actually ran, all "
                                          "UI states handled, inputs validated and the server authoritative, README "
                                          "and .env.example current where the project has them." +
                                          (("\nLeft-over scratch files to delete: " + ", ".join(self.scratch_files()))
                                           if self.scratch_files() else "") +
                                          "\nFor each requirement: is it done, and "
                                          "how do you know (what you ran and saw)? If anything is missing, broken or "
                                          "unverified, fix or verify it now. When everything checks out, reply with one "
                                          "short line saying what you verified; don't repeat your summary." +
                                          (f" Also: this took {steps} steps. If you worked out a reusable procedure "
                                           "for this project (how to build/run/deploy/debug something), save it as a "
                                           f"skill: write_file {STATE / 'skills'}/<kebab-name>/SKILL.md starting with "
                                           "front matter (---, name: ..., description: one line saying when to use it, "
                                           "---) and then the exact steps and commands. Skip this if nothing is reusable."
                                           if steps >= 12 else "")})
                    continue
                if self.steer:  # typed while the model was finishing: handle it in this turn
                    self.messages.append({"role": "user", "content": "[the user, while you were working] " +
                                          " / ".join(self.steer)})
                    self.steer = []
                    continue
                if self.turn_changes and not self.plan_mode and not asking and not getattr(self, "_reviewed", False):
                    self._reviewed = True
                    findings = self.auto_review()
                    if findings:
                        self.messages.append({"role": "user", "content": "[harness] An independent reviewer read "
                                              "your diff and reports:\n" + findings + "\n\nFix the real problems "
                                              "(verify each claim against the code first - the reviewer can be wrong), "
                                              "then finish with one short line."})
                        continue
                fb = self.hooks.stop(msg.get("content") or "") if self.hooks and stop_hooks < 3 else None
                if fb:
                    stop_hooks += 1
                    self.messages.append({"role": "user", "content": "[Stop hook] " + fb})
                    continue
                break
            stop_turn = False
            results = []
            for tc in msg["tool_calls"]:
                if stop_turn:
                    results.append({"role": "tool", "tool_call_id": tc["id"], "content": "Skipped."})
                    continue
                result, stop_turn = self.run_tool(tc)
                results.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                self.track(tc, result)
            note = self.guidance(steps)
            tsn = self.typecheck_step()
            if tsn:
                note = (note + "\n\n" if note else "") + tsn
            if self.steer:  # guidance typed while the model works: appended, so the KV cache stays valid
                note = (note + "\n\n" if note else "") + "[the user, while you work] " + " / ".join(self.steer)
                self.steer = []
            if note:  # appended to the last tool result: history stays append-only, so the KV cache stays valid
                results[-1]["content"] += "\n\n" + note
            self.messages.extend(results)
            self.save()
            if stop_turn:
                break
            if steps >= MAX_STEPS:
                out(YELLOW(f"stopped after {MAX_STEPS} steps - say 'continue' to keep going"))
                break
            if self.ctx_used > self.compact_limit():
                out(YELLOW("context is getting large - compacting to keep ink fast…"))
                self.compact()
        if self.plan_mode and self.plan and not self._auto_build:
            out(GRAY("  plan mode · review the plan above · /build to execute · /plan off to switch back"))
        self.save()
        self.footer(time.time() - t_turn, steps)
        emit("turn_end", secs=round(time.time() - t_turn, 1), steps=steps, interrupted=self.stop.is_set())
        if self.ctx_used > 0.95 * self.compact_limit():
            self.compact()
        self.persist_kv(wait=getattr(self, "once", False))
        self.trace_messages()
        if self._auto_build and not self.stop.is_set():
            self._auto_build = False
            out(f"{ACCENT('●')} {BOLD('Plan is solid')} {GRAY('- switching to build mode')}")
            self.set_mode("build")
            self.display_prompt = "▶ plan approved automatically - building"
            self.turn("[harness] The plan is solid and approved. Build it now, step by step: small pieces, run each "
                      "one, mark steps done as they pass, update the plan as you learn.")

    # -- self-correction: plan freshness and loop detection
    def track(self, tc, result):
        f = tc["function"]
        sig = f["name"] + ":" + (f["arguments"] or "")
        self.sigs[sig] = self.sigs.get(sig, 0) + 1
        failed = result.startswith("Error") or "[exit code" in result[-40:] or "[timed out" in result
        self.fail_streak = self.fail_streak + 1 if failed else 0
        self.since_plan = 0 if f["name"] == "plan" else self.since_plan + 1
        if f["name"] == "check" or (f["name"] == "run" and TEST_CMD.search(f.get("arguments") or "")):
            if not self.count_failures(result) and not failed:
                self.churn = {}  # a passing check: the edits so far led somewhere
        if f["name"] == "run" and getattr(self, "shell_hints", 0) < 2:
            try:
                cmd = str(json.loads(f["arguments"] or "{}").get("command", ""))
            except Exception:
                cmd = ""
            if READ_SHELL.match(cmd):
                self.shell_hints = getattr(self, "shell_hints", 0) + 1
                self.notes.append("For looking at files use read_file / glob / grep, not cat/ls/find in run: they are "
                                  "smaller, tracked (edits stay safe) and don't need `cd`.")
        self.last_sig, self.last_failed = sig, failed

    def guide_brief(self, name, limit=2600):
        """The guide's '## 0.' top-rules section (or its Checklist): short enough to put in front of the model
        directly - a small model follows rules it is shown far more reliably than 'go read file X'."""
        try:
            text = (GUIDES_DIR / name).read_text("utf-8", "replace")
        except Exception:
            return ""
        m = re.search(r"^## 0\..*?$(.*?)(?=^## |\Z)", text, re.M | re.S) or re.search(r"^## Checklist.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
        body = (m.group(1) if m else text[:limit]).strip()
        return body[:limit]

    def request_briefs(self, text):
        """Pick the playbooks this request needs (max 3) and inline their top rules."""
        picked = []
        for name, rx in REQUEST_TRIGGERS:
            if len(picked) < MAX_BRIEFS and name not in self.hinted and (GUIDES_DIR / name).is_file() and rx.search(text):
                self.hinted.add(name)
                picked.append(name)
        if not picked:
            return ""
        out(GRAY("  standards: " + ", ".join(n[:-3] for n in picked)))
        parts = [f"### {n[:-3]} (full playbook: {GUIDES_DIR / n})\n{self.guide_brief(n)}" for n in picked]
        return ("\n\n[harness] Standards for this request - follow them; read the full playbook before designing "
                "anything they cover:\n" + "\n\n".join(parts))

    def orientation(self):
        """First prompt of a session in an existing project: hand the model a map of the code up front, so it
        starts from what is there instead of spending its first steps on ls/cat."""
        if not ORIENT or not is_project(self.cwd):
            return ""
        try:
            res, summ = repo_map(self.cwd, ".", None, budget=ORIENT_TOKENS * 4)
        except Exception:
            return ""
        if not res or res.startswith(("Error", "No ")):
            return ""
        return ("\n\n[harness] Project overview of " + str(self.cwd) + " (generated, so you can skip exploring; "
                "read_file the parts you will change):\n" + res)

    def typecheck_step(self):
        """TypeScript edits this step: run tsc right away (the bugs of a 300-line untested file surface at once,
        not 40 steps later). Turned off for this session if the project is too slow to typecheck."""
        files = [f for f in getattr(self, "step_files", set()) if f.lower().endswith((".ts", ".tsx", ".mts", ".cts"))]
        self.step_files = set()
        if not files or not TS_AFTER_EDIT or getattr(self, "ts_slow", False):
            return ""
        res = ts_errors(files, timeout=60)
        if res is None:
            return ""
        text, secs, n = res
        if secs > 25:
            self.ts_slow = True
        if not n:
            return ""
        out(f"  {GRAY(' ')}  {YELLOW(f'⚠ tsc: {n} type error(s)')}")
        return "[harness] tsc --noEmit after your edit found type errors:\n" + text

    def guidance(self, steps):
        notes = []
        if self.sigs.get(self.last_sig, 0) >= 3:
            notes.append("You have made this exact call 3+ times. Stop repeating it: re-think the approach, "
                         "update the plan, or ask the user if you are blocked.")
            self.sigs[self.last_sig] = 0
        if self.fail_streak >= 3:
            notes.append(f"The last {self.fail_streak} tool calls failed. Step back: re-read the relevant code or error, "
                         "find the root cause, and revise the plan before trying again.")
            self.fail_streak = 0
        if self.plan:
            if self.since_plan >= PLAN_STALE and any(s["status"] in ("todo", "doing") for s in self.plan):
                notes.append("Your plan was last updated several steps ago. If you finished a step or learned "
                             "something that changes the plan, call plan with the updated list.")
                self.since_plan = 0
            elif self.last_failed and self.since_plan > 0:
                notes.append("That failed. If this changes your approach, update the plan.")
        elif steps == 3 and not self.plan_hinted:
            self.plan_hinted = True
            notes.append("If this task needs more than a few steps, call plan to write a checklist first "
                         "(with the goal and key points).")
        if self.fail_counts:
            name, n = max(self.fail_counts.items(), key=lambda kv: kv[1])
            level = self.stuck_noted.get(name, 0)
            if n >= STUCK_AT and level == 0:
                self.stuck_noted[name] = 1
                notes.append(f"`{short(name, 120)}` has now failed {n} times. Stop patching and diagnose: write one "
                             "line each for (a) the code is wrong, (b) the test's expected value is wrong, (c) the "
                             "test's assumptions don't match what the code promises (e.g. a simplified strategy vs a "
                             "textbook number, rounding, randomness, timing). Check the cheapest one first with a "
                             "small experiment. An expected value from theory only holds if the code meets the same "
                             "assumptions - if it doesn't, fix the test (with a comment why), not the code.")
            elif n >= STUCK_AT + 2 and level == 1:
                self.stuck_noted[name] = 2
                advice = self.escalate(name)
                if advice:
                    notes.append("You were stuck, so a fresh debugger (a separate agent with a clean context) "
                                 "investigated the failure, the code and your diff. Its diagnosis - verify it against "
                                 "the code, then apply it:\n" + advice)
                else:
                    notes.append(f"`{short(name, 120)}` still fails after {n} attempts. Re-plan: split this step, or "
                                 "mark the check as a known issue in the plan with what you found, and move on to the "
                                 "next step - don't spend more attempts on the same fix.")
        hot = [(f, n) for f, n in self.churn.items() if n >= CHURN_AT and self.churn_noted.get(f, 0) < n // CHURN_AT]
        if hot:
            f, n = max(hot, key=lambda x: x[1])
            self.churn_noted[f] = n // CHURN_AT
            notes.append(f"You have edited {Path(f).name} {n} times without a passing check in between - that is "
                         "trial and error. Stop editing it. Re-read the whole function you are changing, write down "
                         "in one sentence what it must do and why the current version doesn't, run one small "
                         "experiment that proves the cause, then make one fix.")
        cur = self.current_step()
        clock = getattr(self, "step_clock", None)
        if cur and (not clock or clock["step"] != cur):
            self.step_clock = {"step": cur, "t": time.time(), "warned": 0}
        elif cur and clock and STEP_BUDGET_MIN > 0:
            mins = (time.time() - clock["t"]) / 60
            if mins >= STEP_BUDGET_MIN * (clock["warned"] + 1):
                clock["warned"] += 1
                notes.append(f"The step \"{short(cur, 100)}\" has taken {mins:.0f} minutes. Re-plan now: is it really "
                             "one step? Split it into smaller steps with the plan tool, or record what is left as a "
                             "later step and move on; keep the project working.")
        notes += self.notes
        self.notes = []
        text = ("[harness] " + " ".join(notes)) if notes else ""
        # pinned brief: re-show goal, key points and plan near the end of the context during long work
        if (self.goal or self.key_points or self.plan) and (
                steps - self.pin_step >= PIN_EVERY or self.ctx_used - self.pin_ctx >= PIN_TOKENS):
            self.pin_step, self.pin_ctx = steps, self.ctx_used
            text = (text + "\n\n" if text else "") + "[pinned - keep these in mind]\n" + self.brief_text()
        return text

    def brief_text(self, plan=True):
        """The pinned brief: goal, key points, the user's recent requests and the plan."""
        parts = []
        if self.goal:
            parts.append("Goal: " + self.goal)
        if self.key_points:
            parts.append("Key points:\n" + "\n".join(f"- {k}" for k in self.key_points))
        reqs = [r for r in self.requests if not r.startswith("[")][-4:]
        if reqs:
            parts.append("User's requests (latest last):\n" + "\n".join(f"- {short(r, 300)}" for r in reqs))
        if plan and self.plan:
            parts.append("Plan ([x] done, [>] doing, [ ] todo, [-] dropped):\n" + self.plan_text())
        return "\n".join(parts)

    # -- events for UIs
    def emit_stats(self, st, secs, steps):
        if not RPC:
            return
        if not hasattr(self, "_branch") or time.time() - self._branch[1] > 30:
            try:
                b = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.tools.cwd, capture_output=True,
                                   text=True, timeout=5).stdout.strip()
            except Exception:
                b = ""
            self._branch = (b, time.time())
        prompt = st.get("prompt") or 0
        emit("stats", tps=round(st.get("tps", 0), 1), pps=round(st.get("pps", 0), 1), ctx_used=self.ctx_used,
             ctx=self.P["ctx"], cache_hit=round(st.get("cached", 0) / prompt, 3) if prompt else 0, secs=round(secs, 1),
             steps=steps, model=self.P["title"], effort=self.effort, mode=self.mode_name(), branch=self._branch[0],
             jobs=self.jobs.status_count() if getattr(self, "jobs", None) else 0)

    def mode_name(self):
        return "plan" if self.plan_mode else ("auto" if self.yolo else "safe")

    def say(self, text):
        out(f"  {GRAY('⎿')}  {GRAY(text)}")

    def log(self, msg):
        log(msg)

    # -- subagents, auto-review, specs
    def all_tools(self):
        return TOOLS

    def sub_tool(self, tc, who):
        """Run a subagent's tool call with its own read/dedup state (the main context never saw those reads)."""
        main = self.tools
        if getattr(self, "_sub_tools", None) is None or self._sub_tools.cwd != main.cwd:
            self._sub_tools = Tools(main.cwd)
        self.tools = self._sub_tools
        try:
            res, _ = self.exec_tool(tc)
        finally:
            self.tools = main
        return res

    def run_subagent(self, kind, task, context=""):
        from agents import Subagent, Router, load_agents
        spec = load_agents(self.cwd).get(kind)
        if not spec:
            raise ToolError(f"unknown agent kind {kind!r}")
        self.router = getattr(self, "router", None) or Router(log)
        self._sub_tools = Tools(self.tools.cwd)
        return Subagent(self, spec, self.router).run(task, context)

    def auto_review(self):
        """Independent review of this turn's diff by a fresh-context reviewer (dsh-style auto-review)."""
        if AUTO_REVIEW == "off" or not getattr(self, "turn_cp", None):
            return None
        diff = self.cp.diff_since(self.turn_cp)
        lines = sum(1 for l in diff.splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---")))
        if lines < REVIEW_MIN_LINES:
            return None
        out(f"{ACCENT('●')} {BOLD('Review')}({lines} changed lines)")
        task = ("Review this change.\nGoal: " + (self.goal or short(self.requests[-1] if self.requests else "", 600)) +
                ("\nKey points:\n" + "\n".join(f"- {k}" for k in self.key_points) if self.key_points else "") +
                "\n\nThe quality bar (report violations that matter, not style nits):\n" +
                self.guide_brief("engineering-standard.md", 2200) +
                "\n\nDiff:\n" + diff[:30000])
        try:
            rep = self.run_subagent("review", task)
        except Exception as e:
            log(f"auto-review failed: {e!r}")
            return None
        if not rep or rep.startswith(("Subagent failed", "Subagent interrupted", "(the subagent")):
            self.say("review: skipped (the reviewer gave no report)")
            log(f"auto-review gave no report: {short(rep or '', 200)}")
            return None
        if "NO_ISSUES" in rep[:300]:
            self.say("review: no issues found")
            return None
        first = next((l for l in rep.splitlines() if l.strip()), "")
        self.say("review: " + short(first, 120))
        return rep

    def save_spec(self):
        """Droid-style specs: a plan with a goal is kept as markdown (~/.ink/specs), searchable later."""
        if not self.goal or len(self.plan) < 3:
            return
        try:
            d = STATE / "specs"
            d.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^a-z0-9]+", "-", self.goal.lower())[:50].strip("-")
            f = d / f"{self.session_file.stem}-{slug}.md"
            body = [f"# {self.goal}", f"project: {self.cwd}", f"session: {self.session_file.stem}", ""]
            if self.key_points:
                body += ["## Key points"] + [f"- {k}" for k in self.key_points] + [""]
            body += ["## Plan"] + [f"- [{'x' if s['status'] == 'done' else ' '}] {s['step']}"
                                   + (" (dropped)" if s["status"] == "dropped" else "") for s in self.plan]
            f.write_text("\n".join(body) + "\n", "utf-8")
        except Exception:
            pass

    # -- user input: ! shell, # memory, & background agent, @file mentions
    def user_input(self, text):
        text = text.strip()
        if not text:
            return
        self.display_prompt = text
        if text.startswith("!"):
            cmd = text[1:].strip()
            out(f"{ACCENT('!')} {BOLD(cmd)}")
            res, summ = self.tools.run(cmd, 300, stop=self.stop)
            for l in res.splitlines()[-40:]:
                out("  " + l)
            self.pending.append(f"[The user ran `{cmd}` in the shell ({summ}):]\n{trunc_middle(res, 4000)}")
            return
        if text.startswith("#"):
            note = text[1:].strip()
            MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            f = MEMORY_DIR / "user-notes.md"
            old = f.read_text("utf-8") if f.exists() else "# Notes from the user\n"
            f.write_text(old.rstrip() + f"\n- {note}\n", "utf-8")
            idx = MEMORY_DIR / "MEMORY.md"
            lines = idx.read_text("utf-8").splitlines() if idx.exists() else ["# Ink memory"]
            if not any("(user-notes.md)" in l for l in lines):
                idx.write_text("\n".join(lines + ["- [Notes from the user](user-notes.md) - things the user told ink to remember"]) + "\n", "utf-8")
            out(GRAY(f"saved to memory: {short(note, 100)}"))
            return
        if text.startswith("&"):
            task = text[1:].strip()
            kind = task.split()[0].lower() if task.split() and task.split()[0].lower() in ("explore", "review", "test") else "explore"
            out(f"{ACCENT('&')} {BOLD(kind)} agent: {short(task, 90)}")
            rep = self.run_subagent(kind, task)
            out(rep)
            self.pending.append(f"[A background {kind} agent the user started reported:]\n{rep[:6000]}")
            return
        attach = []
        for m in re.finditer(r"(?<![\w@])@([\w.\-/\\:]+)", text):
            p = self.tools.path(m.group(1).rstrip(".,;:"))
            if p.is_file():
                try:
                    body, _ = self.tools.read_file(str(p), 1, 400)
                    attach.append(f"[@{self.tools.rel(p)}]\n{body}")
                except Exception:
                    pass
            elif p.is_dir():
                body, _ = self.tools.read_file(str(p))
                attach.append(f"[@{self.tools.rel(p)}/ (folder)]\n{body}")
        if self.pending:
            text = "\n\n".join(self.pending) + "\n\n" + text
            self.pending = []
        if attach:
            text += "\n\n" + "\n\n".join(attach)
        with self.model_lock:
            self.turn(text)
        self.turns_since_dream += 1

    def footer(self, secs, steps):
        if RPC:
            return
        st = getattr(self, "last_stats", {})
        pct = 100 * self.ctx_used / self.P["ctx"]
        bits = [f"{secs:.0f}s", f"{st.get('tps', 0):.1f} tok/s", f"ctx {ktok(self.ctx_used)}/{ktok(self.P['ctx'])} ({pct:.0f}%)"]
        if steps > 1:
            bits.insert(1, f"{steps} steps")
        out(GRAY("  " + " · ".join(bits)))

    # -- tool execution with permission + display
    def run_tool(self, tc):
        """exec_tool plus the hooks (PreToolUse/PostToolUse) and, in --rpc mode, tool_start/tool_end events
        (the plain lines a tool prints become its summary and preview)."""
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"] or "{}")
            args = normalize_args(name, args) if isinstance(args, dict) else {}
        except Exception:
            args = {}
        blocked = self.hooks.pre_tool(name, args, self.tools.cwd) if self.hooks else None
        if blocked:
            out(f"{YELLOW('●')} {self.describe(name, args)}  {GRAY('blocked by hook')}")
            return f"Error: blocked by a PreToolUse hook: {blocked}", False
        self.cur_tool = tc.get("id") or f"t{time.time()}"
        if RPC:
            emit("tool_start", id=self.cur_tool, name=name, label=ANSI_RX.sub("", self.describe(name, args)),
                 args={k: (v if len(str(v)) < 400 else str(v)[:400] + "…") for k, v in args.items()})
            RPC.capture = []
        try:
            result, stop = self.exec_tool(tc)
        finally:
            lines = RPC.capture if RPC else None
            if RPC:
                RPC.capture = None
        if RPC:
            body = [l for l in (lines or []) if l.strip() and not l.lstrip().startswith(("●", "@@", "+", "-"))]
            summ = next((l.split("⎿", 1)[1].strip() for l in body if "⎿" in l), "") or short(result.splitlines()[0] if result else "", 120)
            preview = [l.strip(" ⎿") for l in body if "⎿" not in l][:6]
            emit("tool_end", id=self.cur_tool, ok=not (result.startswith("Error") or "[exit code" in result[-40:]),
                 summary=summ, preview=preview, output=result[:20000])
        if self.hooks:
            extra = self.hooks.post_tool(name, args, result, self.tools.cwd)
            if extra:
                result += "\n\n[PostToolUse hook]\n" + extra
        return result, stop

    def exec_tool(self, tc):
        name = tc["function"]["name"]
        raw = tc["function"]["arguments"] or "{}"
        try:
            args = json.loads(raw)
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
        except Exception as e:
            out(f"{RED('●')} {name}  {RED('bad arguments')}")
            return f"Error: tool arguments are not valid JSON ({e}). Send a JSON object.", False
        args = normalize_args(name, args)
        if name in ("read_file", "edit_file", "write_file") and not str(args.get("path") or "").strip():
            out(f"{RED('●')} {name}  {RED('missing file')}")
            return f"Error: {name} needs the `file` argument (the file path). Nothing was changed.", False
        known = name in {t["function"]["name"] for t in TOOLS}
        fn = getattr(self.tools, name, None)
        label = self.describe(name, args)
        if not known:
            out(f"{RED('●')} {label}")
            return f"Error: unknown tool {name}. Available: {', '.join(t['function']['name'] for t in TOOLS)}", False
        if self.plan_mode and name not in READ_ONLY:
            cmd = str(args.get("command", ""))
            ro = (name == "run" and PLAN_SAFE.match(cmd)
                  and not re.search(r"[>|&;]|\brm\b|\bdel\b|-delete|-exec\b", cmd) and not dangerous_cmd(cmd))
            if not ro:
                out(f"{YELLOW('●')} {label}")
                out(f"  {GRAY('⎿')}  {YELLOW('plan mode: changes disabled')}")
                return ("Error: plan mode is on - edit_file, write_file and mutating commands are disabled. Research "
                        "with read_file/grep/glob and read-only commands (git status/diff/log), record steps with the "
                        "plan tool, then present the plan for approval."), False
        try:
            if name == "plan":
                return self.set_plan(args.get("steps"), args.get("goal"), args.get("key_points")), False
            if name == "recall":
                q = str(args.get("query") or "")
                out(f"{ACCENT('●')} {BOLD('Recall')}({short(q, 90)})")
                res = recall_search(RECALL_DB, MEMORY_DIR, q, exclude=self.session_file.name)
                n = res.count("\n[session ") + res.count("\n[memory note ") + (1 if res.startswith("[") else 0)
                out(f"  {GRAY('⎿')}  {GRAY(f'{n} match(es)' if res.startswith('[') else short(res, 100))}")
                return res, False
            if name in ("edit_file", "write_file"):
                return self._exec_write(name, args, label)
            if name == "code":
                return self._exec_code(args)
            if name == "ask_claude":
                return self._ask_claude(str(args.get("question") or ""))
            if name == "job" and self.jobs:
                out(f"{ACCENT('●')} {label}")
                act = str(args.get("action") or "")
                if act == "start":
                    ok, reason = self.permit("run", str(args.get("command") or ""))
                    if not ok:
                        return self.denied(reason)
                res, summ = run_job_tool(self.jobs, args)
                out(f"  {GRAY('⎿')}  {GRAY(short(summ, 110))}")
                return res, False
            if name == "web":
                out(f"{ACCENT('●')} {label}")
                res, summ = web_tool(args)
                out(f"  {GRAY('⎿')}  {GRAY(summ)}")
                return self.maybe_compress(res), False
            if name == "agent":
                kind, task = str(args.get("kind") or "explore"), str(args.get("task") or "")
                if self.plan_mode and kind == "test":
                    raise ToolError("plan mode: the test agent can change files; use explore or review")
                out(f"{ACCENT('●')} {BOLD('Agent')}({kind}: {short(task, 80)})")
                rep = self.run_subagent(kind, task)
                return rep, self.stop.is_set()
            if name == "map":
                out(f"{ACCENT('●')} {label}")
                res, summ = repo_map(self.tools.cwd, str(args.get("path") or "."), args.get("query") or None)
                out(f"  {GRAY('⎿')}  {GRAY(summ)}")
                return res, False
            if name == "check":
                rep, ok, n = self.project_check(only=args.get("only") or None)
                return rep, self.stop.is_set()
            if name == "playtest" and run_playtest:
                out(f"{ACCENT('●')} {label}")
                ok, reason = self.permit("run", "playtest " + json.dumps(args)[:200])
                if not ok:
                    return self.denied(reason)
                self.stop.clear()
                active = threading.Event()
                active.set()
                threading.Thread(target=self._watch_keys, args=(active,), daemon=True).start()
                try:
                    res, summ = run_playtest(args, str(self.tools.cwd), stop=self.stop)
                finally:
                    active.clear()
                col = GREEN if " PASS" in f" {summ}" and "FAIL" not in str(summ) else RED
                out(f"  {GRAY('⎿')}  {col(short(summ, 110))}")
                return res, self.stop.is_set()
            if name == "run":
                cmd = args.get("command", "")
                out(f"{ACCENT('●')} {BOLD('Run')}({short(cmd, 100)})")
                for f in shell_read_files(cmd):
                    if "*" not in f and self.tools.path(f).is_file():
                        self.tools.read_paths.add(str(self.tools.path(f)))
                if SHELL_EDIT.search(cmd):
                    # INK-track O20: models edit source with `sed -i` despite the prompt; that skips the diff, undo,
                    # syntax/type checks and stale-read protection. Refuse and point at the right tool.
                    out(f"  {GRAY('⎿')}  {YELLOW('use edit_file to change files')}")
                    return ("Not run: this edits a file in place from the shell. Use edit_file (find/replace or "
                            "lines) so the change shows as a diff, can be undone and gets checked. Run the test "
                            "command on its own afterwards."), False
                ok, reason = self.permit("run", cmd)
                if not ok:
                    return self.denied(reason)
                self.stop.clear()
                active = threading.Event()
                active.set()
                threading.Thread(target=self._watch_keys, args=(active,), daemon=True).start()
                try:
                    res, summ = self.tools.run(cmd, args.get("timeout") or 120, stop=self.stop)
                finally:
                    active.clear()
                if self.stop.is_set():
                    out(f"  {GRAY('⎿')}  {YELLOW('interrupted')}")
                    return res, True
                preview = [l for l in res.splitlines() if l.strip()][:4]
                for k, l in enumerate(preview):
                    out(f"  {GRAY('⎿' if k == 0 else ' ')}  {GRAY(short(l, 110))}")
                more = len(res.splitlines()) - len(preview)
                if more > 0:
                    out(f"     {GRAY(f'… +{more} lines')}")
                if not preview:
                    out(f"  {GRAY('⎿')}  {GRAY('(no output)')}")
                return self.maybe_compress(res), False
            # read-only tools
            out(f"{ACCENT('●')} {label}")
            res, summ = fn(**{k: v for k, v in args.items() if v is not None})
            out(f"  {GRAY('⎿')}  {GRAY(summ)}")
            if name == "read_file":
                self.guide_hint(str(self.tools.path(args.get("path", ""))), res)
            return res, False
        except ToolError as e:
            out(f"  {GRAY('⎿')}  {RED(short(e, 140))}")
            return f"Error: {e}", False
        except TypeError as e:
            out(f"  {GRAY('⎿')}  {RED(short(e, 140))}")
            return f"Error: bad arguments for {name}: {e}", False
        except Exception as e:
            out(f"  {GRAY('⎿')}  {RED(short(repr(e), 140))}")
            return f"Error: {e!r}", False

    def set_plan(self, steps, goal=None, key_points=None):
        if not isinstance(steps, list) or not all(isinstance(s, dict) and s.get("step") for s in steps):
            raise ToolError('steps must be a list of {"step": "...", "status": "todo|doing|done|dropped"}')
        if goal and str(goal).strip():
            self.goal = str(goal).strip()
        if isinstance(key_points, list) and key_points:
            self.key_points = [str(k).strip() for k in key_points if str(k).strip()][:15]
        if self.goal:
            out(f"{ACCENT('●')} {BOLD('Goal')} {GRAY(short(self.goal, 120))}")
            for k in self.key_points:
                out(f"  {GRAY('•')} {GRAY(short(k, 120))}")
        old = {s["step"]: s["status"] for s in self.plan}
        self.plan = [{"step": str(s["step"]), "status": s.get("status") if s.get("status") in
                      ("todo", "doing", "done", "dropped") else "todo"} for s in steps]
        emit("plan", goal=self.goal, key_points=self.key_points, steps=self.plan)
        self.save_spec()
        verb = "Plan" if not old else "Plan updated"
        out(f"{ACCENT('●')} {BOLD(verb)}")
        for k, s in enumerate(self.plan):
            st, txt = s["status"], s["step"]
            new = txt not in old
            if st == "done":
                line = GREEN("✔ ") + GRAY(txt)
            elif st == "dropped":
                line = GRAY("✘ \x1b[9m" + txt + "\x1b[29m")
            elif st == "doing":
                line = ACCENT("▶ ") + BOLD(txt)
            else:
                line = GRAY("☐ ") + txt
            if new and old:
                line += " " + CYAN("(new)")
            out(f"  {GRAY('⎿' if k == 0 else ' ')}  {line}")
        res = ""
        newly = [s for s in self.plan if s["status"] == "done" and old.get(s["step"]) not in (None, "done")]
        files = sorted(getattr(self, "gate_files", set()))
        if newly and files and AUTO_CHECKS and STEP_CHECKS and not self.plan_mode:
            self.gate_files = set()
            rep_, ok, n = self.project_check(changed=files, label="Step check")
            if n and not ok:
                self.count_failures(rep_)
                for s in newly:
                    s["status"] = "doing"
                emit("plan", goal=self.goal, key_points=self.key_points, steps=self.plan)
                res = ("\n\nThe project checks FAILED after this step, so it is back to doing:\n" + rep_ +
                       "\nFix the cause, then mark it done again.")
            elif n:
                res = "\nStep check passed (" + short(rep_.splitlines()[0] if rep_ else "ok", 120) + ")."
        if newly:
            junk = self.scratch_files()
            if junk:
                res += ("\nLeft-over scratch files in the project: " + ", ".join(junk) + " - delete them (or turn "
                        "them into real tests under tests/) so they don't get committed.")
        n_open = sum(s["status"] in ("todo", "doing") for s in self.plan)
        nxt = next((s["step"] for s in self.plan if s["status"] in ("doing", "todo")), None)
        return (f"Plan saved: {n_open} open, {sum(s['status'] == 'done' for s in self.plan)} done." + res +
                (f"\nNext: {nxt}" if nxt and not res.startswith("\n\nThe project") else ""))

    def plan_text(self):
        marks = {"done": "[x]", "doing": "[>]", "todo": "[ ]", "dropped": "[-]"}
        return "\n".join(f"{marks[s['status']]} {s['step']}" for s in self.plan)

    def describe(self, name, a):
        if name == "read_file":
            extra = f":{a.get('offset')}" if a.get("offset") else ""
            return f"{BOLD('Read')}({a.get('path', '')}{extra})"
        if name == "grep":
            return f"{BOLD('Search')}({short(a.get('pattern', ''), 60)}{' in ' + a['path'] if a.get('path') else ''})"
        if name == "glob":
            return f"{BOLD('Glob')}({a.get('pattern', '')})"
        if name == "edit_file":
            return f"{BOLD('Edit')}({a.get('path', '')})"
        if name == "write_file":
            return f"{BOLD('Write')}({a.get('path', '')})"
        if name == "map":
            return f"{BOLD('Map')}({a.get('path') or '.'}{' · ' + short(a['query'], 50) if a.get('query') else ''})"
        if name == "playtest":
            n = len(a.get("steps") or [])
            return f"{BOLD('Playtest')}({short(a.get('page') or a.get('url') or a.get('file') or '', 70)}{f' · {n} steps' if n else ''})"
        if name == "run":
            return f"{BOLD('Run')}({short(a.get('command', ''), 90)})"
        if name == "web":
            return f"{BOLD('Web')}({short(a.get('url') or a.get('query') or '', 80)})"
        if name == "agent":
            return f"{BOLD('Agent')}({a.get('kind', '')}: {short(a.get('task', ''), 70)})"
        if name == "check":
            return f"{BOLD('Check')}({a.get('only') or 'all'})"
        if name == "job":
            return f"{BOLD('Job')}({a.get('action', '')} {short(a.get('name') or a.get('command') or '', 70)})"
        if name == "plan":
            return f"{BOLD('Plan')}"
        if name == "recall":
            return f"{BOLD('Recall')}({short(a.get('query', ''), 70)})"
        return f"{BOLD(name)}({short(json.dumps(a), 80)})"

    def _ask_claude(self, question):
        out(f"{ACCENT('●')} {BOLD('Ask Claude')}({short(question, 100)})")
        if not question.strip():
            raise ToolError("question is empty")
        exe = shutil.which("claude")
        if not exe:
            raise ToolError("Claude Code (`claude`) is not installed or not on PATH")
        self.stop.clear()
        active = threading.Event()
        active.set()
        threading.Thread(target=self._watch_keys, args=(active,), daemon=True).start()
        prompt = ("You are advising Ink, a small local coding agent working in this directory. It will make the "
                  "changes itself, so answer with a precise diagnosis and concrete fix (exact code, file:line). "
                  "Be concise.\n\nInk's question:\n" + question)
        try:
            proc = subprocess.Popen([exe, "-p", prompt, "--allowedTools", "Read,Grep,Glob,WebSearch,WebFetch"],
                                    cwd=self.tools.cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if IS_WIN else 0)
            chunks = []
            reader = threading.Thread(target=lambda: chunks.append(proc.stdout.read()), daemon=True)
            reader.start()
            t0, i = time.time(), 0
            while reader.is_alive():
                reader.join(0.25)
                out(f"{CLR}  {ACCENT(SPIN[i % 10])} {GRAY(f'Claude is thinking… {int(time.time() - t0)}s · esc to interrupt')}", end="")
                i += 1
                if self.stop.is_set() or time.time() - t0 > 1200:
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"] if IS_WIN else ["kill", str(proc.pid)],
                                   capture_output=True)
                    reader.join(3)
                    break
            out(CLR, end="")
        finally:
            active.clear()
        text = (chunks[0] if chunks else b"").decode("utf-8", "replace").strip()
        if self.stop.is_set():
            out(f"  {GRAY('⎿')}  {YELLOW('interrupted')}")
            return "Interrupted by the user.", True
        if not text:
            text = "(Claude returned nothing)"
        for k, l in enumerate([l for l in text.splitlines() if l.strip()][:4]):
            out(f"  {GRAY('⎿' if k == 0 else ' ')}  {GRAY(short(l, 110))}")
        return trunc_middle(text, 12000), False

    def _exec_code(self, args):
        from codemode.sandbox import run_code
        code = str(args.get("code") or "")
        lines = code.strip().splitlines()
        out(f"{ACCENT('●')} {BOLD('Code')}({short(lines[0] if lines else '', 90)}{' …' if len(lines) > 1 else ''})")
        for l in lines[1:6]:
            out(f"     {GRAY(short(l, 100))}")
        ok, reason = self.permit("code", code)
        if not ok:
            return self.denied(reason)
        self.stop.clear()
        active = threading.Event()
        active.set()
        threading.Thread(target=self._watch_keys, args=(active,), daemon=True).start()
        try:
            res, summ = run_code(code, str(self.tools.cwd), int(args.get("timeout") or 120), stop=self.stop)
        finally:
            active.clear()
        preview = [l for l in res.splitlines() if l.strip()][:4]
        for k, l in enumerate(preview):
            out(f"  {GRAY('⎿' if k == 0 else ' ')}  {GRAY(short(l, 110))}")
        if len(res.splitlines()) > len(preview):
            out(f"     {GRAY(f'… +{len(res.splitlines()) - len(preview)} lines')}")
        return self.maybe_compress(res), self.stop.is_set()

    def maybe_compress(self, res):
        """Shrink long prose output (web pages, docs) with LLMLingua-2. Code, logs and test output are left alone,
        and error lines are always kept word for word."""
        if len(res) < COMPRESS_MIN or not prose_like(res) or not health(COMPRESS_PORT):
            return res
        try:
            st, j = server_post(COMPRESS_PORT, "/compress", {"text": res, "rate": COMPRESS_RATE}, timeout=120)
            if st != 200 or not j.get("text"):
                return res
        except Exception:
            return res
        errs = list(dict.fromkeys(l.strip() for l in res.splitlines() if len(l) < 300 and ERR_LINE.search(l)))[:15]
        msg = f"compressed prose output {ktok(j['orig'])} → {ktok(j['comp'])} tokens"
        out(f"     {GRAY(msg)}")
        note = (f"[prose compressed with LLMLingua from {j['orig']} to {j['comp']} tokens; filler words are dropped. "
                "If you need exact wording, print a narrower slice.]\n")
        return note + j["text"] + ("\n\n[error lines, verbatim]\n" + "\n".join(errs) if errs else "")

    def _exec_write(self, name, args, label):
        out(f"{ACCENT('●')} {label}")
        if name == "edit_file":
            if args.get("lines") and not args.get("old_string"):
                p, old, new = self.tools.line_edit(args.get("path", ""), args["lines"], args.get("new_string", ""))
            else:
                if not args.get("old_string"):
                    raise ToolError("give `find` (the exact text to replace) or `lines` (e.g. \"40-52\")")
                p, old, new = self.tools.edit_preview(args.get("path", ""), args.get("old_string", ""),
                                                      args.get("new_string", ""), bool(args.get("replace_all")))
        else:
            p, old, new = self.tools.write_preview(args.get("path", ""), args.get("content", ""))
        added, removed = self.show_diff(old or "", new, p)
        ok, reason = self.permit("write", str(p))
        if not ok:
            return self.denied(reason)
        existed, backup = self._backup(p)
        try:
            if self.cwd.resolve() in Path(p).resolve().parents:
                self.cwd_writes = getattr(self, "cwd_writes", 0) + 1
        except Exception:
            pass
        self.tools.apply_write(p, new)
        self._record_undo(p, existed, backup)
        self.turn_changes = getattr(self, "turn_changes", 0) + 1
        self.turn_files.add(str(p))
        self.gate_files = getattr(self, "gate_files", set()) | {str(p)}
        self.churn[str(p)] = self.churn.get(str(p), 0) + 1
        self.step_files = getattr(self, "step_files", set()) | {str(p)}
        self.maybe_follow_project(p)
        try:
            rel = str(Path(p).resolve().relative_to(self.cwd.resolve()))
        except ValueError:
            rel = str(Path(p).resolve())
        if rel not in self.changed:
            self.changed.append(rel)
        self.guide_hint(str(p), new)
        bad = quick_check(p)
        if bad:
            note = ("\nSYNTAX CHECK FAILED - the file is now broken. Fix this before anything else:\n" + bad)
        else:
            note = ""
            if lsp_diagnose:
                try:
                    probs = lsp_diagnose(str(p), str(self.tools.cwd))
                except Exception:
                    probs = []
                if probs:
                    note = "\nDIAGNOSTICS (likely bugs this edit introduced or exposed - check each):\n" + "\n".join(probs[:8])
                    out(f"  {GRAY(' ')}  {YELLOW('⚠ ' + short(probs[0], 110))}" + (GRAY(f" (+{len(probs) - 1})") if len(probs) > 1 else ""))
        if old is None:
            out(f"  {GRAY('⎿')}  {GRAY(f'created, {len(new.splitlines())} lines')}")
            res = f"Created {p} ({len(new.splitlines())} lines)."
        else:
            out(f"  {GRAY('⎿')}  {GREEN(f'+{added}')} {RED(f'-{removed}')}")
            res = f"Updated {p} (+{added} -{removed} lines)."
        if bad:
            out(f"  {GRAY(' ')}  {RED('✗ syntax: ' + short(bad.splitlines()[0], 110))}")
        grown = len(new.splitlines()) if old is None else added
        if not bad and grown >= BIG_WRITE and p.suffix.lower() in CODE_EXT:
            note += (f"\n[harness] That is {grown} lines of code that have never run. Run it now (typecheck, the "
                     "relevant test, or a quick script) before writing more, and keep the next pieces smaller.")
        return res + note, False

    def maybe_follow_project(self, p):
        """The model is building a project in another folder than the one ink started in (an empty or
        non-project folder): move the session there so checks, checkpoints, review and resume follow the work."""
        try:
            if self.cwd.resolve() in Path(p).resolve().parents or not FOLLOW_PROJECT:
                return
        except Exception:
            return
        if is_project(self.cwd) or getattr(self, "cwd_writes", 0):
            return
        root = project_root(p)
        if root == self.cwd or not root.is_dir():
            return
        old_cwd = self.cwd
        self.changed = [str((old_cwd / c).resolve()) if not Path(c).is_absolute() else c for c in self.changed]
        os.chdir(root)
        self.cwd = Path(root)
        self.tools.cwd = self.cwd
        self.cp = Checkpoints(self.cwd)
        self.t_session = getattr(self, "t_session", None) or time.time()
        self.fail_counts, self.fail_text, self.stuck_noted = {}, {}, {}
        self.churn, self.churn_noted = {}, {}
        self.turn_cp = self.cp.snapshot("ink: project folder adopted")
        self.hooks = Hooks(self.cwd, log)
        out(GRAY(f"  working folder → {root} (the project being built; checks, undo and resume follow it)"))
        emit("notice", level="info", text=f"working folder is now {root}")
        self.notes.append(f"The project lives in {root}, so that is now the working directory: checks, "
                          "checkpoints and session resume follow it. Paths relative to it work.")
        self.save()

    def guide_hint(self, path, text=""):
        """Point the model at the matching expert guide the first time the work touches its area."""
        if not GUIDES_DIR.is_dir():
            return
        for name, hit in GUIDE_TRIGGERS:
            if name not in self.hinted and (GUIDES_DIR / name).is_file():
                try:
                    if hit(path.replace("\\", "/").lower(), text[:20000] if text else ""):
                        self.hinted.add(name)
                        self.notes.append(f"This touches an area with an expert playbook ({GUIDES_DIR / name}). Its "
                                          "top rules - apply them to what you just wrote and to the next code:\n" +
                                          self.guide_brief(name, 1800))
                except Exception:
                    pass

    def project_check(self, only=None, changed=None, label="Checks"):
        """Run project checks; prints a compact status. Returns (report, ok, n)."""
        names = [c[0] for c in project_checks(self.tools.cwd)]
        out(f"{ACCENT('●')} {BOLD(label)}({short(only or ', '.join(names) or 'none found', 100)})")
        argv = ((lambda c: [SHELL_BASH, "-c", c]) if HAVE_BASH and IS_WIN else
                (lambda c: ["bash", "-c", c]) if HAVE_BASH else
                (lambda c: ["powershell", "-NoProfile", "-NonInteractive", "-Command", c]))
        self.stop.clear()
        t0 = time.time()
        done = threading.Event()

        def spin():
            i = 0
            while not done.is_set():
                out(f"{CLR}  {ACCENT(SPIN[i % 10])} {GRAY(f'running checks… {int(time.time() - t0)}s')}", end="")
                i += 1
                time.sleep(0.25)
            out(CLR, end="")
        th = threading.Thread(target=spin, daemon=True)
        th.start()
        try:
            rep, ok, n = run_checks(self.tools.cwd, argv, timeout=CHECK_TIMEOUT, only=only, stop=self.stop,
                                    changed=changed)
        finally:
            done.set()
            th.join(1)
        for l in [l for l in rep.splitlines() if l.startswith("- ")][:6]:
            col = GREEN if l.startswith("- PASS") else RED if l.startswith("- FAIL") else GRAY
            out(f"  {GRAY('⎿')}  {col(short(l[2:], 110))}")
        if not n:
            out(f"  {GRAY('⎿')}  {GRAY(short(rep, 110))}")
        return rep, ok, n

    def show_diff(self, old, new, p):
        a, b = old.replace("\r\n", "\n").splitlines(), new.replace("\r\n", "\n").splitlines()
        diff = list(difflib.unified_diff(a, b, lineterm="", n=2))[2:]
        added = sum(1 for l in diff if l.startswith("+"))
        removed = sum(1 for l in diff if l.startswith("-"))
        if RPC:
            emit("diff", id=getattr(self, "cur_tool", ""), path=self.tools.rel(p), added=added, removed=removed,
                 created=not old, diff="\n".join(diff[:400]) if old else "\n".join("+" + l for l in b[:200]))
            return added, removed
        shown = diff[:40] if old else [f"+{l}" for l in b[:20]]
        for l in shown:
            if l.startswith("@@"):
                out("    " + CYAN(l))
            elif l.startswith("+"):
                out("    " + GREEN(short(l, 140) if len(l) > 140 else l))
            elif l.startswith("-"):
                out("    " + RED(short(l, 140) if len(l) > 140 else l))
            else:
                out("    " + GRAY(l[:140]))
        total = len(diff) if old else len(b)
        if total > len(shown):
            out("    " + GRAY(f"… {total - len(shown)} more lines"))
        return added, removed

    def permit(self, kind, detail):
        # graded safety: inside the project everything runs in auto mode; leaving it always asks
        if kind == "run":
            for rx in (self.hooks.settings.get("deny") if self.hooks else []) or []:
                if re.search(rx, detail):
                    return False, f"This command is denied by your settings (deny: {rx}). Find another way."
            why = outward(detail) or next((f"matches your 'ask' rule {rx}" for rx in
                                           ((self.hooks.settings.get("ask") if self.hooks else []) or [])
                                           if re.search(rx, detail)), None)
            if why:
                if re.search(r"\bgit\s+push\b", detail):
                    try:
                        leaks = secret_scan(self.tools.cwd)
                    except Exception as e:
                        log(f"secret scan failed: {e!r}")
                        leaks = []
                        out(f"  {YELLOW('⚠ secret scan could not run - check what you push')}")
                    if leaks:
                        out(f"  {RED('✗ push blocked: secrets in what would be uploaded')}")
                        for l in leaks[:6]:
                            out(f"     {RED(l)}")
                        return False, ("Push blocked by the secret scan - these would be uploaded:\n" + "\n".join(leaks) +
                                       "\nRemove them from the commits (and add the files to .gitignore), rotate any "
                                       "real key that was committed, then try again. Tell the user.")
                return self._ask_outward(kind, detail, why)
        if kind == "write" and not self._inside(detail):
            return self._ask_outward(kind, detail, "writes outside the project folder")
        if self.yolo or kind in self.allowed:
            return True, None
        if kind == "run" and SAFE_CMD.match(detail) and not re.search(r"[>]|\brm\b|\bdel\b|&&|;|\|\|", detail):
            return True, None
        prefix = "cmd:" + detail.strip().split()[0] if kind == "run" and detail.strip() else None
        if prefix and prefix in self.allowed:
            return True, None
        what = {"run": "run this command", "code": "run this code"}.get(kind, "make this change")
        always = (f"always allow `{prefix[4:]}` commands" if prefix else
                  "always allow sandboxed code this session" if kind == "code" else "always allow edits this session")
        if RPC and getattr(self, "once", False):
            return False, "Not run: needs approval but ink runs headless (-p). Use auto mode or ask the user."
        if RPC:
            ans = self.ask_ui(kind, what[0].upper() + what[1:] + "?", detail, always)
            if ans["choice"] == "always":
                self.allowed.add(prefix if prefix else kind)
            if ans["choice"] in ("yes", "always"):
                return True, None
            return False, (ans.get("text") or None)
        out(f"  {YELLOW('?')} {what}?  {BOLD('y')} yes · {BOLD('a')} {always} · {BOLD('n')} no (or type what to do instead)")
        while IS_WIN and msvcrt.kbhit():
            msvcrt.getwch()
        try:
            ans = input(f"  {YELLOW('›')} ").strip()
        except (EOFError, KeyboardInterrupt):
            return False, None
        if ans.lower() in ("y", "yes", ""):
            return True, None
        if ans.lower() in ("a", "always"):
            self.allowed.add(prefix if prefix else kind)
            return True, None
        return False, (None if ans.lower() in ("n", "no") else ans)

    def _inside(self, path):
        try:
            p = Path(path).resolve()
        except Exception:
            return False
        roots = [self.cwd.resolve(), STATE.resolve(), Path(tempfile_dir()).resolve()]
        roots += [Path(r) for r in getattr(self, "extra_roots", ())]
        if any(p == r or r in p.parents for r in roots):
            return True
        # Anywhere in the user's own folder counts as a workspace (new projects live next to each other there),
        # except credentials, app data and config. Set "write_anywhere_in_home": false to ask again.
        home = Path.home().resolve()
        if WRITE_IN_HOME and home in p.parents:
            top = p.relative_to(home).parts[0].lower()
            return not (top.startswith(".") or top in HOME_PRIVATE)
        return False

    def _ask_outward(self, kind, detail, why):
        """Outward actions ask even in auto mode. 'always' only covers this exact command for the session."""
        key = f"outward:{detail.strip()}"
        if key in self.allowed:
            return True, None
        if getattr(self, "once", False) or (not RPC and not sys.stdin.isatty()):
            return False, (f"Not run: this {why} and needs the user's approval, but ink is running non-interactively. "
                           "Tell the user the exact command so they can run it themselves.")
        elif RPC:
            ans = self.ask_ui("outward", f"This {why}. Allow it?", detail, "allow this exact action again this session")
            choice, text = ans["choice"], ans.get("text")
        else:
            out(f"  {YELLOW('⚠')} {BOLD(f'this {why}')} - always asks, even in auto mode")
            out(f"  {YELLOW('?')} allow?  {BOLD('y')} yes · {BOLD('a')} yes, this exact one again this session · "
                f"{BOLD('n')} no (or type what to do instead)")
            try:
                a = input(f"  {YELLOW('›')} ").strip()
            except (EOFError, KeyboardInterrupt):
                a = "n"
            choice = {"y": "yes", "yes": "yes", "a": "always", "always": "always", "": "no"}.get(a.lower(), "no")
            text = None if a.lower() in ("n", "no", "") else (a if choice == "no" else None)
        if choice == "always":
            self.allowed.add(key)
            if kind == "write":
                try:
                    self.extra_roots = getattr(self, "extra_roots", set()) | {Path(detail).resolve().parent}
                except Exception:
                    pass
        if choice in ("yes", "always"):
            return True, None
        return False, text

    def ask_ui(self, kind, title, detail, always=""):
        """--rpc: send an `ask` and block until the UI answers (or the turn is interrupted -> no)."""
        aid = f"a{int(time.time() * 1000)}"
        self.answers = getattr(self, "answers", None) or queue.Queue()
        emit("ask", id=aid, kind=kind, title=title, detail=str(detail)[:4000], options=["yes", "always", "no"],
             always=always)
        while True:
            try:
                ans = self.answers.get(timeout=0.2)
            except queue.Empty:
                if self.stop.is_set():
                    return {"choice": "no"}
                continue
            if ans.get("id") in (aid, None):
                return ans

    def denied(self, reason):
        out(f"  {GRAY('⎿')}  {YELLOW('rejected')}")
        if reason:
            return f"The user rejected this action and said: {reason}", False
        return "The user rejected this action. Stop and wait for the user's instructions.", True

    # -- undo: every write is backed up so a bad edit (or a wrong auto-approved change) can be reverted
    def _backup(self, p):
        p = Path(p)
        existed = p.exists() and not p.is_dir()
        if not existed:
            return False, None
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            dest = BACKUP_DIR / f"{self.session_file.stem}-{len(self.undo_stack):04d}-{p.name}"
            shutil.copy2(p, dest)
            return True, str(dest)
        except Exception:
            return True, None

    def _record_undo(self, p, existed, backup):
        self.undo_stack.append({"path": str(Path(p)), "backup": backup, "existed": bool(existed)})
        self._save_undo()

    def _save_undo(self):
        try:
            UNDO_DIR.mkdir(parents=True, exist_ok=True)
            (UNDO_DIR / f"{self.session_file.stem}.json").write_text(json.dumps(self.undo_stack), "utf-8")
        except Exception:
            pass

    def _load_undo(self):
        try:
            f = UNDO_DIR / f"{self.session_file.stem}.json"
            self.undo_stack = json.loads(f.read_text("utf-8")) if f.exists() else []
        except Exception:
            self.undo_stack = []

    def undo(self, n=1):
        if not self.undo_stack:
            out(GRAY("nothing to undo"))
            return
        n = max(1, min(int(n or 1), len(self.undo_stack)))
        for _ in range(n):
            e = self.undo_stack.pop()
            p = Path(e["path"])
            if e.get("existed"):
                b = e.get("backup")
                if b and Path(b).exists():
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(Path(b).read_bytes())
                    out(GRAY(f"restored {self.tools.rel(p)}"))
                else:
                    out(YELLOW(f"no backup for {self.tools.rel(p)}"))
            else:
                if p.exists():
                    p.unlink()
                out(GRAY(f"removed {self.tools.rel(p)}"))
            self.tools.read_paths.discard(str(p.resolve()))
        self._save_undo()

    # -- context management
    def compact(self, focus=""):
        if len(self.messages) < 3:
            return
        if self.ctx_used > 0.9 * self.P["ctx"]:  # the summary request itself would not fit
            self.cold_compact(keep_tokens=int(0.35 * self.P["ctx"]))
            return
        out(GRAY("compacting conversation…"))
        # The model only summarizes the narrative. What must not drift - the user's own words, the goal and key
        # points, the plan and the files changed - is carried over verbatim by the harness below.
        ask = ("Summarize this conversation so you can continue the work with no other context. Use these headings:\n"
               "Decisions: what was decided and why.\nDone: what is finished and verified.\n"
               "Failed: what was tried and did not work, and why (so it is not retried).\n"
               "Facts: exact names, paths, commands, error messages and values you will need again.\n"
               "Next: the exact next steps.\nBe specific and concise; don't repeat the user's requests."
               + (f" Focus: {focus}" if focus else "") + " Reply with the summary as text; do not call any tools.")
        msgs = self.messages + [{"role": "user", "content": ask}]
        try:
            # Same tools and settings as a normal step: the template puts the tool list at the top of the prompt,
            # so tools=[] made the whole conversation be re-read from scratch (~20 min at 210k, INK-track O10).
            # Room for thinking before the summary.
            msg, st = self.complete(msgs, live=False, max_tokens=COMPACT_TOKENS + 6000)
        except Interrupted:
            raise  # Esc means stop, not "compaction failed, carry on" (INK-track O16)
        except Exception as e:
            out(RED(f"compact failed: {e}"))
            return
        summary = (msg.get("content") or "").strip() or (msg.get("reasoning_content") or "").strip()[-6000:]
        if not summary:  # no usable summary: keep the brief + the recent messages without the model
            self.cold_compact(keep_tokens=int(_cfg("cold_keep_tokens", 12000)))
            return
        kept = []
        if self.goal or self.key_points:
            kept.append(self.brief_text(plan=False).split("User's requests")[0].strip())
        reqs = [r for r in self.requests if not r.startswith("[")]
        if reqs:  # the user's own words, verbatim (older ones shortened if there are many)
            kept.append("The user's requests, verbatim (oldest first):\n" + "\n".join(
                f"- {r if i >= len(reqs) - 3 else short(r, 400)}" for i, r in enumerate(reqs[-12:])))
        if self.changed:
            kept.append("Files changed this session: " + ", ".join(self.changed[-40:]))
        if self.plan:
            kept.append("Current plan ([x] done, [>] in progress, [ ] todo, [-] dropped):\n" + self.plan_text())
        kept.append("Older details can be searched with recall(query).")
        text = summary.strip() + "\n\n" + "\n\n".join(kept)
        if self.hooks:
            self.hooks.simple("PreCompact", {"messages": len(self.messages)})
        # layered: summary of the old part + the most recent messages verbatim (minus bulky stale tool output)
        tail = self.recent_tail(int(_cfg("compact_keep_tokens", 4000)))
        self.messages = [self.messages[0], {"role": "user", "content":
                         "[Conversation summary - earlier messages were compacted]\n" + text +
                         ("\nThe most recent messages follow verbatim." if tail else "")},
                         {"role": "assistant", "content": "Understood. I'll continue from this summary."}] + tail
        self.ctx_used = sum(len(json.dumps(m)) for m in self.messages) // 4 + 1500
        self.tools.forget()  # file contents are gone from context: force a fresh read before edits
        out(GRAY(f"compacted to ~{ktok(self.ctx_used)} tokens"))

    def recent_tail(self, keep_tokens):
        """The last messages that fit keep_tokens, starting at a user message (never inside a tool-call pair);
        big tool outputs in them are shortened (the model can re-run or re-read)."""
        tail, size = [], 0
        for m in reversed(self.messages[1:]):
            m = dict(m)
            if m["role"] == "tool" and len(m.get("content") or "") > 1500:
                m["content"] = m["content"][:600] + "\n… [output shortened at compaction; re-run or re-read if needed]"
            m.pop("reasoning_content", None) if tail else None
            n = len(json.dumps(m)) // 4
            if size + n > keep_tokens and tail:
                break
            tail.insert(0, m)
            size += n
        while tail and tail[0]["role"] != "user":
            tail.pop(0)
        if tail and str(tail[0].get("content", "")).startswith("[Conversation summary"):
            tail.pop(0)
            if tail and tail[0]["role"] == "assistant":
                tail.pop(0)
        return tail

    def save(self):
        try:
            SESS_DIR.mkdir(parents=True, exist_ok=True)
            data = {"cwd": str(self.cwd), "profile": self.profile,
                    "effort": self.effort, "plan": self.plan,
                    "goal": self.goal, "key_points": self.key_points,
                    "requests": self.requests, "changed": self.changed,
                    "mode": "plan" if self.plan_mode else "build",
                    "split": self.split,
                    "read": sorted(self.tools.read_paths),
                    "ctx_used": self.ctx_used, "kv_tokens": self.kv_tokens, "tools_sig": tools_sig(),
                    "messages": self.messages}
            if self.dreamed_at is not None:
                data["dreamed_at_msgs"] = self.dreamed_at
            self.session_file.write_text(json.dumps(data), "utf-8")
            index_session(RECALL_DB, self.session_file, data)
        except Exception:
            pass

    def dream_later(self):
        """Sleep/dream: after the session, reflect on it in the background and save lessons to memory."""
        if not DREAM or getattr(self, "once", False) or not self.session_file.exists():
            return
        if sum(1 for r in self.requests if not r.startswith("[")) < 2:
            return
        spawn_detached([sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--dream", str(self.session_file)],
                       "dream.log")
        out(GRAY("  dreaming in the background: lessons from this session go to memory"))

    def dream_now(self):
        from dream import dream
        self.save()
        out(GRAY("dreaming: rereading this session for lessons…"))
        try:
            written = dream(self.session_file, self.P["port"], self.kwargs(), MEMORY_DIR, RECALL_DB, log)
        except Exception as e:
            out(RED(f"dream failed: {e}"))
            return
        self.dreamed_at = len(self.messages)
        out(GRAY("saved to memory: " + ", ".join(written)) if written else GRAY("nothing new worth remembering"))

    def list_sessions(self, n=15):
        rows = []
        for f in sorted(SESS_DIR.glob("*.json"), reverse=True)[:200] if SESS_DIR.exists() else []:
            try:
                with open(f, encoding="utf-8") as fh:
                    head = fh.read(4096)
                if f'"cwd": {json.dumps(str(self.cwd))}' not in head:
                    continue
                d = json.loads(f.read_text("utf-8"))
            except Exception:
                continue
            reqs = [r for r in d.get("requests") or [] if not r.startswith("[")]
            rows.append({"id": f.stem, "when": f"{f.stem[:4]}-{f.stem[4:6]}-{f.stem[6:8]} {f.stem[9:11]}:{f.stem[11:13]}",
                         "cwd": d.get("cwd"), "title": short(reqs[0] if reqs else "(empty)", 70), "prompts": len(reqs),
                         "file": str(f)})
            if len(rows) >= n:
                break
        return rows

    def fork(self):
        """Branch the conversation: continue in a new session; the old one stays resumable (/resume)."""
        self.save()
        old = self.session_file
        self.session_file = SESS_DIR / f"{datetime.datetime.now():%Y%m%d-%H%M%S}.json"
        if self.session_file == old:
            time.sleep(1.1)
            self.session_file = SESS_DIR / f"{datetime.datetime.now():%Y%m%d-%H%M%S}.json"
        self.kv_tokens = {}
        self.save()
        return old.stem, self.session_file.stem

    def load_last(self, only=None):
        files = sorted(SESS_DIR.glob("*.json"), reverse=True) if SESS_DIR.exists() else []
        if only:
            files = [f for f in files if f.stem == only]
        for f in files:
            try:
                d = json.loads(f.read_text("utf-8"))
            except Exception:
                continue
            if session_matches(d, self.cwd):
                self.messages = d["messages"]
                self.profile = d.get("profile", self.profile)
                self.effort = d.get("effort", self.effort)
                self.plan = d.get("plan", [])
                self.goal, self.key_points = d.get("goal", ""), d.get("key_points", [])
                self.requests = d.get("requests") or [m["content"] for m in self.messages
                                                      if m["role"] == "user" and isinstance(m.get("content"), str)
                                                      and not m["content"].startswith("[")]
                self.changed = d.get("changed", [])
                self.dreamed_at = d.get("dreamed_at_msgs")
                self.plan_mode = d.get("mode", "build") == "plan"
                self.split = d.get("split", self.split)
                self.tools.read_paths.update(d.get("read", []))
                self.session_file = f
                self.ctx_used = d.get("ctx_used") or sum(len(json.dumps(m)) for m in self.messages) // 4
                self.kv_tokens = d.get("kv_tokens") or {}
                self.loaded_tools_sig = d.get("tools_sig")
                self._load_undo()
                return len([m for m in self.messages if m["role"] == "user"])
        return 0

# ---------------------------------------------------------------- REPL
HELP = """commands:
  /fast /smart      switch model (Ornith fast / Inkling smart); reloads the server (~20-60s)
  /effort <level>   reasoning effort: none minimal low medium high max (re-reads history once)
  /think            show/hide the model's thinking
  /auto             toggle auto-approve for edits and commands
  /plan [on|off]    toggle plan mode (read-only research + a plan, no changes); /plan show to view it
  /build            switch to build mode and execute the current plan
  /mode [plan|build] show or set the mode
  /split [on|off]   phase-split: plan on smart (Inkling), build on fast (Ornith)
  /context          show context usage
  /compact [focus]  summarize the conversation to free context (goal, key points, your requests and plan are kept verbatim)
  /brief            show the pinned brief: goal, key points, your requests, plan
  /recall <words>   search past sessions and memory notes
  /sleep            reflect on this session now and save lessons to memory (also runs by itself after /exit)
  /undo [n]         revert the last n file edits (backups in ~/.ink/backups)
  /rewind [n|id]    restore ALL project files to before the last (or nth-last) turn - also undoes shell/code changes
  /checkpoints      list the automatic per-turn snapshots
  /check [word]     run the project checks (tests/compile/lint; set them in .ink/checks.json)
  /map [path] [q]   show the repo map (or a file's outline); q filters by words
  /checks on|off    automatic checks before ink finishes a turn with edits
  /clear            start a new conversation
  /resume [n]       list this folder's sessions, or continue session n
  /fork             branch: continue in a new session, the current one stays resumable
  /agents           list subagents (built-in + ~/.ink/agents + .ink/agents)
  /cwd <dir>        change working directory (starts a new conversation)
  /stop             unload the model server and exit
  /exit             quit (server keeps running for a fast restart)
input: end a line with \\ for multi-line; paste works. esc or ctrl+c interrupts the model."""

def read_input():
    lines = []
    prompt = f"\n{ACCENT('❯')} "
    while True:
        line = input(prompt if not lines else f"{GRAY('…')} ")
        if line.endswith("\\"):
            lines.append(line[:-1])
            continue
        lines.append(line)
        # collect the rest of a multi-line paste
        if IS_WIN:
            time.sleep(0.02)
            if msvcrt.kbhit():
                continue
        break
    return "\n".join(lines).strip()

def banner(ag):
    p = ag.P
    eff = f" · effort {ag.effort}" if ag.profile == "smart" else ""
    out(f"{ACCENT('✻')} {BOLD('ink')} {GRAY('·')} {p['title']} {GRAY(f'({ag.profile}{eff}) · {ktok(p["ctx"])} ctx · {ag.cwd}')}")
    out(GRAY("  /help for commands · esc to interrupt"
             + (" · plan mode" if ag.plan_mode else "")
             + (" · phase-split (smart→plan / fast→build)" if ag.split else "")
             + (" · auto-approve ON" if ag.yolo and not ag.plan_mode else "")))

def switch(ag, key):
    if key == ag.profile:
        out(GRAY(f"already on {ag.P['title']}"))
        return True
    stop_dream("model switch")
    if ag.ctx_used > 0.7 * PROFILES[key]["ctx"]:
        out(GRAY(f"the conversation ({ktok(ag.ctx_used)}) is too big for {PROFILES[key]['title']}'s "
                 f"{ktok(PROFILES[key]['ctx'])} window - compacting first"))
        ag.compact()
    ag.reread_guard(key, f"switching to {PROFILES[key]['title']}")
    if len(ag.messages) > 1:
        if RPC:
            emit("status", text=f"switching to {PROFILES[key]['title']}: saving the conversation cache…")
        ag.persist_kv(wait=True)  # so switching back is instant
    if RPC:
        emit("status", text=f"switching to {PROFILES[key]['title']}: loading the model (~20-60s)…")
    old, old_effort = ag.profile, ag.effort
    ag.profile = key
    ag.effort = PROFILES[key]["effort"] or "low"
    if not ensure_server(key):
        ag.profile, ag.effort = old, old_effort
        out(RED(f"could not switch to {PROFILES[key]['title']}; staying on {PROFILES[old]['title']}"))
        return False
    if len(ag.messages) > 1 and ag.restore_kv():
        out(GRAY(f"switched to {ag.P['title']} (conversation cache restored from disk)"))
    else:
        threading.Thread(target=ag.warm, daemon=True).start()
        out(GRAY(f"switched to {ag.P['title']} (conversation kept; it will be re-read once)"))
    return True

def handle_command(ag, cmd, arg=""):
    """A slash command (without the slash). Returns "exit" to quit. Shared by the REPL, --rpc and Telegram."""
    global AUTO_CHECKS
    cmd, arg = cmd.lower(), (arg or "").strip()
    if cmd in ("exit", "quit", "q"):
        return "exit"
    elif cmd == "help":
        out(GRAY(HELP))
    elif cmd in ("clear", "new"):
        ag.dream_later()
        ag.reset()
        threading.Thread(target=ag.warm, daemon=True).start()
        out(GRAY("new conversation"))
    elif cmd == "compact":
        ag.compact(arg)
    elif cmd == "undo":
        ag.undo(int(arg) if arg.isdigit() else 1)
    elif cmd == "rewind":
        ag.rewind(arg)
    elif cmd in ("checkpoints", "cp"):
        rows = ag.cp.list(15)
        if not rows:
            out(GRAY(ag.cp.error or "no checkpoints yet"))
        for i, (h, when, msg) in enumerate(rows):
            out(f"  {ACCENT(h)}  {GRAY(when):<18} {short(msg, 80)}")
    elif cmd == "check":
        ag.project_check(only=arg or None)
    elif cmd == "map":
        path, _, q = arg.partition(" ")
        if path and not ag.tools.path(path).exists():
            path, q = ".", arg
        res, summ = repo_map(ag.tools.cwd, path or ".", q.strip() or None)
        out(res)
        out(GRAY(f"  {summ}"))
    elif cmd == "checks":
        AUTO_CHECKS = (arg == "on") if arg in ("on", "off") else not AUTO_CHECKS
        out(GRAY(f"automatic checks {'ON' if AUTO_CHECKS else 'off'}"))
    elif cmd in ("fast", "smart"):
        switch(ag, cmd)
    elif cmd == "model":
        switch(ag, arg) if arg in PROFILES else out(GRAY("usage: /model fast|smart"))
    elif cmd == "effort":
        if arg in EFFORTS:
            if arg != ag.effort:
                ag.reread_guard(ag.profile, "changing effort", cached=False)
            ag.effort = arg
            out(GRAY(f"effort {arg}"))
        else:
            out(GRAY(f"effort is {ag.effort}; options: {' '.join(EFFORTS)}"))
    elif cmd == "think":
        ag.show_thinking = not ag.show_thinking
        out(GRAY(f"thinking {'shown' if ag.show_thinking else 'hidden'}"))
    elif cmd in ("auto", "yolo"):
        ag.yolo = not ag.yolo
        out(GRAY(f"auto-approve {'ON' if ag.yolo else 'off'}"))
    elif cmd == "cwd":
        d = Path(arg).expanduser()
        if d.is_dir():
            ag.dream_later()
            os.chdir(d)
            ag.cwd = Path.cwd()
            ag.tools = Tools(ag.cwd)
            ag.cp = Checkpoints(ag.cwd)
            ag.hooks = Hooks(ag.cwd, log)
            ag.reset()
            threading.Thread(target=ag.warm, daemon=True).start()
            out(GRAY(f"cwd {ag.cwd} (new conversation)"))
        else:
            out(RED("not a directory"))
    elif cmd == "stop":
        for k, p in PROFILES.items():
            for pid in find_servers(p["model"]):
                kill(pid)
        out(GRAY("model server stopped"))
        ag.stopped = True
        return "exit"
    elif cmd == "sleep":
        ag.dream_now()
    elif cmd == "recall":
        out(recall_search(RECALL_DB, MEMORY_DIR, arg) if arg else GRAY("usage: /recall <keywords>"))
    elif cmd == "brief":
        out(ag.brief_text() or GRAY("no goal, key points or plan yet"))
    elif cmd == "plan":
        if arg == "show":
            out(ag.plan_text() if ag.plan else GRAY("no plan yet"))
        elif arg in ("on", "off"):
            ag.set_mode("plan" if arg == "on" else "build")
        elif arg == "":
            ag.set_mode("build" if ag.plan_mode else "plan")
        else:
            out(GRAY("usage: /plan [on|off|show]"))
    elif cmd == "build":
        ag.set_mode("build")
        if ag.plan and any(s["status"] in ("todo", "doing") for s in ag.plan):
            ag.turn("Plan approved. Execute the plan now.")
    elif cmd == "mode":
        if arg in ("plan", "build"):
            ag.set_mode(arg)
        elif arg == "":
            out(GRAY(f"mode: {'plan' if ag.plan_mode else 'build'} (usage: /mode plan|build)"))
        else:
            out(GRAY("usage: /mode plan|build"))
    elif cmd == "split":
        if arg in ("on", "off"):
            ag.split = arg == "on"
        elif arg == "":
            ag.split = not ag.split
        else:
            out(GRAY("usage: /split [on|off]"))
            return None
        out(GRAY(f"phase-split {'ON' if ag.split else 'OFF'}: plan on smart, build on fast"))
        if ag.split and ag.profile != ("smart" if ag.plan_mode else "fast"):
            switch(ag, "smart" if ag.plan_mode else "fast")
    elif cmd == "resume":
        rows = ag.list_sessions()
        if not arg:
            emit("sessions", items=rows)
            if not rows:
                out(GRAY("no saved sessions in this folder"))
            for i, r in enumerate(rows, 1):
                out(f"  {ACCENT(str(i)):>3}  {GRAY(r['when'])}  {r['title']}  {GRAY(str(r['prompts']) + ' prompts')}")
            if rows:
                out(GRAY("  /resume <number> to continue one"))
        else:
            pick = rows[int(arg) - 1]["id"] if arg.isdigit() and 0 < int(arg) <= len(rows) else arg
            ag.save()
            ag.reset()
            n = ag.load_last(only=pick)
            if not n:
                out(RED(f"no session {arg}"))
            else:
                ok = ag.loaded_tools_sig == tools_sig() and ag.restore_kv()
                if not ok and ag.ctx_used / PREFILL_TPS.get(ag.profile, 73) > REREAD_MAX_S:
                    ag.messages[0] = {"role": "system", "content": system_prompt(ag.cwd, ag.plan_mode)}
                    ag.cold_compact()
                out(GRAY(f"resumed {pick} ({n} prompts{', cache restored' if ok else ''})"))
    elif cmd == "fork":
        old, new = ag.fork()
        out(GRAY(f"forked: now in session {new} · the original ({old}) stays resumable with /resume"))
    elif cmd == "agents":
        from agents import load_agents, Router
        r = Router(log)
        for k, v in load_agents(ag.cwd).items():
            out(f"  {ACCENT(k):<20} {GRAY(v.get('where', 'any')):<8} {short(v['description'], 90)}")
        out(GRAY(f"  remote providers: {len(r.available())} ready of {len(r.providers)} ({r.mode}) - ~/.ink/providers.json"))
    elif cmd == "context":
        # context inspector: where the window goes (estimates: ~4 chars per token)
        tok = lambda x: len(json.dumps(x)) // 4
        parts = {"system prompt": tok(ag.messages[0]), "tool definitions": tok(TOOLS)}
        tools_by = {}
        for m in ag.messages[1:]:
            key = {"user": "your messages + harness notes", "assistant": "ink's replies + thinking"}.get(m["role"], "tool results")
            parts[key] = parts.get(key, 0) + tok(m)
        calls = {}
        for m in ag.messages:
            for tc in m.get("tool_calls") or []:
                calls[tc.get("id")] = tc["function"]["name"] + " " + short(tc["function"].get("arguments", ""), 50)
        for m in ag.messages:
            if m["role"] == "tool":
                tools_by[calls.get(m.get("tool_call_id"), "?")] = tok(m)
        total = sum(parts.values())
        ctx = ag.P["ctx"]
        out(f"  {BOLD('context')} ~{ktok(total)} of {ktok(ctx)} ({100 * total / ctx:.0f}%) · {len(ag.messages)} messages")
        for k, v in sorted(parts.items(), key=lambda x: -x[1]):
            bar = "▓" * max(1, round(30 * v / max(total, 1)))
            out(f"  {k:<30} {ktok(v):>7}  {ACCENT(bar)}")
        if tools_by:
            out(GRAY("  biggest tool results:"))
            for k, v in sorted(tools_by.items(), key=lambda x: -x[1])[:8]:
                out(GRAY(f"    {ktok(v):>7}  {k}"))
        out(GRAY(f"  cache: {ktok(ag.kv_tokens.get(ag.profile, 0))} tokens saved on disk · /compact frees space"))
    else:
        out(GRAY("unknown command - /help"))
    return None

def repl(ag):
    while True:
        try:
            text = read_input()
        except (EOFError, KeyboardInterrupt):
            out("")
            return
        if not text:
            continue
        if text.startswith("/"):
            cmd, _, arg = text[1:].partition(" ")
            if handle_command(ag, cmd, arg) == "exit":
                return
            continue
        ag.user_input(text)

def command_list():
    items = []
    for l in HELP.splitlines():
        m = re.match(r"\s+/(\w+)\s*(\S*(?:\s\S+)?)?\s{2,}(.*)", l)
        if m:
            items.append({"name": m.group(1), "args": (m.group(2) or "").strip(), "desc": m.group(3).strip()})
    return items

_FILES_CACHE = {}

def complete_files(cwd, prefix, limit=20):
    key = str(cwd)
    hit = _FILES_CACHE.get(key)
    if not hit or time.time() - hit[0] > 30:
        files = []
        try:
            if RG:
                r = subprocess.run([RG, "--files", "--hidden", "-g", "!.git"], cwd=cwd, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=10)
                files = r.stdout.splitlines()
        except Exception:
            pass
        if not files:
            for dp, dns, fns in os.walk(cwd):
                dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
                files += [os.path.relpath(os.path.join(dp, f), cwd) for f in fns]
                if len(files) > 20000:
                    break
        files = [f.replace("\\", "/") for f in files]
        hit = _FILES_CACHE[key] = (time.time(), files)
    pre = prefix.lower().lstrip("@")

    def score(f):
        fl = f.lower()
        if fl.startswith(pre):
            return 0
        if "/" + pre in fl or Path(fl).name.startswith(pre):
            return 1
        it = iter(fl)
        return 2 if all(c in it for c in pre) else None
    ranked = sorted((s, len(f), f) for f in hit[1] if (s := score(f)) is not None)
    return [{"value": f, "label": f, "desc": ""} for _, _, f in ranked[:limit]]

def idle_dream(ag, quiet=True):
    """Hermes-style memory review during long sessions, when the user is idle. Never clobbers the main KV cache."""
    if not DREAM or ag.turns_since_dream < DREAM_EVERY or not ag.model_lock.acquire(blocking=False):
        return
    try:
        from agents import LocalSlot
        from dream import dream
        ag.save()
        with LocalSlot(ag.P["port"], ag.kv_name() + ".main", log):
            written = dream(ag.session_file, ag.P["port"], ag.kwargs(), MEMORY_DIR, RECALL_DB, log)
        ag.turns_since_dream = 0
        ag.dreamed_at = len(ag.messages)
        if written and not quiet:
            out(GRAY("memory review saved: " + ", ".join(written)))
    except Exception as e:
        log(f"idle dream failed: {e!r}")
    finally:
        ag.model_lock.release()

def rpc_main(ag, first_prompt=None):
    """--rpc: the engine for a UI (tui/), speaking PROTOCOL.md over stdin/stdout."""
    q = queue.Queue()
    ag.busy = False

    def reader():
        for line in RPC.inp:
            try:
                m = json.loads(line)
            except Exception:
                continue
            t = m.get("type")
            if t == "interrupt":
                ag.stop.set()
            elif t == "answer":
                ag.answers.put(m)
            elif t == "steer":
                if ag.busy:
                    ag.steer.append(str(m.get("text") or ""))
                    emit("notice", level="info", text="↳ guidance queued for the running turn")
                else:
                    q.put({"type": "prompt", "text": m.get("text") or ""})
            elif t == "complete":
                kind, prefix = m.get("kind"), str(m.get("prefix") or "")
                items = (complete_files(ag.tools.cwd, prefix) if kind == "file" else
                         [{"value": c["name"], "label": "/" + c["name"], "desc": c["desc"]} for c in command_list()
                          if c["name"].startswith(prefix.lstrip("/"))])
                emit("completions", req=m.get("req"), items=items)
            else:
                q.put(m)
        q.put({"type": "quit"})

    threading.Thread(target=reader, daemon=True).start()
    emit("hello", version=1, model=ag.P["title"], profile=ag.profile, effort=ag.effort, ctx=ag.P["ctx"],
         cwd=str(ag.cwd), mode=ag.mode_name(), session=ag.session_file.stem,
         resumed=sum(1 for m in ag.messages if m["role"] == "user" and not str(m.get("content", "")).startswith("[")),
         commands=command_list(),
         tips=["Just describe what you want - ink plans, edits, runs checks and reviews by itself",
               "@file attaches a file · !cmd runs a shell command · #note saves to memory · &task runs a subagent",
               "shift+tab: auto → safe → plan · esc interrupts · type while it works to queue or steer"])
    if first_prompt:
        q.put({"type": "prompt", "text": first_prompt})
    last = time.time()
    while True:
        try:
            m = q.get(timeout=5)
        except queue.Empty:
            if time.time() - last > 90:
                idle_dream(ag)
            continue
        last = time.time()
        t = m.get("type")
        if t == "quit":
            break
        try:
            ag.busy = True
            if t == "prompt":
                text = str(m.get("text") or "")
                if text.startswith("/"):
                    cmd, _, arg = text[1:].partition(" ")
                    t, m = "command", {"name": cmd, "arg": arg}
                else:
                    special = text[:1] in ("!", "#", "&")
                    ag.user_input(text)
                    if special:
                        emit("turn_end", secs=0, steps=0, interrupted=False)
            if t == "command":
                RPC.capture = []
                try:
                    res = handle_command(ag, str(m.get("name") or ""), str(m.get("arg") or ""))
                finally:
                    lines, RPC.capture = RPC.capture, None
                if lines:
                    emit("output", text="\n".join(lines).rstrip())
                emit("turn_end", secs=0, steps=0, interrupted=False)
                if res == "exit":
                    break
            elif t == "mode":
                want = m.get("mode")
                if want == "plan":
                    ag.set_mode("plan")
                else:
                    if ag.plan_mode:
                        ag.set_mode("build")
                    ag.yolo = want != "safe"
                emit("stats", **{"mode": ag.mode_name(), "ctx_used": ag.ctx_used, "ctx": ag.P["ctx"],
                                 "model": ag.P["title"], "effort": ag.effort})
        except Exception as e:
            log(f"rpc error: {e!r}")
            emit("notice", level="error", text=f"error: {e}")
            emit("turn_end", secs=0, steps=0, interrupted=True)
        finally:
            ag.busy = False
            ag.stop.clear()
    ag.save()
    emit("bye")

def tools_sig():
    return hashlib.sha1(json.dumps(TOOLS, sort_keys=True).encode()).hexdigest()[:12]

PROJECT_MARKERS = ("package.json", ".git", "pyproject.toml", "foundry.toml", "Cargo.toml", "go.mod", "tsconfig.json",
                   "hardhat.config.js", "hardhat.config.ts", "hardhat.config.cjs", "index.html")

def is_project(d):
    d = Path(d)
    try:
        if d.resolve() == Path.home().resolve():
            return False
    except Exception:
        return False
    return d.is_dir() and any((d / m).exists() for m in PROJECT_MARKERS)

def project_root(path):
    """The project a file belongs to: the outermost git repo, else the nearest folder with a project file, else
    (a brand-new project) the top folder under the user's home."""
    p = Path(path).resolve()
    home = Path.home().resolve()
    nearest = git = None
    for d in [p.parent, *p.parent.parents]:
        if d == home or d == d.parent:
            break
        if (d / ".git").exists():
            git = d
        if nearest is None and is_project(d):
            nearest = d
    if git or nearest:
        return git or nearest
    if home in p.parents and len(p.relative_to(home).parts) > 1:
        return home / p.relative_to(home).parts[0]
    return p.parent

def session_matches(d, cwd):
    """A saved session belongs to this folder if it ran here or changed files in here (it may have been started
    elsewhere and built the project in this folder)."""
    here = str(Path(cwd).resolve()).lower()
    if str(d.get("cwd", "")).lower() == here:
        return True
    if here == str(Path.home().resolve()).lower():
        return False
    base = Path(d.get("cwd") or ".")
    for c in (d.get("changed") or [])[-80:]:
        try:
            q = str((base / c).resolve()).lower() if not Path(c).is_absolute() else str(Path(c).resolve()).lower()
        except Exception:
            continue
        if q.startswith(here + os.sep):
            return True
    return False

def recent_session(cwd):
    """True if this dir has a session saved within auto_continue_hours."""
    hours = float(_cfg("auto_continue_hours", 12))
    if hours <= 0 or not SESS_DIR.exists():
        return False
    for f in sorted(SESS_DIR.glob("*.json"), reverse=True)[:40]:
        if time.time() - f.stat().st_mtime > hours * 3600:
            continue
        try:
            if session_matches(json.loads(f.read_text("utf-8")), cwd):
                return True
        except Exception:
            continue
    return False

def main():
    ap = argparse.ArgumentParser(prog="ink", description="local coding agent (Inkling 2 Mini / Ornith on llama.cpp)")
    ap.add_argument("prompt", nargs="*", help="start with this prompt")
    ap.add_argument("-p", "--print", dest="once", help="run one prompt non-interactively and exit")
    ap.add_argument("--fast", action="store_true", help="use Ornith (fast: ~25x faster prefill, 16/20 eval)")
    ap.add_argument("--smart", action="store_true", help="use Inkling (smart: 20/20 eval) - default unless config sets default_model")
    ap.add_argument("--effort", choices=EFFORTS, help="reasoning effort")
    ap.add_argument("--yolo", "--auto", action="store_true", default=True, help="auto-approve edits and commands (default)")
    ap.add_argument("--safe", dest="yolo", action="store_false", help="ask before edits and commands")
    ap.add_argument("--plan", action="store_true", help="start in plan mode (research and plan only, no changes)")
    ap.add_argument("--split", action="store_true", help="phase-split: start planning on smart (Inkling); /build reloads fast (Ornith) to execute")
    ap.add_argument("-c", "--continue", dest="cont", action="store_true", help="continue the last session in this dir")
    ap.add_argument("--new", action="store_true", help="start a new conversation (by default a session from the last "
                    "auto_continue_hours (12) in this dir is continued automatically)")
    ap.add_argument("--hide-thinking", action="store_true")
    ap.add_argument("--stop", action="store_true", help="unload the model servers and exit")
    ap.add_argument("--dream", metavar="SESSION", help=argparse.SUPPRESS)
    ap.add_argument("--rpc", action="store_true", help="engine mode for a UI: JSON lines on stdin/stdout (PROTOCOL.md)")
    ap.add_argument("--json", action="store_true", help="with -p: stream PROTOCOL.md events as JSON lines (headless)")  # background reflection after a session
    a = ap.parse_args()
    STATE.mkdir(parents=True, exist_ok=True)
    global RPC
    if a.rpc or (a.json and a.once):
        RPC = RpcIO()
    if a.dream:
        from dream import dream
        d = json.loads(Path(a.dream).read_text("utf-8"))
        key = d.get("profile", "smart") if d.get("profile") in PROFILES else "smart"
        if not health(PROFILES[key]["port"]):  # server was stopped: try again after the next session
            return
        kw = {"reasoning_effort": "low"} if key == "smart" else {"enable_thinking": False}
        DREAM_PID.write_text(str(os.getpid()))
        try:
            dream(a.dream, PROFILES[key]["port"], kw, MEMORY_DIR, RECALL_DB, log)
        except Exception as e:
            log(f"dream {a.dream} failed: {e!r}")
        finally:
            DREAM_PID.unlink(missing_ok=True)
        return
    if a.stop:
        for p in PROFILES.values():
            for pid in find_servers(p["model"]):
                kill(pid)
        out("stopped")
        return
    model = "fast" if a.fast else ("smart" if a.smart else str(_cfg("default_model", "smart")))
    if model not in PROFILES:
        model = "smart"
    split = a.split or bool(_cfg("split_models", False))
    if split and not a.cont:
        a.plan = True  # phase-split starts by planning on smart; /build reloads fast to execute
    if split:
        model = "smart" if a.plan else "fast"
    ag = Agent(model, a.effort, a.yolo, not a.hide_thinking, a.plan, split)
    ag.once = bool(a.once)
    n = 0
    if not a.cont and not a.new and not a.once and recent_session(ag.cwd):
        a.cont = True  # pick up where you left off: the saved KV cache makes this instant
    if a.cont:
        n = ag.load_last()  # restores profile/effort/plan/mode before we pick which server to start
        if n:
            if ag.split:
                ag.profile = "smart" if (ag.plan_mode or a.plan) else "fast"  # match the (restored or requested) phase
            if a.fast:
                ag.profile = "fast"
            elif a.smart:
                ag.profile = "smart"
            if a.effort:
                ag.effort = a.effort
    if not ensure_server(ag.profile):
        sys.exit(1)
    restored = False
    if n:
        same_tools = ag.loaded_tools_sig == tools_sig()  # older sessions have none: their tools did change
        restored = same_tools and ag.restore_kv()
        out(GRAY(f"continuing session ({n} prompts" + (", KV cache restored from disk)" if restored else ")")))
        if ag.ctx_used > 0.8 * ag.P["ctx"] or (not restored and ag.ctx_used / PREFILL_TPS.get(ag.profile, 73) > REREAD_MAX_S):
            # no usable cache (pruned, or ink's tools changed): don't re-read 100k+ tokens, rebuild a compact context
            ag.messages[0] = {"role": "system", "content": system_prompt(ag.cwd, ag.plan_mode)}
            ag.cold_compact()
        if a.plan and not ag.plan_mode:
            ag.set_mode("plan")
            restored = True  # set_mode already warmed the new system prompt
    elif a.cont:
        out(GRAY("no previous session here"))
    if a.once:
        ag.user_input(a.once)
        if RPC:
            emit("bye")
        return
    if a.rpc:
        start_compressor()
        if not restored:
            threading.Thread(target=ag.warm, daemon=True).start()
        rpc_main(ag, " ".join(a.prompt) if a.prompt else None)
        if not getattr(ag, "stopped", False):
            ag.dream_later()
        return
    start_compressor()
    if not restored:
        threading.Thread(target=ag.warm, daemon=True).start()
    banner(ag)
    if a.prompt:
        out(f"\n{ACCENT('❯')} " + " ".join(a.prompt))
        ag.user_input(" ".join(a.prompt))
    try:
        repl(ag)
    except KeyboardInterrupt:
        pass
    if not getattr(ag, "stopped", False):
        ag.dream_later()

if __name__ == "__main__":
    main()
