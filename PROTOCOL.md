# ink engine <-> UI protocol (v1)

The engine runs as `ink --rpc [normal ink flags]` (cwd = the project). It reads **JSON lines on stdin** and writes
**JSON lines on stdout** (one object per line, UTF-8). stderr is free-form log text (UIs may ignore it).
Every object has a `type`. Unknown types must be ignored by both sides (forward compatibility).

## Engine -> UI events

| type | fields | meaning |
|---|---|---|
| `hello` | `version`(1), `model`, `profile` (smart/fast), `effort`, `ctx`, `cwd`, `mode` (auto/safe/plan), `session`, `resumed` (int: prompts restored), `commands` [{name, args, desc}], `tips` [str] | first event; draw the welcome box |
| `status` | `text` | transient one-line status (loading model, running checks…). Replace previous status; `text:""` clears |
| `turn_start` | `id`, `text` | a user prompt started processing (echo it into scrollback if the UI didn't already) |
| `progress` | `done`, `total` | prompt prefill progress in tokens (the model is reading) |
| `thinking_delta` | `text` | streamed reasoning text |
| `thinking_end` | `secs`, `tokens` | reasoning block finished -> collapse to "∴ Thought for Ns" |
| `text_delta` | `text` | streamed answer text (markdown) |
| `text_end` | | answer block finished (render final markdown) |
| `tool_start` | `id`, `name`, `label` (e.g. `Read(src/a.js)`), `args` {} | a tool call starts -> draw "● label" |
| `tool_end` | `id`, `ok` (bool), `summary` (one line), `preview` [≤6 lines], `output` (full text, capped ~20k) | tool finished -> "⎿ summary" (+ preview); full output for the expanded view |
| `diff` | `id`, `path`, `diff` (unified diff text, no file headers), `added`, `removed`, `created` (bool) | file change of tool `id` |
| `plan` | `goal`, `key_points` [str], `steps` [{step, status: todo/doing/done/dropped}] | the current plan (replace the panel) |
| `check` | `name`, `ok`, `summary` | one automatic check result |
| `notice` | `level` (info/warn/error), `text` | a line of plain information for the scrollback |
| `output` | `text` | multi-line plain output of a slash command (/map, /context, /help…) |
| `ask` | `id`, `kind` (run/write/code/outward), `title`, `detail`, `options` ["yes","always","no"] | permission request; the engine blocks until `answer` |
| `stats` | `tps`, `pps`, `ctx_used`, `ctx`, `cache_hit` (0-1), `secs`, `steps`, `model`, `effort`, `mode`, `branch`, `jobs` (int) | after each model call and at turn end -> status line |
| `turn_end` | `secs`, `steps`, `interrupted` (bool) | turn finished; UI may send the next queued prompt |
| `completions` | `req`, `items` [{value, label, desc}] | answer to `complete` |
| `sessions` | `items` [{id, when, cwd, title, prompts}] | answer to `/resume` listing |
| `bye` | | engine is exiting |

## UI -> engine commands

| type | fields | meaning |
|---|---|---|
| `prompt` | `text` | a user message. If a turn is running it is queued (FIFO) |
| `steer` | `text` | guidance for the RUNNING turn: injected at the next tool boundary (if the turn ends first it becomes the next prompt) |
| `interrupt` | | stop the running turn (like Esc) |
| `answer` | `id`, `choice` (yes/always/no), `text` (optional: what to do instead) | reply to `ask` |
| `command` | `name`, `arg` | a slash command without the slash, e.g. {"name":"compact","arg":""} |
| `mode` | `mode` (auto/safe/plan) | change permission/plan mode (shift+tab) |
| `complete` | `req`, `kind` (file/command), `prefix` | ask for completions (@-mentions, / palette) |
| `quit` | | save and exit |

## Rules
- Text deltas may arrive in any chunk size. A UI should render scrollback for finished blocks once (static) and
  keep only the live block (current thinking/answer/tool) redrawable.
- `@path` mentions in prompts are sent as typed; the engine expands them.
- A prompt starting with `!` is a shell command run by the engine (`notice`/`output` back, no model call);
  `#` saves a memory note; `&` runs the prompt as a background subagent task.
