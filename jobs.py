"""Background jobs for ink: long-running programs (dev servers, watchers, game backends) that keep running
across tool calls, belong to the ink session and are stopped when ink exits.

    jobs = Jobs(cwd, Path.home() / ".ink" / "jobs")     # cwd may also be a callable returning the current dir
    text, summary = run_job_tool(jobs, {"action": "start", "command": "npm run dev", "ready": "port"})

Windows: every job runs in a Win32 job object (KILL_ON_JOB_CLOSE), so its whole process tree, including
children that detach from their parent, dies with ink even on a crash; stop() also uses taskkill /T /F and a
parent-chain scan for processes that broke away. Ports come from `netstat -ano`. Stdlib only.
"""
import atexit
import codecs
import collections
import ctypes
import os
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path

IS_WIN = os.name == "nt"
SHELL_BASH = r"C:\Program Files\Git\bin\bash.exe"
HAVE_BASH = Path(SHELL_BASH).exists() if IS_WIN else True
READ_CHARS = 4000      # cap on text returned by read/wait/start
BUF_LINES = 5000       # in-memory ring buffer per job (the log file keeps everything)
LINE_MAX = 4000        # longer lines are cut in memory (the log file keeps them whole)
SHOW_LINE = 400        # ...and cut to this when shown to the model
MAX_TIMEOUT = 600
MAX_RUNNING = 12
NOWIN = 0x08000000 if IS_WIN else 0   # CREATE_NO_WINDOW

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
URL_RE = re.compile(r"\b(?:https?|wss?)://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\]|[\w.-]+):(\d{2,5})\b[^\s'\"<>)\]]*")
PORT_RES = [re.compile(r"\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\]|::):(\d{2,5})\b"),
            re.compile(r"(?i)\b(?:port|listening(?:\s+on|\s+at)?|running\s+(?:on|at)|serving\s+(?:on|at)|started\s+on)"
                       r"\s*[:=]?\s*(\d{2,5})\b")]
INUSE_RE = re.compile(r"(?i)EADDRINUSE|address already in use|WinError 10048|only one usage of each socket address|"
                      r"port \d+ is (?:already )?in use|already in use.*port|failed to bind|could not bind")
ONE_SHOT = re.compile(r"""(?ix)(?:^|[\s;&|(])(?:
      (?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|build|lint|ci|install|i|tsc)\b
    | npx\s+(?:tsc|jest|mocha|eslint|prettier|playwright\s+test)\b
    | (?:pytest|py\.test|tsc|jest|mocha|eslint)(?:\.cmd|\.exe)?\b
    | (?:python3?|py)\s+-m\s+(?:pytest|unittest|pip|compileall|py_compile)\b
    | pip3?\s+install\b
    | (?:hardhat|forge)\s+(?:test|compile|build)\b
    | cargo\s+(?:test|build|check|clippy)\b
    | go\s+(?:test|build|vet)\b
    )""")
WATCH = re.compile(r"(?i)--watch\b|(?:^|\s)-w(?:\s|$)|\bwatch\b|--serve\b|\bserve\b")
LAUNCHERS = {"npx", "pnpm", "yarn", "bun", "bunx", "npm", "uv", "poetry", "pipenv", "exec", "env", "sudo", "time",
             "nohup", "deno", "run", "dlx", "x", "-m", "py", "python", "python3", "node", "cmd", "/c", "sh", "bash"}


class JobError(Exception):
    pass


# ---------------------------------------------------------------- Windows process plumbing (ctypes)
if IS_WIN:
    from ctypes import wintypes as W

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

    class _IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ro", "wo", "oo", "rt", "wt", "ot")]

    class _BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", W.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", W.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", W.DWORD), ("SchedulingClass", W.DWORD)]

    class _EXT(ctypes.Structure):
        _fields_ = [("Basic", _BASIC), ("Io", _IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class _PIDS(ctypes.Structure):
        _fields_ = [("assigned", W.DWORD), ("listed", W.DWORD), ("ids", ctypes.c_size_t * 2048)]

    class _PE32(ctypes.Structure):
        _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("th32ProcessID", W.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", W.DWORD), ("cntThreads", W.DWORD),
                    ("th32ParentProcessID", W.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", W.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    _k32.CreateJobObjectW.restype = W.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    _k32.SetInformationJobObject.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [W.HANDLE, W.HANDLE]
    _k32.QueryInformationJobObject.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD, ctypes.c_void_p]
    _k32.TerminateJobObject.argtypes = [W.HANDLE, W.UINT]
    _k32.CloseHandle.argtypes = [W.HANDLE]
    _k32.CreateToolhelp32Snapshot.restype = W.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
    _k32.Process32FirstW.argtypes = [W.HANDLE, ctypes.POINTER(_PE32)]
    _k32.Process32NextW.argtypes = [W.HANDLE, ctypes.POINTER(_PE32)]
    _k32.OpenProcess.restype = W.HANDLE
    _k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    _k32.GetProcessTimes.argtypes = [W.HANDLE] + [ctypes.POINTER(W.FILETIME)] * 4
    _ntdll.NtResumeProcess.argtypes = [W.HANDLE]

    def _new_job_object():
        h = _k32.CreateJobObjectW(None, None)
        if not h:
            return None
        info = _EXT()
        # KILL_ON_JOB_CLOSE and no BREAKAWAY_OK: MSYS bash asks for CREATE_BREAKAWAY_FROM_JOB when it spawns
        # Windows programs and falls back to staying in the job when that is refused; allowing it lets node etc. escape
        info.Basic.LimitFlags = 0x2000
        if not _k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
            _k32.CloseHandle(h)
            return None
        return h

    def _job_object_pids(h):
        buf = _PIDS()
        if not _k32.QueryInformationJobObject(h, 3, ctypes.byref(buf), ctypes.sizeof(buf), None):
            return []
        return [int(buf.ids[i]) for i in range(min(buf.listed, 2048))]

    def _procs():
        """{pid: (ppid, exe)} for every process."""
        out = {}
        snap = _k32.CreateToolhelp32Snapshot(2, 0)
        if not snap or snap == W.HANDLE(-1).value:
            return out
        try:
            e = _PE32()
            e.dwSize = ctypes.sizeof(_PE32)
            ok = _k32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                out[int(e.th32ProcessID)] = (int(e.th32ParentProcessID), e.szExeFile)
                ok = _k32.Process32NextW(snap, ctypes.byref(e))
        finally:
            _k32.CloseHandle(snap)
        return out

    def _ctime(pid):
        """Process creation time as unix seconds, or None."""
        h = _k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            t = [W.FILETIME() for _ in range(4)]
            if not _k32.GetProcessTimes(h, *[ctypes.byref(x) for x in t]):
                return None
            ft = (t[0].dwHighDateTime << 32) | t[0].dwLowDateTime
            return ft / 1e7 - 11644473600
        finally:
            _k32.CloseHandle(h)
else:
    def _procs():
        out = {}
        try:
            r = subprocess.run(["ps", "-eo", "pid=,ppid=,comm="], capture_output=True, text=True, timeout=10)
            for line in r.stdout.splitlines():
                p = line.split(None, 2)
                if len(p) >= 2:
                    out[int(p[0])] = (int(p[1]), p[2] if len(p) > 2 else "?")
        except Exception:
            pass
        return out

    def _ctime(pid):
        try:
            return os.stat(f"/proc/{pid}").st_mtime
        except OSError:
            return None


def _listening():
    """{port: {pids}} for TCP sockets in LISTEN state (IPv4 + IPv6)."""
    res = collections.defaultdict(set)
    try:
        if IS_WIN:
            out = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=20, creationflags=NOWIN)
            for line in out.stdout.decode("utf-8", "replace").splitlines():
                p = line.split()
                # foreign address "0.0.0.0:0" / "[::]:0" == listening (locale-independent, unlike the state word)
                if len(p) >= 5 and p[0] == "TCP" and p[2].endswith(":0") and p[-1].isdigit():
                    port = p[1].rsplit(":", 1)[-1]
                    if port.isdigit():
                        res[int(port)].add(int(p[-1]))
        else:
            out = subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True, timeout=20)
            for line in out.stdout.splitlines():
                p = line.split()
                if len(p) >= 4:
                    port = p[3].rsplit(":", 1)[-1]
                    if port.isdigit():
                        res[int(port)].update(int(x) for x in re.findall(r"pid=(\d+)", line))
    except Exception:
        pass
    return dict(res)


def _kill_pid(pid, tree=True):
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid)] + (["/T"] if tree else []) + ["/F"],
                       capture_output=True, timeout=30, creationflags=NOWIN)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


# ---------------------------------------------------------------- helpers
def _cut(line, n):
    return line if len(line) <= n else line[:n] + f" …[{len(line) - n} chars cut]"


def _clean(line, limit=None):
    line = line.rstrip()
    if "\r" in line:   # carriage-return redraws (progress bars): keep what's visible last
        segs = [s for s in line.split("\r") if s.strip()]
        line = segs[-1] if segs else ""
    line = ANSI.sub("", line)
    return _cut(line, limit or LINE_MAX)


def _dur(s):
    s = int(max(s, 0))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def _compile(pattern):
    try:
        return re.compile(pattern, re.I)
    except re.error:
        return re.compile(re.escape(pattern), re.I)


def _dedupe(lines):
    """Collapse runs of lines that are equal up to numbers (spam, progress) into the last one + (xN)."""
    out, prev, last, n = [], None, None, 0
    for line in lines:
        key = re.sub(r"\d+", "#", line.strip())
        if key == prev:
            n += 1
            last = line
            continue
        if prev is not None:
            out.append(last + (f"  (x{n})" if n > 1 else ""))
        prev, last, n = key, line, 1
    if prev is not None:
        out.append(last + (f"  (x{n})" if n > 1 else ""))
    # drop runs of blank lines
    return [l for i, l in enumerate(out) if l.strip() or (i and out[i - 1].strip())]


def _fmt(lines, max_lines=60, max_chars=READ_CHARS, log=None):
    lines = _dedupe([_cut(l, SHOW_LINE) for l in lines])
    if len(lines) > max_lines:
        head = max(max_lines // 4, 1)
        tail = max_lines - head
        skipped = len(lines) - head - tail
        lines = lines[:head] + [f"[... {skipped} lines skipped{' - full log: ' + str(log) if log else ''} ...]"] + lines[-tail:]
    text = "\n".join(lines).strip("\n")
    if len(text) > max_chars:
        h = max_chars // 4
        t = max_chars - h - 60
        text = text[:h] + f"\n[... {len(text) - h - t} chars skipped ...]\n" + text[-t:]
    return text


def _auto_name(command):
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        all_toks = list(lex)
    except ValueError:
        all_toks = command.split()
    segs, cur, piped = [], [], False
    for t in all_toks:   # split on shell operators outside quotes; name after the last command (first of a pipe)
        if t in ("&&", "||", ";", "&"):
            segs.append(cur)
            cur, piped = [], False
        elif t == "|" or (t and set(t) <= set("|()<>")):
            piped = piped or t == "|"
        elif not piped:
            cur.append(t)
    segs.append(cur)
    toks = next((s for s in reversed(segs) if s and s[0] != "cd"), all_toks)
    toks = [t for t in toks if not re.match(r"^\w+=", t)]
    words, first = [], "job"
    skip_next = False
    for t in toks:
        if skip_next:
            skip_next = False
            continue
        if t in ("-e", "-c", "--eval", "-p", "--print"):   # inline code is not a name
            skip_next = True
            continue
        if re.search(r"[\s(){};'\"]", t) or re.fullmatch(r"[\d.:]+", t):
            continue
        base = re.sub(r"\.(js|mjs|cjs|ts|py|exe|cmd|bat|sh)$", "", t.replace("\\", "/").rstrip("/").split("/")[-1])
        if not base or base.startswith("-") or base in (".", "..") or base.isdigit():
            continue
        if base.lower() in LAUNCHERS and len(toks) > 1 and not words:
            first = base
            continue
        words.append(base)
        if len(words) == 2:
            break
    if not words:
        words = [first]
    name = re.sub(r"[^\w.-]+", "", "-".join(words))[:24].strip("-.")
    return name or "job"


def _ports_in(text):
    ports = []
    for rx in (URL_RE, *PORT_RES):
        for m in rx.finditer(text):
            p = int(m.group(1))
            if 1 <= p <= 65535 and p not in ports:
                ports.append(p)
    return ports


def _url_in(text, port):
    for m in URL_RE.finditer(text):
        if int(m.group(1)) == port:
            u = m.group(0).rstrip(".,;:")
            return re.sub(r"//(?:0\.0\.0\.0|\[::\]|::)", "//localhost", u)
    return f"http://localhost:{port}"


# ---------------------------------------------------------------- job
class Job:
    def __init__(self, jid, name, command, cwd, log_path, ready=None):
        self.id, self.name, self.command, self.cwd, self.log_path, self.ready = jid, name, command, cwd, log_path, ready
        self.lines = collections.deque(maxlen=BUF_LINES)
        self.total = 0            # lines ever received (index of the next line)
        self.partial = ""         # incomplete last line
        self.shown_partial = None  # (line index, text) of a partial line already returned to the model
        self.cursor = 0           # first line not yet returned by read/wait
        self.lock = threading.Lock()
        self.t0 = time.time()
        self.t_end = None
        self.proc = None
        self.hjob = None
        self.reader = None
        self.known = {}           # pid -> (exe, creation time) of every process seen in the tree
        self.ports = set()
        self.url = None
        self.stopped = False
        self.log = None

    # output plumbing
    def _feed(self, text, final=False):
        with self.lock:
            buf = (self.partial + text).replace("\r\n", "\n")
            parts = buf.split("\n")
            last = parts.pop()
            if final and last.strip():
                parts.append(last)
            self.partial = "" if final else last
            for raw in parts:
                full = _clean(raw, 1 << 30)
                self.lines.append(_cut(full, LINE_MAX))
                self.total += 1
                if self.log:
                    try:
                        self.log.write(full + "\n")
                    except Exception:
                        pass
            if self.log:
                try:
                    self.log.flush()
                except Exception:
                    pass

    def _read_loop(self):
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        f = self.proc.stdout
        try:
            while True:
                b = f.read1(65536) if hasattr(f, "read1") else f.read(4096)
                if not b:
                    break
                self._feed(dec.decode(b))
        except Exception:
            pass
        self._feed(dec.decode(b"", final=True), final=True)

    def take(self, start, advance):
        """Lines from absolute index `start` (plus the partial line). Returns (lines, skipped, end)."""
        with self.lock:
            base = self.total - len(self.lines)
            s = max(start, base)
            lines = list(self.lines)[s - base:]
            partial = _clean(self.partial) if self.partial.strip() else ""
            sp = self.shown_partial
            if sp and lines and sp[0] == s and lines[0].startswith(sp[1]):
                rest = lines[0][len(sp[1]):].lstrip()
                lines = ([rest] if rest.strip() else []) + lines[1:]
            elif sp and not lines and sp[0] == s and partial.startswith(sp[1]):
                partial = partial[len(sp[1]):]
            if partial.strip():
                lines.append(partial)
            if advance:
                self.cursor = self.total
                self.shown_partial = (self.total, _clean(self.partial)) if self.partial.strip() else None
            return lines, s - start, self.total

    def last_line(self):
        with self.lock:
            if self.partial.strip():
                return _clean(self.partial)
            return next((l for l in reversed(self.lines) if l.strip()), "")


# ---------------------------------------------------------------- manager
class Jobs:
    def __init__(self, cwd, log_dir):
        self._cwd = cwd
        self.log_dir = Path(os.path.expanduser(str(log_dir)))
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.jobs = collections.OrderedDict()
        self.next_id = 1
        self.lock = threading.RLock()
        for f in self.log_dir.glob("*.log"):   # prune logs from old sessions
            try:
                if time.time() - f.stat().st_mtime > 3 * 86400:
                    f.unlink()
            except OSError:
                pass
        self._closing = False
        atexit.register(self.stop_all)
        threading.Thread(target=self._monitor, daemon=True, name="jobs-monitor").start()

    @property
    def cwd(self):
        c = self._cwd() if callable(self._cwd) else self._cwd
        return Path(c or ".")

    @cwd.setter
    def cwd(self, value):
        self._cwd = value

    # -- lookup / status
    def _get(self, key=None, purpose="read"):
        with self.lock:
            if not self.jobs:
                raise JobError("no jobs. Start one with action=start.")
            if key in (None, ""):
                running = [j for j in self.jobs.values() if self.alive(j)]
                if purpose == "stop" and len(running) > 1:
                    raise JobError("several jobs are running; say which one (name). " + self._names())
                return running[-1] if running else list(self.jobs.values())[-1]
            k = str(key).strip().lstrip("#")
            if k.isdigit() and int(k) in self.jobs:
                return self.jobs[int(k)]
            for j in reversed(self.jobs.values()):
                if j.name == k:
                    return j
            hits = [j for j in self.jobs.values() if j.name.startswith(k) or k in j.command]
            if len(hits) == 1:
                return hits[0]
            raise JobError(f"no job {key!r}. " + self._names())

    def _names(self):
        return "Jobs: " + ", ".join(f"{j.id} {j.name} ({self.status(j)})" for j in self.jobs.values())

    def alive(self, job):
        if job.proc is None:
            return False
        if job.proc.poll() is None:
            return True
        if IS_WIN and job.hjob and _job_object_pids(job.hjob):
            return True
        # processes that broke away from the job object / detached, found by the monitor's parent-chain scan
        for pid, (exe, ct) in list(job.known.items()):
            if pid != job.proc.pid and not job.stopped:
                c = _ctime(pid)
                if c is not None and abs(c - ct) < 1:
                    return True
        return False

    def _monitor(self):
        """Every 2 s, record the process tree of running jobs so children that detach (parent exits) stay tracked."""
        while not self._closing:
            time.sleep(2)
            try:
                with self.lock:
                    jobs = [j for j in self.jobs.values() if not j.stopped and j.t_end is None]
                if jobs:
                    procs = _procs()
                    for j in jobs:
                        self.tree(j, procs)
            except Exception:
                pass

    def status(self, job):
        if self.alive(job):
            return "running" if job.proc.poll() is None else "running (shell exited)"
        if job.t_end is None:
            job.t_end = time.time()
        if job.stopped:
            return "stopped"
        return f"exited {job.proc.returncode}"

    def status_count(self):
        with self.lock:
            return sum(1 for j in self.jobs.values() if self.alive(j))

    def tree(self, job, procs=None):
        """{pid: exe} of the job's live processes: job-object members + root + anything whose parent chain leads
        to a process seen in the job (catches children that broke away or detached)."""
        procs = _procs() if procs is None else procs
        live = {}
        cands = set()
        if IS_WIN and job.hjob:
            cands.update(_job_object_pids(job.hjob))
        if job.proc is not None and job.proc.poll() is None:
            cands.add(job.proc.pid)
        for pid in cands:
            if pid in procs:
                live[pid] = procs[pid][1]
                if pid not in job.known:
                    job.known[pid] = (procs[pid][1], _ctime(pid) or job.t0)
        changed = True
        while changed:
            changed = False
            for pid, (ppid, exe) in procs.items():
                if pid in live or ppid not in job.known or ppid == 0 or pid == os.getpid():
                    continue
                pexe, pct = job.known[ppid]
                if ppid in procs and ppid not in live:   # parent pid alive but not ours any more -> reused pid
                    pct2 = _ctime(ppid)
                    if pct2 is None or abs(pct2 - pct) > 1:
                        continue
                ct = _ctime(pid)
                if ct is None or ct < pct - 1 or ct < job.t0 - 2:
                    continue
                live[pid] = exe
                job.known[pid] = (exe, ct)
                changed = True
        return live

    def _job_ports(self, job, listen=None, tree=None):
        listen = _listening() if listen is None else listen
        pids = set(self.tree(job) if tree is None else tree)
        return sorted(p for p, owners in listen.items() if owners & pids), listen

    def _holder(self, port, listen, procs, exclude=()):
        owners = [p for p in listen.get(port, ()) if p not in exclude]
        if not owners:
            return None
        desc = []
        for pid in owners:
            exe = procs.get(pid, (0, "?"))[1]
            ours = next((j for j in self.jobs.values() if pid in j.known and self.alive(j)), None)
            desc.append(f"PID {pid} ({exe}{', ink job ' + str(ours.id) + ' ' + ours.name if ours else ''})")
        return ", ".join(desc)

    def _conflicts(self, job, lines, listen=None):
        """Explain EADDRINUSE-style failures: which PID holds the port."""
        text = "\n".join(lines)
        bad = [l for l in lines if INUSE_RE.search(l)]
        if not bad:
            return ""
        listen = _listening() if listen is None else listen
        procs = _procs()
        mine = set(job.known)
        cands = []
        for p in _ports_in("\n".join(bad)) + [int(m) for m in re.findall(r"(?<![\w.])(\d{2,5})(?![\w.])", "\n".join(bad))] \
                + [int(m) for m in re.findall(r"(?<![\w./-])(\d{4,5})(?![\w.])", job.command)] + _ports_in(text):
            if 1 <= p <= 65535 and p not in cands:
                cands.append(p)
        held = []
        for p in cands:
            h = self._holder(p, listen, procs, exclude=mine)
            if h:
                held.append(f"port {p} is held by {h}")
        msg = "Port conflict: " + bad[0].strip()[:200]
        if held:
            msg += "\n" + "; ".join(held) + ". Stop that process (or the ink job) or use another port."
        else:
            msg += "\n(could not find which process holds the port; it may already be gone - try again)"
        return msg

    # -- start
    def start(self, command, name=None, ready=None, timeout=60, cwd=None):
        command = (command or "").strip()
        if not command:
            raise JobError("start needs a command.")
        if ONE_SHOT.search(command) and not WATCH.search(command):
            raise JobError("this looks like a one-shot build/test/install command; run it with the `run` tool "
                           "(set its timeout for slow ones). `job` is for programs that keep running: dev servers, "
                           "watchers, `npx hardhat node`, game servers.")
        with self.lock:
            running = [j for j in self.jobs.values() if self.alive(j)]
            if len(running) >= MAX_RUNNING:
                raise JobError(f"{len(running)} jobs are already running; stop some first. " + self._names())
            for j in running:
                if j.command == command:
                    where = f" ({j.url})" if j.url else ""
                    raise JobError(f"job {j.id} {j.name!r} is already running this command{where}. Read it with "
                                   f"action=read, or action=restart to restart it.")
            if name:
                name = re.sub(r"[^\w.-]+", "-", str(name)).strip("-")[:32] or None
                if name and name.isdigit():
                    name = "job" + name
            name = name or _auto_name(command)
            taken = {j.name for j in self.jobs.values() if self.alive(j)}
            base, k = name, 2
            while name in taken:
                name, k = f"{base}-{k}", k + 1
            jid = self.next_id
            self.next_id += 1
        wd = Path(cwd) if cwd else self.cwd
        if not wd.is_absolute():
            wd = self.cwd / wd
        if not wd.is_dir():
            raise JobError(f"working directory not found: {wd}")
        before = _listening()
        job = Job(jid, name, command, wd, self.log_dir / f"{jid}-{name}.log", ready)
        self._spawn(job)
        with self.lock:
            self.jobs[jid] = job
            self._prune()
        return self._start_report(job, ready, timeout, before)

    def _spawn(self, job):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", GIT_PAGER="cat", PAGER="cat",
                   NO_COLOR="1", FORCE_COLOR="0", TERM="dumb", BROWSER="none")
        if HAVE_BASH:
            argv = [SHELL_BASH if IS_WIN else "bash", "-c", job.command]
        else:
            argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", job.command]
        job.log = open(job.log_path, "a", encoding="utf-8", errors="replace")
        job.log.write(f"$ {job.command}\n# cwd {job.cwd}  started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        kw = {}
        if IS_WIN:
            job.hjob = _new_job_object()
            # start suspended so the shell can't spawn children before it is in the job object
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | NOWIN | (0x4 if job.hjob else 0)
        else:
            kw["start_new_session"] = True
        try:
            job.proc = subprocess.Popen(argv, cwd=str(job.cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, env=env, **kw)
        except Exception as e:
            if IS_WIN and job.hjob:
                _k32.CloseHandle(job.hjob)
                job.hjob = None
            job.log.close()
            raise JobError(f"could not start: {e}")
        if IS_WIN and job.hjob:
            h = int(job.proc._handle)
            if not _k32.AssignProcessToJobObject(job.hjob, h):
                _k32.CloseHandle(job.hjob)
                job.hjob = None
            _ntdll.NtResumeProcess(h)
        job.t0 = time.time()
        job.known[job.proc.pid] = ("bash.exe" if HAVE_BASH else "shell", _ctime(job.proc.pid) or job.t0)
        job.reader = threading.Thread(target=job._read_loop, daemon=True, name=f"job-{job.id}")
        job.reader.start()

    def _prune(self):
        dead = [j for j in self.jobs.values() if not self.alive(j)]
        for j in dead[:-20]:
            self.jobs.pop(j.id, None)

    def _start_report(self, job, ready, timeout, before):
        res = self._await(job, ready, timeout, start=0, before=before)
        lines, _, end = job.take(0, advance=True)
        out = _fmt(lines, 40, READ_CHARS, job.log_path) or "(no output yet)"
        head = f"Job {job.id} {job.name!r} (pid {job.proc.pid})"
        kind = res["kind"]
        if kind == "exited":
            code = job.proc.returncode
            text = f"{head} FAILED: exited with code {code} after {_dur(time.time() - job.t0)}.\nOutput:\n{out}"
            c = self._conflicts(job, lines, before)
            if c:
                text += "\n" + c
            return text, f"{job.name}: exited {code}"
        port = self._port_note(job)
        if kind == "ready":
            text = f"{head} is ready after {res['elapsed']:.1f}s{port}."
            if res.get("line"):
                text += f"\nReady line: {res['line'].strip()[:300]}"
            summary = f"{job.name}: ready{' ' + job.url if job.url else ''}"
            listen, procs = _listening(), _procs()
            mine = set(job.known)
            for p in sorted(job.ports):
                h = self._holder(p, listen, procs, exclude=mine)
                if h:
                    text += f"\nWarning: port {p} is also held by {h}; requests may go to that process."
        elif kind == "timeout":
            what = "listening on a port" if res["mode"] == "port" else f"/{ready}/"
            text = f"{head} is running but NOT ready: no {what} after {timeout:g}s{port}. It keeps running."
            summary = f"{job.name}: not ready"
        else:
            text = f"{head} started, running{port}."
            summary = f"{job.name}: running{' ' + job.url if job.url else ''}"
        text += f"\nOutput:\n{out}"
        c = self._conflicts(job, lines)
        if c:
            text += "\n" + c
        warn = [p for p in re.findall(r"(?<![\w./-])(\d{4,5})(?![\w.])", job.command)
                if int(p) in before and int(p) not in job.ports]
        if warn and kind != "ready":
            procs = _procs()
            text += "\nWarning: " + "; ".join(f"port {p} was already in use before start by "
                                              f"{self._holder(int(p), before, procs) or '?'}" for p in warn)
        text += f"\n[log: {job.log_path}; use job read/wait/stop name={job.name!r}]"
        return text, summary

    def _port_note(self, job):
        if job.ports:
            return f", listening on port {', '.join(map(str, sorted(job.ports)))} -> {job.url}"
        return ""

    def _update_ports(self, job, listen=None, before=None):
        ports, listen = self._job_ports(job, listen)
        lines, _, _ = job.take(0, advance=False)
        text = "\n".join(lines[-400:])
        mine = set(job.known)
        for p in _ports_in(text):   # e.g. servers in WSL/docker: output says the port, netstat shows another pid
            owners = listen.get(p, set())
            if owners and (owners & mine or (before is not None and p not in before)):
                ports.append(p)
        if ports:
            job.ports.update(ports)
            first = next((p for p in _ports_in(text) if p in job.ports), min(job.ports))
            job.url = _url_in(text, first)
        return ports

    def _await(self, job, ready, timeout, start, before=None):
        """Wait for `ready` (regex, "port", or a port number) in output from line `start`, or for exit."""
        timeout = min(max(float(timeout if timeout not in (None, "") else 60), 0.5), MAX_TIMEOUT)
        t0 = time.time()
        mode, rx, want_port = None, None, None
        r = (ready or "").strip() if isinstance(ready, str) else ready
        if isinstance(r, (int, float)) and not isinstance(r, bool):
            r = str(int(r))
        if r:
            if r.lower() in ("port", "url", "listen", "listening"):
                mode = "port"
            elif re.fullmatch(r":?\d{2,5}", r):
                mode, want_port = "port", int(r.lstrip(":"))
            else:
                mode, rx = "regex", _compile(r)
        last_net = 0.0
        min_wait = 2.0
        while True:
            exited = not self.alive(job)
            if exited and job.reader:
                job.reader.join(1.5)
            lines, _, _ = job.take(start, advance=False)
            if rx:
                for l in lines:
                    if rx.search(l):
                        self._update_ports(job, before=before)
                        return {"kind": "ready", "mode": mode, "line": l, "elapsed": time.time() - t0}
            if mode == "port" and not exited and time.time() - last_net > 0.6:
                last_net = time.time()
                listen = _listening()
                ports = self._update_ports(job, listen, before)
                if want_port is not None:
                    owners = listen.get(want_port, set())
                    ok = want_port in ports or (owners and before is not None and want_port not in before)
                else:
                    ok = bool(ports)
                if ok:
                    if want_port is not None:
                        job.ports.add(want_port)
                        job.url = job.url if job.url and f":{want_port}" in job.url else \
                            _url_in("\n".join(lines), want_port)
                    hit = next((l for l in lines if _ports_in(l)), "")
                    return {"kind": "ready", "mode": mode, "line": hit, "elapsed": time.time() - t0}
            if exited:
                return {"kind": "exited", "mode": mode}
            el = time.time() - t0
            if mode is None:
                if el >= min_wait and (lines or el >= 5):
                    self._update_ports(job, before=before)
                    return {"kind": "started", "mode": None}
            elif el >= timeout:
                return {"kind": "timeout", "mode": mode}
            time.sleep(0.2)

    # -- read / wait
    def _header(self, job):
        st = self.status(job)
        up = _dur((job.t_end or time.time()) - job.t0)
        port = f", {job.url}" if job.url and self.alive(job) else ""
        return f"[job {job.id} {job.name}: {st}, {'ran' if job.t_end else 'up'} {up}{port}]"

    def read(self, name_or_id=None, lines=60, since_last=True, grep=None):
        job = self._get(name_or_id)
        lines = max(int(lines or 60), 1)
        if since_last:
            new, skipped, _ = job.take(job.cursor, advance=True)
        else:
            new, skipped, _ = job.take(0, advance=True)
            new = new[-lines * 4:]
        if grep:
            rx = _compile(grep)
            new = [l for l in new if rx.search(l)]
        head = self._header(job)
        if not new:
            what = f"matching /{grep}/ " if grep else ""
            last = job.last_line()
            return (f"{head} no new output {what}since last read." + (f" Last line: {last[:200]}" if last else ""),
                    f"{job.name}: no new output")
        note = f"[{skipped} older lines dropped from memory; see {job.log_path}]\n" if skipped else ""
        body = _fmt(new, lines, READ_CHARS, job.log_path)
        if not self.alive(job) and not job.stopped:
            c = self._conflicts(job, new)
            if c:
                body += "\n" + c
        return f"{head}\n{note}{body}", f"{job.name}: {len(new)} lines"

    def wait(self, name_or_id=None, pattern=None, timeout=60):
        if not pattern:
            raise JobError("wait needs a pattern (regex, or \"port\").")
        job = self._get(name_or_id)
        start = job.cursor
        res = self._await(job, pattern, timeout, start=start)
        new, _, _ = job.take(start, advance=True)
        body = _fmt(new, 40, READ_CHARS - 400, job.log_path) or "(no new output)"
        head = self._header(job)
        if res["kind"] == "ready":
            m = (res.get("line") or "").strip()[:300]
            text = f"{head} matched /{pattern}/ after {res['elapsed']:.1f}s" + (f": {m}" if m else "") + \
                   f"\nNew output:\n{body}"
            return text, f"{job.name}: matched"
        if res["kind"] == "exited":
            text = f"{head} exited before /{pattern}/ appeared.\nNew output:\n{body}"
            c = self._conflicts(job, new)
            return text + ("\n" + c if c else ""), f"{job.name}: {self.status(job)}"
        return f"{head} /{pattern}/ not seen within {float(timeout):g}s (still running).\nNew output:\n{body}", \
            f"{job.name}: timeout"

    # -- stop
    def stop(self, name_or_id=None, _quiet=False):
        job = self._get(name_or_id, purpose="stop")
        was_alive = self.alive(job)
        procs = _procs()
        tree = self.tree(job, procs)
        ports, _ = self._job_ports(job, tree=tree)
        ports = sorted(set(ports) | job.ports)
        job.stopped = job.stopped or was_alive
        if IS_WIN and job.hjob:
            _k32.TerminateJobObject(job.hjob, 1)
        if job.proc.poll() is None or job.proc.pid in tree:
            _kill_pid(job.proc.pid, tree=True)
        if not IS_WIN:
            try:
                os.killpg(job.proc.pid, 9)
            except OSError:
                pass
        for pid in tree:
            if pid != job.proc.pid:
                _kill_pid(pid, tree=True)
        # second pass: anything spawned while we were killing, or that detached
        deadline = time.time() + 5
        left = {}
        while time.time() < deadline:
            time.sleep(0.2)
            left = self.tree(job)
            if not left:
                break
            for pid in left:
                _kill_pid(pid, tree=True)
        try:
            job.proc.wait(3)
        except Exception:
            pass
        if job.reader:
            job.reader.join(2)
        if IS_WIN and job.hjob:
            _k32.CloseHandle(job.hjob)
            job.hjob = None
        if job.t_end is None:
            job.t_end = time.time()
        try:
            job.log.write(f"# stopped {time.strftime('%H:%M:%S')}\n")
            job.log.close()
        except Exception:
            pass
        if _quiet:
            return "", ""
        busy = {}
        t_end = time.time() + 4
        ours = set(job.known)
        while ports:
            listen = _listening()
            busy = {p: listen[p] for p in ports if p in listen}
            # wait only while one of the killed processes still appears as owner (Windows lags a moment)
            if not any(o & ours for o in busy.values()) or time.time() > t_end:
                break
            time.sleep(0.3)
        killed = ", ".join(f"{pid} {exe}" for pid, exe in sorted(tree.items())) or "none left"
        if was_alive:
            text = f"Stopped job {job.id} {job.name!r} after {_dur(job.t_end - job.t0)}; killed processes: {killed}."
        else:
            text = f"Job {job.id} {job.name!r} had already {self.status(job)}; cleaned up: {killed}."
        if left:
            text += f"\nWARNING: still alive: {', '.join(f'{p} {e}' for p, e in left.items())}"
        if ports:
            free = [p for p in ports if p not in busy]
            if free:
                text += f"\nPort {', '.join(map(str, free))} is free."
            if busy:
                procs = _procs()
                text += "\nWARNING: " + "; ".join(f"port {p} still in use by {self._holder(p, busy, procs) or '?'}"
                                                  for p in busy)
        return text, f"stopped {job.name}"

    def restart(self, name_or_id=None, ready=None, timeout=60):
        job = self._get(name_or_id, purpose="stop")
        if self.alive(job):
            self.stop(job.id)
        text, summary = self.start(job.command, name=job.name, ready=ready or job.ready, timeout=timeout,
                                   cwd=str(job.cwd))
        return "Restarted. " + text, summary

    def stop_all(self):
        self._closing = True
        n = 0
        for job in list(self.jobs.values()):
            try:
                if self.alive(job) or self.tree(job):
                    self.stop(job.id, _quiet=True)
                    n += 1
            except Exception:
                pass
        return n

    # -- list
    def list(self):
        with self.lock:
            jobs = list(self.jobs.values())
        if not jobs:
            return "No jobs.", "0 jobs"
        rows = [("id", "name", "status", "uptime", "port/url", "last output")]
        for j in jobs:
            alive = self.alive(j)
            if alive and not j.ports:
                self._update_ports(j)
            rows.append((str(j.id), j.name, self.status(j), _dur((j.t_end or time.time()) - j.t0),
                         (j.url or "-") if alive else "-", j.last_line()[:70] or "-"))
        w = [max(len(r[i]) for r in rows) for i in range(5)]
        text = "\n".join("  ".join(r[i].ljust(w[i]) for i in range(5)) + "  " + r[5] for r in rows)
        n = sum(1 for j in jobs if self.alive(j))
        return text, f"{n} running"


# ---------------------------------------------------------------- the model's tool
JOBS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "job",
        "description": (
            "Run long-lived programs in the background (dev servers, watchers, game backends, `npx hardhat node`) "
            "while you keep working; they survive across tool calls and are stopped when ink exits. Use `run` for "
            "builds/tests. Actions: start (command; ready = regex or \"port\" to wait until it listens), read (new "
            "output since last read; pattern filters lines), wait (until pattern appears or the job exits), stop "
            "(kills the whole process tree), restart, list. "
            'Examples: {"action":"start","command":"npm run dev","name":"web","ready":"port"} '
            '{"action":"wait","name":"web","pattern":"compiled|error","timeout":30}'
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "read", "wait", "stop", "restart", "list"]},
                "command": {"type": "string", "description": "start: shell command (Git Bash) run in the working directory"},
                "name": {"type": "string", "description": "job name or id; on start an optional label (default: from the command)"},
                "pattern": {"type": "string", "description": "wait: regex to wait for; read: only show lines matching it"},
                "ready": {"type": "string", "description": "start: regex that means ready, or \"port\" = wait until it listens on a TCP port"},
                "timeout": {"type": "number", "description": "seconds to wait for ready/pattern, default 60"},
            },
            "required": ["action"],
        },
    },
}

_ARG_ALIASES = {"cmd": "command", "id": "name", "job": "name", "job_id": "name", "regex": "pattern", "grep": "pattern",
                "until": "pattern", "match": "pattern", "wait_for": "ready", "ready_pattern": "ready", "dir": "cwd"}
_ACTION_ALIASES = {"run": "start", "spawn": "start", "launch": "start", "logs": "read", "log": "read", "output": "read",
                   "tail": "read", "kill": "stop", "terminate": "stop", "status": "list", "ls": "list", "ps": "list"}


def run_job_tool(jobs, args):
    """Dispatch a `job` tool call. Never raises: errors come back as "Error: ..." text."""
    try:
        if not isinstance(args, dict):
            raise JobError("arguments must be an object.")
        a = {_ARG_ALIASES.get(k, k): v for k, v in args.items()}
        action = str(a.get("action") or ("start" if a.get("command") else "list")).strip().lower()
        action = _ACTION_ALIASES.get(action, action)
        name = a.get("name")
        name = None if name in (None, "") else str(name)
        timeout = a.get("timeout")
        try:
            timeout = float(timeout) if timeout not in (None, "") else 60.0
        except (TypeError, ValueError):
            timeout = 60.0
        if action == "start":
            return jobs.start(a.get("command") or "", name=name, ready=a.get("ready") or None, timeout=timeout,
                              cwd=a.get("cwd") or None)
        if action == "read":
            return jobs.read(name, lines=int(a.get("lines") or 60), since_last=not a.get("all"),
                             grep=a.get("pattern") or None)
        if action == "wait":
            return jobs.wait(name, a.get("pattern") or a.get("ready"), timeout=timeout)
        if action == "stop":
            if name and name.lower() == "all":
                return f"Stopped {jobs.stop_all()} jobs.", "stopped all"
            return jobs.stop(name)
        if action == "restart":
            return jobs.restart(name, ready=a.get("ready") or None, timeout=timeout)
        if action == "list":
            return jobs.list()
        raise JobError(f"unknown action {action!r}; use start, read, wait, stop, restart or list.")
    except JobError as e:
        return f"Error: {e}", "error"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}", "error"
