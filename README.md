# ink

A local coding agent for your terminal that runs entirely on your own PC - built for a normal gaming GPU
(developed on an **RTX 4060 8 GB + 32 GB RAM**). No cloud, no API keys, no Claude/OpenAI: two open models on a
patched llama.cpp, wrapped in a harness that does the hard parts a small model can't do alone.

- **Ornith 1.5 35B-A3B** (fast, default) - reads ~1,100 tok/s, writes ~35 tok/s on an 8 GB card
- **Inkling 2 Mini** (smart) - for harder reasoning; `/smart` and `/fast` switch, the conversation is kept

## Install (Windows + NVIDIA)

```powershell
git clone https://github.com/BesiBesi123/ink
cd ink
powershell -ExecutionPolicy Bypass -File install.ps1
```

The installer checks your GPU, RAM, Python 3.11+ and Node 20+ (offers to install them with winget), then downloads:

| What | Where | Size |
|---|---|---|
| ink's llama.cpp build (upstream + patches, CUDA 12.4) | `~/.ink/bin/llama` | ~0.7 GB |
| Ornith 1.5 35B-A3B APEX-I MiniPlus (fast model) | `~/.ink/models` | 15.2 GB |
| Inkling 2 Mini (smart model) | `~/.ink/models` | see `models.json` |
| terminal UI packages | `tui/node_modules` | small |

and puts an `ink` command on your PATH. Downloads resume if interrupted - just run the installer again.
`-Models fast` installs only Ornith. Then, in any project folder:

```
ink
```

The first start tunes the memory layout for your card (which expert layers go on the GPU, context size) and
remembers it in `~/.ink/fit.json`.

## What the harness does

ink is built around one idea: **things that are easy for a big model are hard for a small one, so the harness does
them** - structure, checking, memory, and getting unstuck.

- **Work loop for every task** - orient (repo map on the first prompt), plan (goal, key points, small testable
  steps), build one step, run it, mark it done. Marking a step done runs the project's checks; a failing step goes
  back to "doing". Plan mode switches to building by itself once a self-critique says the plan is solid.
- **Checks after every edit** - syntax for JS/TS/HTML/JSON/Python/Solidity, `tsc --noEmit` for TypeScript, LSP-lite
  diagnostics (wrong imports/exports, renamed functions, missing events), project tests/lint auto-detected.
- **Loop breakers** - same test failing 3x: a guided diagnosis (code vs expected value vs assumptions); 5x: a fresh
  local debugger agent with a clean context investigates and hands back the root cause; 6 edits to one file without a
  passing check: stop and diagnose; 30 min on one plan step: re-plan; cut-off replies are never half-executed.
- **Independent review** - after a turn with edits, a reviewer agent reads only the diff.
- **Context that stays fast** - only the latest thinking is sent back, compaction at 120k tokens keeps the KV cache,
  sessions and caches are saved to disk (resuming a big session takes seconds).
- **Standards** - playbooks (engineering standard, new-project blueprint, modern stack, UI, backend/realtime, testing,
  delivery, games, web3/Solidity, real-money games, TypeScript) are matched to each request and their top rules are put
  in front of the model.
- **Safety** - pushes, deploys, publishing and writes outside your user folder always ask; `git push` runs a secret
  scan first. Hooks in the Claude Code format (`~/.ink/settings.json`).
- **Terminal UI** - Node + Ink: tool cards, diffs, collapsible thinking, `/` commands, `@file`, `!shell`, `#memory`,
  `&subagent`, type while it works to queue or steer, `/stop`.

Live trace of every session in `~/.ink/trace/` (`python ink_watch.py` prints a digest).

## llama.cpp

Release builds come from `.github/workflows/build-llama.yml`: upstream llama.cpp at `llama/LLAMA_COMMIT` plus the
patches in `llama/patches` (see `llama/README.md` - the main one fixes prompt processing for Inkling's looped layers
on 8 GB cards, llama.cpp issue #32910).

## Configuration

`~/.ink/config.json` - any key, e.g. `{"default_model": "fast", "fast_compact_tokens": 120000, "escalate": "local"}`.
Model and llama paths: `fast_model`, `smart_model`, `fast_exe`, `smart_exe`, `models_dir`, `llama_dir`.

## License

MIT (see LICENSE). Models and llama.cpp keep their own licenses.
