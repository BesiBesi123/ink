#!/usr/bin/env node
// ink TUI - Claude Code-style terminal front-end for the ink engine (`ink.py --rpc`).
//   node tui/ink-tui.mjs [--mock] [any ink flags]
// env: INK_PY, INK_ENGINE, INK_THEME=light|dark, INK_TUI_SCRIPT=steps.json, INK_TUI_LOG
import { Engine, engineCommand } from './lib/engine.mjs';
import { startTui } from './lib/tui.mjs';
import { getTheme } from './lib/theme.mjs';

import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const argv = process.argv.slice(2);

// Not an interactive session (headless -p, --stop, --help, --plain) or no real terminal: run the Python engine
// directly with the classic interface, exactly like the old launcher did.
const DIRECT = ['-p', '--print', '--plain', '--stop', '--rpc', '--json', '-h', '--help', '--dream'];
if (argv.some((a) => DIRECT.includes(a)) || process.env.INK_PLAIN || (!process.env.INK_TUI_SCRIPT && (!process.stdout.isTTY || !process.stdin.isTTY))) {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const py = process.env.INK_PY || (process.platform === 'win32' ? 'python' : 'python3');
  const r = spawnSync(py, ['-X', 'utf8', path.join(here, '..', 'ink.py'), ...argv.filter((a) => a !== '--plain')],
    { stdio: 'inherit' });
  process.exit(r.status ?? (r.error ? 127 : 1));
}

const mock = argv.includes('--mock');
const args = argv.filter((a) => a !== '--mock');

if (argv.includes('--tui-help')) {
  process.stdout.write('usage: node ink-tui.mjs [--mock] [ink flags]\nSee tui/README.md for keys and env vars.\n');
  process.exit(0);
}

const { cmd, argv: engArgs } = engineCommand(args, { mock });
const engine = new Engine({ cmd, argv: engArgs, cwd: process.cwd() });

process.on('uncaughtException', (e) => {
  engine.logLine(`[tui] uncaught: ${e?.stack || e}`);
});
process.on('unhandledRejection', (e) => {
  engine.logLine(`[tui] unhandled rejection: ${e?.stack || e}`);
});
process.on('exit', () => { if (!engine.exited) engine.kill(); });

startTui({ engine, theme: getTheme(), script: process.env.INK_TUI_SCRIPT || null });
