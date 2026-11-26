// Spawns the ink engine in --rpc mode and speaks the JSON-lines protocol (see ../PROTOCOL.md).
import { spawn } from 'node:child_process';
import { EventEmitter } from 'node:events';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_PY = process.platform === 'win32' ? 'python' : 'python3';
const DEFAULT_ENGINE = path.resolve(HERE, '..', '..', 'ink.py');
export const MOCK_ENGINE = path.resolve(HERE, '..', 'mock-engine.mjs');

export function logPath() {
  return process.env.INK_TUI_LOG || path.join(os.homedir(), '.ink', 'tui.log');
}

export function engineCommand(args, { mock = false } = {}) {
  if (mock) return { cmd: process.execPath, argv: [MOCK_ENGINE, ...args] };
  const engine = process.env.INK_ENGINE || DEFAULT_ENGINE;
  if (/\.(m?js|cjs)$/i.test(engine)) return { cmd: process.execPath, argv: [engine, '--rpc', ...args] };
  const py = process.env.INK_PY || DEFAULT_PY;
  return { cmd: py, argv: ['-X', 'utf8', engine, '--rpc', ...args] };
}

export class Engine extends EventEmitter {
  constructor({ cmd, argv, cwd = process.cwd(), env = process.env }) {
    super();
    this.stderrTail = [];
    this.exited = false;
    this.code = null;
    try { fs.mkdirSync(path.dirname(logPath()), { recursive: true }); } catch {}
    try { this.log = fs.createWriteStream(logPath(), { flags: 'a' }); } catch { this.log = null; }
    this.logLine(`--- ${new Date().toISOString()} start: ${cmd} ${argv.join(' ')} (cwd ${cwd})`);
    this.child = spawn(cmd, argv, {
      cwd,
      env: { ...env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' },
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });
    this.child.on('error', (err) => {
      this.addStderr(`failed to start engine: ${err.message}`);
      this.exited = true;
      this.code = 127;
      this.emit('exit', 127);
    });
    let buf = '';
    this.child.stdout.setEncoding('utf8');
    this.child.stdout.on('data', (d) => {
      buf += d;
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl).replace(/\r$/, '');
        buf = buf.slice(nl + 1);
        if (!line.trim()) continue;
        let ev;
        try { ev = JSON.parse(line); } catch { this.logLine(`[stdout] ${line}`); continue; }
        if (ev && typeof ev.type === 'string') this.emit('event', ev);
      }
    });
    let ebuf = '';
    this.child.stderr.setEncoding('utf8');
    this.child.stderr.on('data', (d) => {
      ebuf += d;
      const parts = ebuf.split('\n');
      ebuf = parts.pop();
      for (const p of parts) this.addStderr(p.replace(/\r$/, ''));
    });
    this.child.on('exit', (code, signal) => {
      if (ebuf) this.addStderr(ebuf);
      if (this.exited) return;
      this.exited = true;
      this.code = code ?? (signal ? 1 : 0);
      this.logLine(`--- engine exited code=${code} signal=${signal}`);
      this.emit('exit', this.code);
    });
    this.child.stdin.on('error', () => {});
  }

  addStderr(line) {
    this.stderrTail.push(line);
    if (this.stderrTail.length > 40) this.stderrTail.shift();
    this.logLine(line);
  }

  logLine(s) {
    try { this.log?.write(s + '\n'); } catch {}
  }

  send(obj) {
    if (this.exited) return;
    try { this.child.stdin.write(JSON.stringify(obj) + '\n'); } catch {}
  }

  kill() {
    try { this.child.kill(); } catch {}
  }
}
