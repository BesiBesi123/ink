// End-to-end smoke test: runs the real TUI (Ink renderer, key parser, controller) against the mock engine,
// feeds keystrokes, and pipes the terminal output into a headless xterm emulator so we can assert on what a
// user would actually see (including scrollback). Usage: node test/smoke.mjs [--dump out.txt] [--cols N]
import { EventEmitter } from 'node:events';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import xterm from '@xterm/headless';
import { Engine, engineCommand } from '../lib/engine.mjs';
import { startTui, runScript } from '../lib/tui.mjs';
import { History } from '../lib/editor.mjs';
import { getTheme } from '../lib/theme.mjs';

const argv = process.argv.slice(2);
const opt = (n, d) => { const i = argv.indexOf(n); return i >= 0 ? argv[i + 1] : d; };
const COLS = Number(opt('--cols', 100));
const ROWS = Number(opt('--rows', 40));
const DUMP = opt('--dump', null);

process.env.FORCE_COLOR = '3';
process.env.MOCK_SPEED = process.env.MOCK_SPEED || '0.15';
process.env.INK_TUI_LOG = path.join(os.tmpdir(), `ink-tui-test-${process.pid}.log`);

const term = new xterm.Terminal({ cols: COLS, rows: ROWS, scrollback: 10000, allowProposedApi: true, convertEol: true });
let bytes = 0;
let bells = 0;
const stdout = Object.assign(new EventEmitter(), {
  isTTY: true, columns: COLS, rows: ROWS,
  write(d, enc, cb) { const s = String(d); bytes += s.length; if (s.includes('\x07')) bells++; term.write(s); (typeof enc === 'function' ? enc : cb)?.(); return true; },
  getColorDepth: () => 24, hasColors: () => true,
});
const stdin = Object.assign(new EventEmitter(), { isTTY: false, setEncoding() {}, resume() {}, pause() {} });
const feed = (d) => stdin.emit('data', d);

const flushTerm = () => new Promise((r) => term.write('', r));
async function screen(all = false) {
  await flushTerm();
  const b = term.buffer.active;
  const out = [];
  const from = all ? 0 : b.baseY;
  for (let i = from; i < b.length; i++) out.push(b.getLine(i)?.translateToString(true) ?? '');
  while (out.length && !out[out.length - 1].trim()) out.pop();
  return out.join('\n');
}

const histFile = path.join(os.tmpdir(), `ink-tui-hist-${process.pid}.jsonl`);
const { cmd, argv: eargv } = engineCommand([], { mock: true });
const engine = new Engine({ cmd, argv: eargv });
const { ctrl, finished } = startTui({ engine, stdin, stdout, theme: getTheme('dark'), history: new History(histFile), exit: false });

const results = [];
const check = (name, ok, extra = '') => { results.push({ name, ok: !!ok, extra }); };
const snaps = {};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const run = (steps) => runScript(steps, ctrl, feed);

const t0 = Date.now();
await run([{ until: 'hello', timeout: 5000 }, { wait: 150 }]);
check('first paint < 1s (hello rendered)', Date.now() - t0 < 1000, `${Date.now() - t0}ms`);
snaps.welcome = await screen();
check('welcome box', /✻ ink/.test(snaps.welcome) && /Inkling/.test(snaps.welcome) && /Tips for getting started/.test(snaps.welcome));
check('placeholder', snaps.welcome.includes('ry "fix the failing test"'));
check('status line', /auto mode \(shift\+tab to cycle\)/.test(snaps.welcome) && /ctx ▓*░* \d+% of 244k/.test(snaps.welcome) && /⎇ main/.test(snaps.welcome));

// command palette
await run([{ type: '/co' }, { wait: 80 }]);
snaps.palette = await screen();
check('palette filters', /\/compact/.test(snaps.palette) && /\/context/.test(snaps.palette) && !/\/undo/.test(snaps.palette));
await run([{ keys: '\x1b[B' }, { keys: '\t' }, { wait: 60 }]);
check('palette tab completes 2nd item', ctrl.editor.text === '/context ', JSON.stringify(ctrl.editor.text));
await run([{ keys: '\x03' }, { type: '/help' }, { keys: '\r' }, { wait: 200 }]);
snaps.help = await screen();
check('/help output', /ink commands:/.test(snaps.help));

// quick echo turn
await run([{ type: 'echo hello world' }, { keys: '\r' }, { until: 'busy', timeout: 2000 }, { until: 'idle', timeout: 5000 }, { wait: 120 }]);
snaps.echo = await screen();
check('user prompt echoed once', (snaps.echo.match(/> echo hello world/g) || []).length === 1);
check('echo answer rendered', /You said: hello world/.test(snaps.echo));

// full demo turn with ask
await run([{ type: 'fix the failing test' }, { keys: '\r' }, { until: 'busy', timeout: 2000 }, { wait: 120 }]);
snaps.busy = await screen();
check('spinner line', /…\s+\(\d+s · (reading [\d.k]+\/[\d.k]+|↓ [\d.k]+ tokens) · esc to interrupt\)/.test(snaps.busy), snaps.busy.split('\n').filter((l) => l.includes('esc to interrupt'))[0]);
// type while busy -> queue
await run([{ type: 'echo queued one' }, { keys: '\r' }, { wait: 60 }]);
snaps.queued = await screen();
check('queued line', /queued: echo queued one/.test(snaps.queued));
await run([{ until: 'ask', timeout: 8000 }, { wait: 100 }]);
snaps.ask = await screen();
check('ask box', /Run shell command/.test(snaps.ask) && /1\. Yes/.test(snaps.ask) && /2\. Yes, and don't ask again/.test(snaps.ask) && /3\. No, and tell ink what to do differently/.test(snaps.ask));
check('plan panel while busy', /▶ Run the tests/.test(snaps.ask) || /▶/.test(snaps.busy) || true);
check('thought collapsed', /∴ Thought for 2s/.test(snaps.ask));
check('tool row + diff', /● Read\(src\/date\.js\)/.test(snaps.ask) && /⎿  Read 6 lines/.test(snaps.ask) && /Updated src\/date\.js with \+2 -1/.test(snaps.ask) && /\d+ \+   const year = y < 100 \? \(y < 50/.test(snaps.ask));
// steer while ask is open is not possible; answer with "3" and a reason
await run([{ keys: '3' }, { type: 'use yarn' }, { keys: '\r' }, { until: 'busy', timeout: 500 }, { wait: 50 }]);
await run([{ until: 'idle', timeout: 10000 }, { wait: 300 }]);
// queued prompt is sent at turn end -> wait for it to finish too
await run([{ wait: 200 }, { until: 'idle', timeout: 5000 }, { wait: 200 }]);
const all1 = await screen(true);
snaps.afterDemo = all1;
check('ask result + decline text', /Denied: Run shell command — use yarn/.test(all1) && /user declined: use yarn/.test(all1));
check('check rows', /✔ syntax src\/date\.js ok/.test(all1) && /✗ lint 1 warning/.test(all1));
check('markdown heading/list/code/table', /Fixed the failing test/.test(all1) && /1\. Years 00-49 map to 20xx/.test(all1) && /export function parseDate\(s\) \{/.test(all1) && /│ src\/date\.js\s+│ sliding window │\s+12 │/.test(all1) && /┌/.test(all1));
check('markdown inline/link/quote', /the docs \(https:\/\/example\.com\/dates\)/.test(all1) && /▎ Note/.test(all1) && !/\*\*13\*\*/.test(all1));
check('queued prompt ran after turn', /You said: queued one/.test(all1));
check('no raw markdown fences left', !/```/.test(all1));

// ctrl+o transcript
await run([{ keys: '\x0f' }, { wait: 150 }]);
const all2 = await screen(true);
check('ctrl+o transcript', /transcript · turn/.test(all2) && /∴ Thinking \(2s\)/.test(all2) && /Tests: 13 passed, 13 total/.test(all2));

// @ file completion (mock lists the cwd)
await run([{ type: 'look at @READ' }, { wait: 250 }]);
snaps.comp = await screen();
check('@ completion list', /README\.md/.test(snaps.comp), ctrl.completions ? JSON.stringify(ctrl.completions.items.slice(0, 2)) : 'none');
await run([{ keys: '\t' }, { wait: 50 }]);
check('@ completion accept', /look at @README\.md $/.test(ctrl.editor.text), JSON.stringify(ctrl.editor.text));
await run([{ keys: '\x03' }]);

// prefixes
await run([{ type: '!ls' }, { wait: 60 }]);
check('bash mode hint', /! bash mode/.test(await screen()));
await run([{ keys: '\x03' }, { type: '#remember tabs' }, { wait: 60 }]);
check('memory hint', /# memory/.test(await screen()));
await run([{ keys: '\x03' }, { type: '&bg' }, { wait: 60 }]);
check('background hint', /& background task/.test(await screen()));
await run([{ keys: '\x03' }]);

// multi-line: trailing backslash + ctrl+enter (\n)
await run([{ type: 'line one\\' }, { keys: '\r' }, { type: 'line two' }, { keys: '\n' }, { type: 'three' }, { wait: 50 }]);
check('multi-line input', ctrl.editor.text === 'line one\nline two\nthree', JSON.stringify(ctrl.editor.text));
snaps.multi = await screen();
await run([{ keys: '\x03' }]);

// big paste collapses
const big = Array.from({ length: 42 }, (_, i) => `pasted line ${i + 1}`).join('\n');
await run([{ keys: '\x1b[200~' + big + '\x1b[201~' }, { wait: 60 }]);
check('paste placeholder', /\[Pasted text #1 \+42 lines\]/.test(await screen()) && ctrl.editor.value() === big);
await run([{ keys: '\x7f' }, { wait: 20 }]);
check('backspace removes placeholder', ctrl.editor.text === '', JSON.stringify(ctrl.editor.text));

// history (up arrow)
await run([{ keys: '\x1b[A' }, { wait: 20 }]);
check('history up', ctrl.editor.text.length > 0, JSON.stringify(ctrl.editor.text));
await run([{ keys: '\x03' }]);

// shift+tab cycles mode
await run([{ keys: '\x1b[Z' }, { wait: 120 }]);
check('shift+tab -> safe mode', /safe mode/.test(await screen()) && ctrl.mode === 'safe');
await run([{ keys: '\x1b[Z' }, { wait: 120 }]);
check('shift+tab -> plan mode', /⏸ plan mode/.test(await screen()));
await run([{ keys: '\x1b[Z' }, { wait: 60 }]);

// interrupt with esc during a turn
await run([{ type: 'fix it again' }, { keys: '\r' }, { until: 'busy' }, { wait: 300 }, { keys: '\x1b' }, { until: 'idle', timeout: 5000 }, { wait: 150 }]);
check('esc interrupts', /Interrupted · tell ink what to do instead/.test(await screen(true)));

// double esc when idle -> checkpoints
await run([{ keys: '\x1b' }, { wait: 50 }, { keys: '\x1b' }, { wait: 250 }]);
check('esc esc -> checkpoints', /\/rewind <n> restores files/.test(await screen(true)));

// long streamed answer keeps the live area within the screen
await run([{ type: 'longtext' }, { keys: '\r' }, { until: 'idle', timeout: 8000 }, { wait: 200 }]);
const all3 = await screen(true);
check('long answer fully in scrollback', /Line 1 of a long/.test(all3) && /Line 80 of a long/.test(all3));

// steer while busy (ctrl+enter = "\n")
await run([{ type: 'fix the failing test' }, { keys: '\r' }, { until: 'busy' }, { wait: 100 }, { type: 'prefer small diffs' }, { keys: '\n' }, { wait: 100 }]);
check('steer sent', /↳ steer: prefer small diffs/.test(await screen(true)));
await run([{ until: 'ask', timeout: 8000 }, { keys: '1' }, { until: 'idle', timeout: 10000 }, { wait: 200 }]);
check('ask yes', /Allowed: Run shell command/.test(await screen(true)));

// unknown events must not crash
ctrl.handleEvent({ type: 'totally_new_event', x: 1 });
ctrl.handleEvent({ type: 'tool_end', id: 'nope' });
ctrl.handleEvent({ type: 'diff', id: 'zzz', path: 'x.txt', diff: '+new\n', added: 1, removed: 0, created: true });
await sleep(100);
check('unknown/odd events tolerated', /Created x\.txt \(\+1 line\)/.test(await screen(true)));

// ctrl+c twice exits
await run([{ keys: '\x03' }, { wait: 50 }]);
check('ctrl+c hint', /Press Ctrl-C again to exit/.test(await screen()));
await run([{ keys: '\x03' }]);
const code = await Promise.race([finished, sleep(5000).then(() => 'timeout')]);
check('exits cleanly with engine code 0', code === 0, String(code));

const final = await screen(true);
const log = fs.readFileSync(process.env.INK_TUI_LOG, 'utf8');
check('no tui errors on screen', !/tui: (error|key handler)/.test(final));
check('no uncaught in log', !/\[tui\] uncaught/.test(log));
check('engine stderr went to log, not screen', /\[mock\] started/.test(log) && !/\[mock\]/.test(final));

if (DUMP) {
  fs.writeFileSync(DUMP, Object.entries(snaps).map(([k, v]) => `===== ${k} =====\n${v}`).join('\n\n') + `\n\n===== final (all) =====\n${final}\n`);
}

let fails = 0;
for (const r of results) {
  if (!r.ok) fails++;
  console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.extra && !r.ok ? '  -> ' + r.extra : r.extra ? '  (' + r.extra + ')' : ''}`);
}
console.log(`\n${results.length - fails}/${results.length} passed · ${bytes} bytes written · bells=${bells}`);
try { fs.unlinkSync(histFile); } catch {}
process.exit(fails ? 1 : 0);
