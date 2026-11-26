# ink TUI

Claude Code-style terminal front-end for the ink engine. It spawns `ink.py --rpc` and speaks the
JSON-lines protocol in `../PROTOCOL.md`. Built with [Ink](https://github.com/vadimdemedes/ink) + React,
plain ESM with no build step.

```
node tui\ink-tui.mjs [ink flags]     # real engine (python ink.py --rpc <flags>)
node tui\ink-tui.mjs --mock          # scripted mock engine (tui\mock-engine.mjs), no model needed
node tui\test\smoke.mjs [--dump out.txt] [--cols 100 --rows 40]   # end-to-end test (headless xterm)
```

Finished items (prompts, answers, tool rows, diffs, notices) are printed once into the normal terminal
scrollback. Only the bottom area is redrawn: the streaming block, the spinner, the plan, the input box and
the status line.

## Keys

| key | action |
|---|---|
| enter | submit. While a turn runs, the prompt is queued and sent when the turn ends |
| `\` then enter, ctrl+enter, ctrl+j, alt+enter, shift+enter* | insert a newline (idle) |
| ctrl+enter / alt+s (while busy) | send the input as `steer` for the running turn |
| up / down | history (`~/.ink/tui-history.jsonl`), or move between lines of a multi-line input; up with an empty input pulls back the last queued prompt |
| esc | busy: interrupt. Palette/completions: close. Idle: twice quickly runs `/checkpoints` |
| ctrl+c | clear input. Busy with empty input: interrupt. Twice (within 1.5s): exit |
| ctrl+d | exit (empty input) |
| shift+tab | cycle mode auto -> safe -> plan |
| ctrl+o | print the full thinking + tool outputs of the last turn into scrollback |
| ctrl+l | clear the screen |
| `/` | command palette (fuzzy). up/down to select, tab to complete, enter to run |
| `@` | file completion (`complete` request). up/down, tab/enter to accept |
| `!` `#` `&` | bash mode / memory note / background task hints (the engine handles them) |
| `?` (alone) | show shortcuts |
| left/right, ctrl+left/right, home/end, ctrl+a/e, ctrl+w, ctrl+u, ctrl+k, alt+b/f | editing |
| permission box | 1/2/3, y/n, or up/down + enter. Option 3 asks what to do instead (sent as `answer.text`); esc = no |

Pastes bigger than 800 chars or 10 lines show as `[Pasted text #1 +42 lines]`. The full text is still
submitted, and backspace removes the whole placeholder.

\* **Windows Terminal:** shift+enter sends the same byte as enter, so the TUI cannot tell them apart
and shift+enter submits. Alt+enter is taken by Windows Terminal for fullscreen. Ctrl+enter reaches
the app as `\n` (LF), which the TUI uses for newline (idle) and steer (busy). A trailing `\` + enter
always works. Terminals that send kitty (`CSI 13;2u`) or modifyOtherKeys (`CSI 27;2;13~`) sequences
get real shift+enter support.

## Environment

| var | meaning |
|---|---|
| `INK_PY` | python executable (default: the hermes python 3.14) |
| `INK_ENGINE` | engine script (default `..\ink.py`). A `.mjs`/`.js` path is run with node |
| `INK_THEME` | `dark` (default) or `light` |
| `INK_TUI_LOG` | log file for engine stderr (default `~/.ink/tui.log`) |
| `INK_TUI_SCRIPT` | JSON file of scripted steps for non-interactive runs: `{"until":"hello"\|"idle"\|"busy"\|"ask"}`, `{"type":"text"}`, `{"keys":"\r"}`, `{"wait":ms}`, `{"quit":true}` |
| `MOCK_SPEED` | mock engine time multiplier (e.g. `0.2`) |

The engine's stderr never reaches the screen. If the engine exits unexpectedly, the TUI prints the last
log lines and exits with the engine's exit code. At the end of a turn that took more than 30s, the TUI
rings the terminal bell.

## Layout

- `ink-tui.mjs`: entry point (args, spawn the engine, start the UI)
- `lib/engine.mjs`: spawns the engine, JSON-lines I/O, stderr to the log
- `lib/controller.mjs`: state machine (protocol events + keys to snapshots)
- `lib/keys.mjs`: raw stdin key parser (bracketed paste, CSI/SS3, kitty, LF vs CR)
- `lib/editor.mjs`: input buffer, paste placeholders, history
- `lib/markdown.mjs`, `lib/highlight.mjs`: markdown to ANSI (highlight.js loads lazily)
- `lib/tui.mjs`: wiring plus the `INK_TUI_SCRIPT` runner
- `components/*.mjs`: Ink components (items, live area, panels, input/status line)
- `mock-engine.mjs`: protocol mock. `test/smoke.mjs`: end-to-end test
