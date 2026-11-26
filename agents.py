"""Subagents for ink: a child conversation with a fresh context, its own instructions and a limited tool set,
that does one job (explore the code, review a diff, write and run tests) and returns only a short report.
The main conversation stays small - and its KV cache is never clobbered:

  local   - the llama-server on this PC. With 2+ slots the subagent uses slot 1; with one slot the main
            conversation's KV is saved to disk first and restored afterwards (a few seconds).
  remote  - any OpenAI-compatible endpoint from ~/.ink/providers.json (free tiers: OpenRouter, Nous Portal,
            opencode zen, Groq, Cerebras, ...). Providers are rotated on rate limits (429/quota) with cooldowns and
            daily caps, and fall back to local. Secrets are redacted from everything sent out, and projects
            marked private never go remote.

~/.ink/providers.json:
{"providers": [{"name": "openrouter", "base_url": "https://openrouter.ai/api/v1", "model": "some/model:free",
                "key_env": "OPENROUTER_API_KEY", "daily": 200, "rpm": 20}],
 "remote": "prefer" | "fallback" | "off"}          (default "off" until you add providers)
Keys come from the environment variable named in key_env (never stored in the file).
Per project, .ink/settings.json {"private": true} keeps every subagent local.

Custom agents: ~/.ink/agents/<name>.md or <project>/.ink/agents/<name>.md with front matter
---
name: gas-auditor
description: one line, shown to the main model
tools: read_file, grep, glob, map        (subset of: read_file grep glob map run edit_file write_file check)
where: local | remote | any
---
<system prompt>
"""
import json, os, re, time, http.client, urllib.request, urllib.error, threading
from pathlib import Path

STATE = Path.home() / ".ink"
PROVIDERS_FILE = STATE / "providers.json"
USAGE_FILE = STATE / "provider-usage.json"
READ_TOOLS = ("read_file", "grep", "glob", "map")

BUILTIN = {
    "explore": dict(
        description="Read-only investigation: find where something lives, how a flow works, what calls what. "
                    "Returns file:line findings.",
        tools=READ_TOOLS, where="any",
        prompt="You are a code explorer working for another agent. Investigate the codebase with map, grep, glob and "
               "read_file to answer the task. Be efficient: map/grep first, then read only the line ranges you need. "
               "Do not guess - every claim must come from code you read. Finish with a compact report: the answer, "
               "then the evidence as `path:line - what is there` bullets (max 15), then open questions. No preamble."),
    "review": dict(
        description="Independent code review of a diff: bugs, broken callers, missed edge cases, security issues.",
        tools=READ_TOOLS, where="any",
        prompt="You are a strict senior code reviewer. You get a goal and a diff. Look for REAL defects only: logic "
               "bugs, wrong conditions, off-by-one, missed edge cases the goal implies, callers broken by a changed "
               "signature/name (grep for them), state not updated, async/await mistakes, security problems (for "
               "Solidity: reentrancy, access control, unchecked transfers, rounding, randomness). Use the tools to "
               "verify each suspicion in the code before reporting it. Do not report style, naming, missing comments or "
               "speculative issues. Reply with NO_ISSUES if the change is correct, otherwise numbered findings: "
               "`path:line - problem - why it is wrong - suggested fix` (max 6, most severe first)."),
    "debug": dict(
        description="Fresh-eyes diagnosis of one failing test/check the main agent is stuck on.",
        tools=READ_TOOLS + ("run",), where="local",
        prompt="You are a debugger with fresh eyes. Another agent is stuck on one failing check and keeps trying "
               "fixes. You get the goal, the failure output and its diff. Find the ROOT CAUSE: read the failing test "
               "and the code it exercises, then decide which is wrong - the code, the test's expected value, or the "
               "test's assumptions (e.g. a simplified strategy vs a textbook number, rounding, randomness, order). "
               "Prove it with one small experiment using run (a one-off node -e / tsx script or a single test), but do "
               "not edit any files. Report in at most 12 lines: Cause (one sentence), Evidence (what you ran and saw), "
               "Fix (exact file:line and code, or the corrected expected value with the reason)."),
    "test": dict(
        description="Write or extend tests for given code, run them, and report what passes/fails.",
        tools=READ_TOOLS + ("run", "write_file", "edit_file"), where="local",
        prompt="You are a test engineer. Write focused tests for the task (follow the project's existing test style "
               "and runner: look at existing tests first), run them with `run`, fix the TESTS (never the code under "
               "test) until they correctly express the intended behaviour. Only create or edit test files. Report: "
               "files written, the command to run them, and which behaviours pass or fail (a failing test that "
               "reveals a real bug is a valuable result - describe the bug)."),
}

SECRET_RX = re.compile(r"(?i)(-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----|"
                       r"\b(?:private[_-]?key|mnemonic|seed[_ ]?phrase|api[_-]?key|secret|token|password)\b\s*[:=]\s*\S+|"
                       r"\b0x[0-9a-f]{64}\b|\bghp_[A-Za-z0-9]{36}\b|\bsk-[A-Za-z0-9_-]{32,}|\b\d{8,10}:AA[A-Za-z0-9_-]{33}\b)")


def redact(text):
    return SECRET_RX.sub("[REDACTED]", text or "")


def _front(text):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        if k.strip():
            meta[k.strip().lower()] = v.strip()
    return meta, m.group(2).strip()


def load_agents(cwd):
    agents = {k: dict(v, name=k) for k, v in BUILTIN.items()}
    for d in (STATE / "agents", Path(cwd) / ".ink" / "agents"):
        for f in sorted(d.glob("*.md")) if d.is_dir() else []:
            try:
                meta, body = _front(f.read_text("utf-8", "replace"))
            except Exception:
                continue
            name = re.sub(r"[^a-z0-9-]", "-", (meta.get("name") or f.stem).lower())
            tools = tuple(t.strip() for t in (meta.get("tools") or ",".join(READ_TOOLS)).split(",") if t.strip())
            agents[name] = dict(name=name, description=meta.get("description", ""), tools=tools,
                                where=meta.get("where", "any"), prompt=body)
    return agents


# ---------------------------------------------------------------- providers
class Router:
    def __init__(self, log=lambda m: None):
        self.log = log
        self.cool = {}  # provider name -> unix time when usable again
        try:
            self.cfg = json.loads(PROVIDERS_FILE.read_text("utf-8")) if PROVIDERS_FILE.is_file() else {}
        except Exception:
            self.cfg = {}
        self.providers = [p for p in self.cfg.get("providers", []) if p.get("base_url") and p.get("model")]
        self.mode = self.cfg.get("remote", "off")  # fully local unless explicitly enabled (user rule)

    def _usage(self):
        try:
            u = json.loads(USAGE_FILE.read_text("utf-8"))
        except Exception:
            u = {}
        today = time.strftime("%Y-%m-%d")
        return u if u.get("day") == today else {"day": today}

    def _count(self, name):
        u = self._usage()
        u[name] = u.get(name, 0) + 1
        try:
            USAGE_FILE.write_text(json.dumps(u), "utf-8")
        except Exception:
            pass

    def available(self):
        u, now = self._usage(), time.time()
        out = []
        for p in self.providers:
            key = os.environ.get(p.get("key_env", "")) if p.get("key_env") else p.get("key_literal_unsafe")
            if not key and p.get("key_env"):
                continue
            if self.cool.get(p["name"], 0) > now:
                continue
            if p.get("daily") and u.get(p["name"], 0) >= int(p["daily"]):
                continue
            out.append(dict(p, key=key))
        return out

    def chat(self, messages, tools, max_tokens=4096, timeout=300):
        """Try remote providers in order; returns (message, provider_name) or (None, reason)."""
        last = "no remote provider available"
        for p in self.available():
            body = {"model": p["model"], "messages": messages, "max_tokens": max_tokens, "temperature": 0.3}
            if tools:
                body["tools"] = tools
            req = urllib.request.Request(p["base_url"].rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {p['key']}",
                                                  "User-Agent": "ink-agent"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    j = json.loads(r.read())
                self._count(p["name"])
                msg = j["choices"][0]["message"]
                return {k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")}, p["name"]
            except urllib.error.HTTPError as e:
                wait = 60 if e.code == 429 else 600 if e.code in (402, 403) else 120
                ra = e.headers.get("Retry-After") if e.headers else None
                if ra and ra.isdigit():
                    wait = int(ra)
                self.cool[p["name"]] = time.time() + wait
                last = f"{p['name']}: HTTP {e.code}"
                self.log(f"provider {last}, cooling {wait}s")
            except Exception as e:
                self.cool[p["name"]] = time.time() + 120
                last = f"{p['name']}: {e!r}"
                self.log(f"provider {last}")
        return None, last


# ---------------------------------------------------------------- local backend
def _post(port, path, body, timeout=900):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    c.request("POST", path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, json.loads(data or b"{}")


def _slots(port):
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        c.request("GET", "/slots")
        j = json.loads(c.getresponse().read() or b"[]")
        c.close()
        return len(j)
    except Exception:
        return 1


class LocalSlot:
    """Context manager: a slot for a subagent that leaves the main conversation's KV cache intact."""
    def __init__(self, port, main_kv_name, log=lambda m: None):
        self.port, self.kv, self.log = port, main_kv_name, log
        self.slot, self.saved = 0, False

    def __enter__(self):
        if _slots(self.port) >= 2:
            self.slot = 1
        else:
            try:
                st, _ = _post(self.port, "/slots/0?action=save", {"filename": self.kv}, timeout=300)
                self.saved = st == 200
            except Exception as e:
                self.log(f"subagent: main KV save failed {e!r}")
        return self

    def __exit__(self, *a):
        if self.saved:
            try:
                _post(self.port, "/slots/0?action=restore", {"filename": self.kv}, timeout=300)
            except Exception as e:
                self.log(f"subagent: main KV restore failed {e!r}")
        return False


# ---------------------------------------------------------------- the subagent loop
class Subagent:
    """run() -> report text. `host` is the ink Agent (for its tools, port, kwargs, printing and permissions)."""
    MAX_STEPS = 30
    MAX_CTX_CHARS = 90000  # ~22k tokens: the subagent wraps up beyond this

    def __init__(self, host, spec, router=None):
        self.host, self.spec = host, spec
        self.router = router

    def _tools(self):
        allowed = set(self.spec["tools"])
        return [t for t in self.host.all_tools() if t["function"]["name"] in allowed]

    def _where(self):
        private = bool(self.host.hooks and self.host.hooks.settings.get("private"))
        r = self.router
        if private or not r or r.mode == "off" or self.spec.get("where") == "local" or not r.available():
            return "local"
        if self.spec.get("where") == "remote":
            return "remote"
        return "remote" if r.mode == "prefer" else "local"

    def _call(self, where, messages, tools, think=True):
        if where == "remote":
            send = [dict(m, content=redact(m.get("content") or "")) for m in messages]
            msg, who = self.router.chat(send, tools)
            if msg is not None:
                return msg, who
            self.host.say(f"remote unavailable ({who}) - using the local model")
        kw = self.host.kwargs()
        if not think:
            kw = {"enable_thinking": False} if "enable_thinking" in kw else {"reasoning_effort": "low"}
        body = {"model": "local", "messages": messages, "max_tokens": 12000, "tools": tools,
                "chat_template_kwargs": kw, "id_slot": self.slot, "cache_prompt": True}
        if not tools:
            body.pop("tools")
        st, j = _post(self.host.P["port"], "/v1/chat/completions", body, timeout=1800)
        if st != 200:
            raise RuntimeError(f"local model HTTP {st}: {str(j)[:200]}")
        m = j["choices"][0]["message"]
        return {k: v for k, v in m.items() if k in ("role", "content", "tool_calls", "reasoning_content")}, "local"

    def run(self, task, context=""):
        where = self._where()
        tools = self._tools()
        messages = [{"role": "system", "content": self.spec["prompt"] + f"\n\nWorking directory: {self.host.tools.cwd}"},
                    {"role": "user", "content": task + (f"\n\nContext:\n{context}" if context else "")}]
        self.host.say(f"{self.spec['name']} agent ({where}) started")
        guard = LocalSlot(self.host.P["port"], self.host.kv_name() + ".main", self.host.log) if where == "local" else None
        self.slot = 0
        t0 = time.time()
        try:
            if guard:
                guard.__enter__()
                self.slot = guard.slot
            for step in range(self.MAX_STEPS):
                if self.host.stop.is_set():
                    return "Subagent interrupted by the user."
                size = sum(len(json.dumps(m)) for m in messages)
                if size > self.MAX_CTX_CHARS or step == self.MAX_STEPS - 1:
                    messages.append({"role": "user", "content": "Stop investigating now and write your final report "
                                     "with what you have."})
                    tools_now = []
                else:
                    tools_now = tools
                msg, who = self._call(where, messages, tools_now)
                messages.append(msg)
                calls = msg.get("tool_calls") or []
                if not calls:
                    text = (msg.get("content") or "").strip()
                    if not text and where == "local":
                        # a thinking model can spend the whole budget reasoning and never write the report:
                        # ask once more with thinking off, from what it has gathered
                        messages.append({"role": "user", "content": "Write your final report now, from what you "
                                         "have already found. No more investigation."})
                        msg, who = self._call(where, messages, [], think=False)
                        messages.append(msg)
                        text = (msg.get("content") or "").strip()
                        if not text:  # last resort: the conclusion at the end of its reasoning
                            text = (msg.get("reasoning_content") or messages[-3].get("reasoning_content") or "").strip()[-3000:]
                    self.host.say(f"{self.spec['name']} agent done in {time.time() - t0:.0f}s ({step + 1} steps, {who})")
                    return text[:8000] or "(the subagent returned no report)"
                for tc in calls:
                    name = tc["function"]["name"]
                    if name not in self.spec["tools"]:
                        res = f"Error: tool {name} is not available to this agent."
                    else:
                        res = self.host.sub_tool(tc, self.spec["name"])
                    messages.append({"role": "tool", "tool_call_id": tc.get("id") or name, "content": res[:12000]})
            return "(the subagent ran out of steps)"
        except Exception as e:
            return f"Subagent failed: {e!r}"
        finally:
            if guard:
                guard.__exit__()


AGENT_SCHEMA_DESC = ("Delegate a self-contained job to a subagent with a fresh context; you get back only its short "
                     "report, so your own context stays small. kind: {kinds}. task: everything it needs (goal, files, "
                     "what to report) - it cannot see this conversation. Use explore before reading many files yourself; "
                     "use review for an independent check of risky changes.")


def agent_schema(agents):
    kinds = "; ".join(f"{k} = {v['description']}" for k, v in agents.items())
    return {"type": "function", "function": {"name": "agent", "description": AGENT_SCHEMA_DESC.format(kinds=kinds),
            "parameters": {"type": "object", "properties": {
                "kind": {"type": "string", "enum": list(agents)}, "task": {"type": "string"}},
                "required": ["kind", "task"]}}}
