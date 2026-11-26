#!/usr/bin/env node
// Mock ink engine speaking PROTOCOL.md v1 over stdio - for developing/testing the TUI without a model.
//   prompts starting with "echo " get a quick one-paragraph answer; anything else plays a scripted demo turn
//   (progress, thinking, plan, tools, diff, ask, checks, markdown with code + table, stats).
// env MOCK_SPEED: time multiplier (0.1 = 10x faster). MOCK_EXIT_CODE: make /crash exit with this code.
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';

const SPEED = Number(process.env.MOCK_SPEED || 1);
const send = (type, o = {}) => process.stdout.write(JSON.stringify({ type, ...o }) + '\n');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms * SPEED));
const log = (s) => process.stderr.write(`[mock] ${s}\n`);

let mode = 'auto';
let busy = false;
let interrupted = false;
let turnId = 0;
const queue = [];
let pendingAsk = null;
let steers = [];
const args = process.argv.slice(2);

const COMMANDS = [
  { name: 'help', args: '', desc: 'Show help' },
  { name: 'clear', args: '', desc: 'Start a new conversation' },
  { name: 'compact', args: '[focus]', desc: 'Summarise the conversation to free context' },
  { name: 'context', args: '', desc: 'Show context usage' },
  { name: 'map', args: '', desc: 'Show the repo map' },
  { name: 'model', args: '<name>', desc: 'Switch model profile' },
  { name: 'checkpoints', args: '', desc: 'List file checkpoints' },
  { name: 'rewind', args: '[n]', desc: 'Restore files to a checkpoint' },
  { name: 'resume', args: '[id]', desc: 'Resume a previous session' },
  { name: 'undo', args: '[n]', desc: 'Undo the last edit(s)' },
  { name: 'exit', args: '', desc: 'Quit ink' },
];

const stats = (extra = {}) => send('stats', {
  tps: 19.4, pps: 812, ctx_used: 92800 + turnId * 4100, ctx: 244000, cache_hit: 0.93, secs: 12.3, steps: 3,
  model: 'Inkling', effort: 'low', mode, branch: 'main', jobs: turnId > 1 ? 2 : 0, ...extra,
});

log(`started with args: ${JSON.stringify(args)}`);
send('hello', {
  version: 1, model: 'Inkling', profile: 'smart', effort: 'low', ctx: 244000, cwd: process.cwd(), mode,
  session: 'mock-0001', resumed: 0, commands: COMMANDS,
  tips: ['Ask ink to explain or fix code: "why does test_login fail?"', 'Mention files with @path, run shell with !cmd', 'shift+tab cycles auto / safe / plan mode'],
});
stats({ tps: 0 });

async function stream(type, text, chunk = 7, ms = 15) {
  for (let i = 0; i < text.length; i += chunk) {
    if (interrupted) return false;
    send(type, { text: text.slice(i, i + chunk) });
    await sleep(ms);
  }
  return true;
}

function waitAnswer(id) {
  return new Promise((resolve) => { pendingAsk = { id, resolve }; });
}

const ANSWER = `## Fixed the failing test

The test failed because \`parseDate()\` treated **two-digit years** as *19xx*. I changed it to use a sliding window:

1. Years \`00-49\` map to 20xx
2. Years \`50-99\` map to 19xx
   - nested note: matches Python's \`%y\`

\`\`\`js
export function parseDate(s) {
  const [d, m, y] = s.split('/').map(Number);
  const year = y < 100 ? (y < 50 ? 2000 + y : 1900 + y) : y;
  return new Date(year, m - 1, d); // months are 0-based
}
\`\`\`

| file | change | tests |
|:-----|:------:|------:|
| src/date.js | sliding window | 12 |
| test/date.test.js | new case | 1 |

> Note: see [the docs](https://example.com/dates) for details.

All **13** tests pass now.
`;

const THINK = `The user wants the failing test fixed. Let me look at the test output first.
The failure is in date.test.js: expected 2024 but got 1924. So parseDate handles two-digit years wrong.
I should read src/date.js, find the year logic, and apply a sliding window like Python's strptime.
Then run the tests to confirm. Also check nothing else depends on the old behaviour.
Plan: read file, edit, run tests.`;

const DIFF = `@@ -1,6 +1,7 @@
 export function parseDate(s) {
   const [d, m, y] = s.split('/').map(Number);
-  const year = y < 100 ? 1900 + y : y;
+  const year = y < 100 ? (y < 50 ? 2000 + y : 1900 + y) : y;
+  // two-digit years use a sliding window (00-49 -> 20xx)
   return new Date(year, m - 1, d);
 }`;

async function demoTurn(text) {
  const plan = (doing) => send('plan', {
    goal: 'Fix the failing date test', key_points: ['two-digit years'],
    steps: ['Read src/date.js', 'Fix the year window', 'Run the tests'].map((step, i) => ({ step, status: i < doing ? 'done' : i === doing ? 'doing' : 'todo' })),
  });
  for (let d = 0; d <= 11400; d += 2850) { if (interrupted) return; send('progress', { done: d, total: 11400 }); await sleep(120); }
  if (!(await stream('thinking_delta', THINK, 9, 20))) return;
  send('thinking_end', { secs: 2.4, tokens: Math.round(THINK.length / 4) });
  plan(0);
  send('tool_start', { id: 't1', name: 'read_file', label: 'Read(src/date.js)', args: { path: 'src/date.js' } });
  await sleep(300);
  send('tool_end', { id: 't1', ok: true, summary: 'Read 6 lines', preview: ['export function parseDate(s) {', "  const [d, m, y] = s.split('/').map(Number);"], output: DIFF.replace(/^[-+ @].?/gm, '') });
  plan(1);
  if (interrupted) return;
  send('tool_start', { id: 't2', name: 'edit_file', label: 'Update(src/date.js)', args: { path: 'src/date.js' } });
  await sleep(200);
  send('diff', { id: 't2', path: 'src/date.js', diff: DIFF, added: 2, removed: 1, created: false });
  send('tool_end', { id: 't2', ok: true, summary: 'Updated src/date.js', preview: [], output: DIFF });
  stats();
  plan(2);
  if (mode !== 'auto' || process.env.MOCK_ASK !== '0') {
    send('ask', { id: 'a1', kind: 'run', title: 'Run shell command', detail: 'npm test -- date', options: ['yes', 'always', 'no'] });
    const ans = await waitAnswer('a1');
    if (interrupted) return;
    if (ans.choice === 'no') {
      send('notice', { level: 'warn', text: `user declined${ans.text ? ': ' + ans.text : ''}` });
    }
  }
  send('tool_start', { id: 't3', name: 'run', label: 'Bash(npm test -- date)', args: { cmd: 'npm test -- date' } });
  await sleep(400);
  send('tool_end', { id: 't3', ok: true, summary: '13 passed', preview: ['PASS test/date.test.js', '  ✓ parses 2-digit years (3 ms)'], output: 'PASS test/date.test.js\n  ✓ parses 4-digit years\n  ✓ parses 2-digit years (3 ms)\n\nTests: 13 passed, 13 total' });
  send('check', { name: 'syntax', ok: true, summary: 'src/date.js ok' });
  send('check', { name: 'lint', ok: false, summary: '1 warning: unused variable x' });
  plan(3);
  if (steers.length) { send('notice', { level: 'info', text: `steer applied: ${steers.join(' | ')}` }); steers = []; }
  stats();
  await stream('text_delta', ANSWER, 11, 12);
  send('text_end');
}

async function runTurn(text) {
  busy = true;
  interrupted = false;
  turnId++;
  const t0 = Date.now();
  send('turn_start', { id: turnId, text });
  send('status', { text: '' });
  if (text.startsWith('echo ')) {
    await sleep(100);
    await stream('text_delta', `You said: **${text.slice(5)}**`, 5, 10);
    send('text_end');
  } else if (text === 'longtext') {
    let s = '';
    for (let i = 1; i <= 80; i++) s += `Line ${i} of a long streamed answer without paragraph breaks.\n`;
    await stream('text_delta', s, 40, 5);
    send('text_end');
  } else {
    await demoTurn(text);
  }
  stats({ secs: (Date.now() - t0) / 1000 });
  send('turn_end', { secs: Math.round((Date.now() - t0) / 100) / 10, steps: 3, interrupted });
  busy = false;
  if (queue.length) runTurn(queue.shift());
}

function complete(msg) {
  if (msg.kind === 'file') {
    const pre = String(msg.prefix || '').replace(/\\/g, '/');
    const dir = pre.includes('/') ? pre.slice(0, pre.lastIndexOf('/') + 1) : '';
    const base = pre.slice(dir.length).toLowerCase();
    let items = [];
    try {
      items = fs.readdirSync(path.resolve(process.cwd(), dir || '.'), { withFileTypes: true })
        .filter((e) => e.name.toLowerCase().startsWith(base) && !e.name.startsWith('.'))
        .slice(0, 30)
        .map((e) => ({ value: dir + e.name + (e.isDirectory() ? '/' : ''), label: dir + e.name + (e.isDirectory() ? '/' : ''), desc: e.isDirectory() ? 'dir' : '' }));
    } catch {}
    send('completions', { req: msg.req, items });
  } else {
    send('completions', { req: msg.req, items: COMMANDS.filter((c) => c.name.startsWith(msg.prefix || '')).map((c) => ({ value: c.name, label: '/' + c.name, desc: c.desc })) });
  }
}

function command(name, arg) {
  switch (name) {
    case 'help': send('output', { text: 'ink commands:\n' + COMMANDS.map((c) => `  /${c.name.padEnd(12)} ${c.desc}`).join('\n') }); break;
    case 'checkpoints': case 'cp': send('output', { text: '  1. a1b2c3  2 min ago   Update(src/date.js)\n  2. d4e5f6  9 min ago   Write(README.md)\n\n/rewind <n> restores files' }); break;
    case 'resume': send('sessions', { items: [{ id: 'mock-0001', when: 'today 10:02', cwd: process.cwd(), title: 'fix date test', prompts: 3 }] }); break;
    case 'clear': send('notice', { level: 'info', text: 'new conversation' }); break;
    case 'crash': log('simulated crash: something went wrong'); process.exit(Number(process.env.MOCK_EXIT_CODE || 3));
    case 'exit': case 'quit': case 'q': send('bye'); process.exit(0);
    default: send('notice', { level: 'info', text: `mock: /${name}${arg ? ' ' + arg : ''}` });
  }
}

const rl = readline.createInterface({ input: process.stdin });
rl.on('line', (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { log(`bad json: ${line}`); return; }
  log(`<- ${msg.type}`);
  switch (msg.type) {
    case 'prompt': {
      const text = String(msg.text || '');
      if (text.startsWith('!')) { send('output', { text: `$ ${text.slice(1)}\n(mock shell output)` }); break; }
      if (text.startsWith('#')) { send('notice', { level: 'info', text: `saved memory: ${text.slice(1).trim()}` }); break; }
      if (busy) queue.push(text); else runTurn(text);
      break;
    }
    case 'steer': steers.push(msg.text); send('notice', { level: 'info', text: `steer queued: ${msg.text}` }); break;
    case 'interrupt':
      if (busy) { interrupted = true; if (pendingAsk) { pendingAsk.resolve({ choice: 'no' }); pendingAsk = null; } }
      break;
    case 'answer':
      if (pendingAsk && pendingAsk.id === msg.id) { const p = pendingAsk; pendingAsk = null; p.resolve(msg); }
      break;
    case 'command': command(String(msg.name || ''), String(msg.arg || '')); send('turn_end', { secs: 0, steps: 0, interrupted: false }); break;
    case 'mode': mode = ['auto', 'safe', 'plan'].includes(msg.mode) ? msg.mode : mode; stats({ tps: 0 }); send('notice', { level: 'info', text: `mode: ${mode}` }); break;
    case 'complete': complete(msg); break;
    case 'quit': send('bye'); setTimeout(() => process.exit(0), 20); break;
    default: break; // unknown: ignore
  }
});
rl.on('close', () => process.exit(0));
