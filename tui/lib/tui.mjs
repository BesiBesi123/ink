// Wires engine <-> controller <-> Ink renderer <-> raw stdin. Used by ink-tui.mjs and the tests.
import fs from 'node:fs';
import { render } from 'ink';
import { h } from '../components/h.mjs';
import { App } from '../components/App.mjs';
import { Controller } from './controller.mjs';
import { KeyParser } from './keys.mjs';
import { History } from './editor.mjs';
import { getTheme } from './theme.mjs';
import { logPath } from './engine.mjs';

export function startTui({ engine, stdin = process.stdin, stdout = process.stdout, theme = getTheme(), history, script, exit = true }) {
  let done;
  const finished = new Promise((r) => { done = r; });
  let ended = false;
  let quitTimer = null;

  const ctrl = new Controller({
    engine,
    stdout,
    history: history || new History(),
    onQuit: () => {
      quitTimer = setTimeout(() => engine.kill(), 3000);
      quitTimer.unref?.();
    },
    bell: () => { try { stdout.write('\x07'); } catch {} },
  });

  const ink = render(h(App, { ctrl, theme, stdout }), {
    stdin, stdout, exitOnCtrlC: false, patchConsole: false, kittyKeyboard: { mode: 'disabled' }, maxFps: 30,
  });

  // ---- raw input ----
  const parser = new KeyParser();
  const onData = (d) => { for (const k of parser.feed(d)) ctrl.handleKey(k); };
  if (stdin.isTTY && typeof stdin.setRawMode === 'function') { try { stdin.setRawMode(true); } catch {} }
  try { stdin.setEncoding?.('utf8'); } catch {}
  stdin.on('data', onData);
  stdin.resume?.();
  if (stdout.isTTY) stdout.write('\x1b[?2004h'); // bracketed paste

  engine.on('event', (ev) => ctrl.handleEvent(ev));

  const teardown = () => {
    stdin.off?.('data', onData);
    if (stdout.isTTY) { try { stdout.write('\x1b[?2004l'); } catch {} }
    try { if (stdin.isTTY && typeof stdin.setRawMode === 'function') stdin.setRawMode(false); } catch {}
    stdin.pause?.();
    ctrl.dispose();
  };

  engine.on('exit', async (code) => {
    if (ended) return;
    ended = true;
    clearTimeout(quitTimer);
    ctrl.notify(true);
    await new Promise((r) => setTimeout(r, 50));
    try { ink.unmount(); } catch {}
    try { await ink.waitUntilExit(); } catch {}
    teardown();
    const expected = ctrl.quitting || ctrl.byeSeen;
    if (!expected || code) {
      const tail = engine.stderrTail.slice(-15);
      const msg = [`\nink engine exited with code ${code}${expected ? '' : ' (unexpectedly)'}.`];
      if (tail.length) msg.push('Last log lines:', ...tail.map((l) => '  ' + l));
      msg.push(`(full log: ${logPath()})`);
      try { process.stderr.write(msg.join('\n') + '\n'); } catch {}
    }
    done(code ?? 0);
    if (exit) process.exit(code ?? 0);
  });

  if (script) runScript(script, ctrl, onData).catch((e) => ctrl.push({ kind: 'notice', level: 'error', text: 'script: ' + e.message }));

  return { ctrl, ink, finished };
}

// INK_TUI_SCRIPT: JSON array of steps, for non-interactive smoke tests.
//   {"wait": ms} | {"type": "text"} (char by char) | {"keys": "raw chunk"} |
//   {"until": "idle"|"busy"|"ask"|"hello", "timeout": ms} | {"quit": true}
export async function runScript(steps, ctrl, feed) {
  if (typeof steps === 'string') steps = JSON.parse(fs.readFileSync(steps, 'utf8'));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const cond = {
    idle: () => ctrl.hello && !ctrl.busy && !ctrl.ask,
    busy: () => ctrl.busy,
    ask: () => !!ctrl.ask,
    hello: () => !!ctrl.hello,
  };
  for (const s of steps) {
    if (s.wait) await sleep(s.wait);
    if (s.until) {
      const t0 = Date.now();
      while (!cond[s.until]?.() && Date.now() - t0 < (s.timeout || 15000)) await sleep(20);
    }
    if (s.type != null) for (const ch of String(s.type)) { feed(ch); await sleep(s.delay ?? 2); }
    if (s.keys != null) { feed(String(s.keys)); await sleep(s.delay ?? 10); }
    if (s.quit) ctrl.quit();
  }
}
