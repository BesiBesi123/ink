#!/usr/bin/env node
// playtest.mjs - headless-Chrome playtest for browser games, text report for a small local LLM.
// Usage: node playtest.mjs <config.json>
// Config: {page | url | file, steps:[...], seconds, size:"WxH" | width,height, root | serve_root, gpu, timeout}
// Node built-ins only (fetch/WebSocket from Node 22). Chrome renders with SwiftShader (CPU) so it never
// takes VRAM from the local LLM; the temp profile and every Chrome process are removed at exit.
import http from 'node:http';
import net from 'node:net';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import zlib from 'node:zlib';
import { spawn, spawnSync } from 'node:child_process';

const T0 = Date.now();
const MAX_REPORT = 4000;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const clip = (s, n) => { s = String(s); return s.length > n ? s.slice(0, n - 3) + '...' : s; };
const oneLine = (s) => String(s).replace(/\s+/g, ' ').trim();

// ------------------------------------------------------------------ config
function loadConfig() {
  const arg = process.argv[2];
  if (!arg) { console.log('usage: node playtest.mjs <config.json>'); process.exit(2); }
  let cfg;
  try { cfg = JSON.parse(fs.readFileSync(arg, 'utf8')); }
  catch (e) { console.log('PLAYTEST ERROR: bad config: ' + e.message); process.exit(2); }
  const base = cfg.cwd || process.cwd();
  let target = cfg.page || cfg.url || cfg.file;
  if (!target) { console.log('PLAYTEST ERROR: give "page" (an .html file path or http URL)'); process.exit(2); }
  target = String(target).trim();
  const isUrl = /^(https?|data|about):/i.test(target);
  let file = null, url = null;
  if (isUrl) url = target;
  else if (/^file:\/\//i.test(target)) file = decodeURIComponent(target.replace(/^file:\/\/\/?/i, ''));
  else file = path.resolve(base, target);
  if (file) {
    if (fs.existsSync(file) && fs.statSync(file).isDirectory()) file = path.join(file, 'index.html');
    if (!fs.existsSync(file)) { console.log('PLAYTEST ERROR: file not found: ' + file); process.exit(2); }
  }
  let width = Number(cfg.width) || 1280, height = Number(cfg.height) || 720;
  const m = /^(\d+)\s*[x*,]\s*(\d+)$/i.exec(String(cfg.size || ''));
  if (m) { width = +m[1]; height = +m[2]; }
  width = Math.max(200, Math.min(3840, width)); height = Math.max(150, Math.min(2160, height));
  const root = cfg.root || cfg.serve_root;
  return {
    url, file, width, height, base,
    root: file ? path.resolve(base, root || path.dirname(file)) : null,
    steps: Array.isArray(cfg.steps) ? cfg.steps : [],
    seconds: Math.max(0, Math.min(120, Number(cfg.seconds ?? 3))),
    gpu: !!cfg.gpu,
    timeout: Math.max(10, Math.min(600, Number(cfg.timeout) || 150)) * 1000,
    profileTag: String(cfg.profile_tag || '').replace(/[^\w-]/g, ''),
    ascii: !!cfg.ascii,
  };
}

// ------------------------------------------------------------------ static server
const MIME = {
  '.html': 'text/html; charset=utf-8', '.htm': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8', '.cjs': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8', '.map': 'application/json', '.wasm': 'application/wasm',
  '.glb': 'model/gltf-binary', '.gltf': 'model/gltf+json', '.bin': 'application/octet-stream',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp',
  '.svg': 'image/svg+xml', '.ico': 'image/x-icon', '.ktx2': 'image/ktx2', '.hdr': 'application/octet-stream',
  '.mp3': 'audio/mpeg', '.ogg': 'audio/ogg', '.wav': 'audio/wav', '.mp4': 'video/mp4', '.webm': 'video/webm',
  '.txt': 'text/plain; charset=utf-8', '.xml': 'application/xml', '.ttf': 'font/ttf', '.otf': 'font/otf',
  '.woff': 'font/woff', '.woff2': 'font/woff2', '.fbx': 'application/octet-stream', '.obj': 'text/plain',
};
function startStatic(root) {
  const ROOT = path.resolve(root);
  const srv = http.createServer((req, res) => {
    let p;
    try { p = decodeURIComponent(new URL(req.url, 'http://x').pathname); } catch { p = '/'; }
    let abs = path.resolve(ROOT, '.' + p.replace(/\\/g, '/'));
    if (abs !== ROOT && !abs.startsWith(ROOT + path.sep)) { res.writeHead(403); return res.end('forbidden'); }
    try { if (fs.statSync(abs).isDirectory()) abs = path.join(abs, 'index.html'); } catch {}
    fs.readFile(abs, (err, data) => {
      if (err) { res.writeHead(404, { 'content-type': 'text/plain' }); return res.end('not found'); }
      res.writeHead(200, { 'content-type': MIME[path.extname(abs).toLowerCase()] || 'application/octet-stream',
        'cache-control': 'no-store', 'access-control-allow-origin': '*' });
      res.end(data);
    });
  });
  return new Promise((resolve, reject) => {
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => resolve({ srv, port: srv.address().port }));
  });
}
function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.on('error', reject);
    s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); });
  });
}

// ------------------------------------------------------------------ chrome
function findChrome() {
  const c = [process.env.PLAYTEST_CHROME,
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'];
  return c.find((p) => p && fs.existsSync(p));
}

const state = { chrome: null, profile: null, server: null, ws: null, browserWs: null, cleaned: false };

async function cleanup() {
  if (state.cleaned) return;
  state.cleaned = true;
  try { state.ws && state.ws.close(); } catch {}
  // Graceful Browser.close first, then a tree kill so no renderer/GPU child is left behind.
  if (state.browserWs) {
    try {
      const bw = new WebSocket(state.browserWs);
      await Promise.race([new Promise((r) => { bw.onopen = r; bw.onerror = r; }), sleep(1000)]);
      if (bw.readyState === 1) bw.send(JSON.stringify({ id: 1, method: 'Browser.close' }));
      await sleep(300);
      try { bw.close(); } catch {}
    } catch {}
  }
  const ch = state.chrome;
  if (ch && ch.pid) {
    const exited = ch.exitCode !== null || ch.signalCode !== null;
    if (!exited) await Promise.race([new Promise((r) => ch.once('exit', r)), sleep(2000)]);
    if (process.platform === 'win32') spawnSync('taskkill', ['/PID', String(ch.pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true });
    else { try { process.kill(-ch.pid, 'SIGKILL'); } catch {} try { ch.kill('SIGKILL'); } catch {} }
  }
  if (state.profile && process.platform === 'win32') {
    // Backstop: any chrome.exe still holding our private profile dir (only ours - matched by its unique path).
    const tag = path.basename(state.profile).replace(/'/g, '');
    spawnSync('powershell', ['-NoProfile', '-Command',
      `Get-CimInstance Win32_Process -Filter "Name='chrome.exe' or Name='msedge.exe'" | Where-Object { $_.CommandLine -like '*${tag}*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }`],
      { stdio: 'ignore', windowsHide: true, timeout: 15000 });
  }
  if (state.server) { try { state.server.closeAllConnections?.(); state.server.close(); } catch {} }
  if (state.profile) {
    for (let i = 0; i < 10; i++) {
      try { fs.rmSync(state.profile, { recursive: true, force: true }); } catch {}
      if (!fs.existsSync(state.profile)) break;
      await sleep(300);
    }
  }
}

async function launchChrome(cfg) {
  const exe = findChrome();
  if (!exe) throw new Error('Chrome/Edge not found (set PLAYTEST_CHROME)');
  let port = await freePort();
  while (port === 9333) port = await freePort();
  state.profile = fs.mkdtempSync(path.join(os.tmpdir(), 'playtest-' + (cfg.profileTag ? cfg.profileTag + '-' : '')));
  const args = [
    '--headless=new', `--remote-debugging-port=${port}`, '--remote-debugging-address=127.0.0.1',
    `--user-data-dir=${state.profile}`, `--window-size=${cfg.width},${cfg.height}`,
    '--no-first-run', '--no-default-browser-check', '--disable-extensions', '--disable-sync',
    '--disable-background-networking', '--disable-component-update', '--disable-default-apps',
    '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
    '--disable-backgrounding-occluded-windows', '--mute-audio', '--autoplay-policy=no-user-gesture-required',
    '--no-proxy-server', '--password-store=basic', '--disable-features=Translate,MediaRouter,OptimizationHints',
  ];
  if (!cfg.gpu) args.push('--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--use-gl=angle', '--ignore-gpu-blocklist');
  args.push('about:blank');
  const ch = spawn(exe, args, { stdio: 'ignore', windowsHide: true, detached: process.platform !== 'win32' });
  state.chrome = ch;
  ch.on('error', () => {});
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    if (ch.exitCode !== null) throw new Error('Chrome exited at startup (code ' + ch.exitCode + ')');
    try {
      const v = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
      state.browserWs = v.webSocketDebuggerUrl;
      return { port, exe, version: v.Browser };
    } catch {}
    await sleep(150);
  }
  throw new Error('Chrome DevTools port did not come up in 20s');
}

// ------------------------------------------------------------------ CDP
function cdpConnect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  state.ws = ws;
  let seq = 0;
  const pending = new Map(), handlers = new Map();
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id) {
      const p = pending.get(m.id);
      if (p) { pending.delete(m.id); clearTimeout(p.t); m.error ? p.rej(new Error(m.error.message)) : p.res(m.result); }
    } else if (m.method) (handlers.get(m.method) || []).forEach((f) => { try { f(m.params); } catch {} });
  };
  ws.onclose = () => { for (const p of pending.values()) { clearTimeout(p.t); p.rej(new Error('CDP connection closed')); } pending.clear(); };
  const send = (method, params = {}, timeout = 15000) => new Promise((res, rej) => {
    if (ws.readyState !== 1) return rej(new Error('CDP not connected'));
    const id = ++seq;
    const t = setTimeout(() => { pending.delete(id); rej(new Error(`${method} timed out after ${timeout}ms`)); }, timeout);
    pending.set(id, { res, rej, t });
    ws.send(JSON.stringify({ id, method, params }));
  });
  const on = (ev, f) => { if (!handlers.has(ev)) handlers.set(ev, []); handlers.get(ev).push(f); };
  const ready = new Promise((r, j) => { ws.onopen = r; ws.onerror = () => j(new Error('CDP websocket failed')); });
  return { send, on, ready };
}

// Injected before any page script: rAF counter, frame gaps, long tasks, canvas context tracking.
const INJECT = `(() => {
  if (window.top !== window || window.__pt) return;
  const P = window.__pt = { frames: 0, last: 0, maxGap: 0, markT: 0, markF: 0, lt: 0, ltMax: 0, ltTotal: 0, ctx: [] };
  const tick = (t) => { if (P.last && P.markT && t - P.last > P.maxGap) P.maxGap = t - P.last; P.last = t; P.frames++; requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
  try { new PerformanceObserver((l) => { for (const e of l.getEntries()) { P.lt++; P.ltTotal += e.duration; if (e.duration > P.ltMax) P.ltMax = e.duration; } }).observe({ type: 'longtask', buffered: true }); } catch (e) {}
  const og = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (type, ...a) {
    const c = og.call(this, type, ...a);
    if (c && !this.__ptType) { this.__ptType = String(type); this.__ptCtx = c; P.ctx.push(this); }
    return c;
  };
})();`;

const SAFE_STRINGIFY = `function () {
  const seen = new WeakSet();
  const walk = (v, d) => {
    if (v === undefined) return 'undefined';
    if (typeof v === 'function') return 'fn ' + (v.name || 'anon') + '()';
    if (typeof v === 'bigint') return v.toString() + 'n';
    if (typeof v === 'number' && !Number.isFinite(v)) return String(v);
    if (typeof v === 'number') return Math.round(v * 1000) / 1000;
    if (v === null || typeof v !== 'object') return v;
    if (typeof Node !== 'undefined' && v instanceof Node) return '<' + (v.nodeName || 'node').toLowerCase() + (v.id ? '#' + v.id : '') + '>';
    if (seen.has(v)) return '[circular]';
    seen.add(v);
    if (d > 4) return Array.isArray(v) ? '[array ' + v.length + ']' : '[object]';
    if (Array.isArray(v) || ArrayBuffer.isView(v)) { const a = Array.from(v).slice(0, 30).map((x) => walk(x, d + 1)); if (v.length > 30) a.push('...+' + (v.length - 30)); return a; }
    if (v instanceof Map) return walk(Object.fromEntries(Array.from(v).slice(0, 30)), d);
    if (v instanceof Set) return walk(Array.from(v), d);
    if (v instanceof Error) return v.name + ': ' + v.message;
    const o = {}; let n = 0;
    for (const k in v) { if (++n > 40) { o['...'] = 'more keys'; break; } let x; try { x = v[k]; } catch (e) { x = '[throws]'; } if (typeof x === 'function') continue; o[k] = walk(x, d + 1); }
    return o;
  };
  try { return JSON.stringify(walk(this, 0)); } catch (e) { return String(this); }
}`;

// ------------------------------------------------------------------ keys
const KEYS = {
  arrowleft: ['ArrowLeft', 'ArrowLeft', 37], arrowup: ['ArrowUp', 'ArrowUp', 38], arrowright: ['ArrowRight', 'ArrowRight', 39],
  arrowdown: ['ArrowDown', 'ArrowDown', 40], left: ['ArrowLeft', 'ArrowLeft', 37], up: ['ArrowUp', 'ArrowUp', 38],
  right: ['ArrowRight', 'ArrowRight', 39], down: ['ArrowDown', 'ArrowDown', 40],
  space: [' ', 'Space', 32, ' '], ' ': [' ', 'Space', 32, ' '], spacebar: [' ', 'Space', 32, ' '],
  enter: ['Enter', 'Enter', 13, '\r'], return: ['Enter', 'Enter', 13, '\r'], escape: ['Escape', 'Escape', 27], esc: ['Escape', 'Escape', 27],
  shift: ['Shift', 'ShiftLeft', 16], control: ['Control', 'ControlLeft', 17], ctrl: ['Control', 'ControlLeft', 17],
  alt: ['Alt', 'AltLeft', 18], tab: ['Tab', 'Tab', 9], backspace: ['Backspace', 'Backspace', 8], delete: ['Delete', 'Delete', 46],
  home: ['Home', 'Home', 36], end: ['End', 'End', 35], pageup: ['PageUp', 'PageUp', 33], pagedown: ['PageDown', 'PageDown', 34],
  '-': ['-', 'Minus', 189, '-'], '=': ['=', 'Equal', 187, '='], ',': [',', 'Comma', 188, ','], '.': ['.', 'Period', 190, '.'],
  '/': ['/', 'Slash', 191, '/'], ';': [';', 'Semicolon', 186, ';'], "'": ["'", 'Quote', 222, "'"], '[': ['[', 'BracketLeft', 219, '['],
  ']': [']', 'BracketRight', 221, ']'], '`': ['`', 'Backquote', 192, '`'], '\\': ['\\', 'Backslash', 220, '\\'],
};
for (let i = 1; i <= 12; i++) KEYS['f' + i] = ['F' + i, 'F' + i, 111 + i];
const MODBIT = { Alt: 1, Control: 2, Meta: 4, Shift: 8 };
function keyInfo(name) {
  const s = String(name);
  if (/^[a-z]$/i.test(s)) { const u = s.toUpperCase(); return { key: s, code: 'Key' + u, vk: u.charCodeAt(0), text: s }; }
  if (/^key[a-z]$/i.test(s)) { const u = s[3].toUpperCase(); return { key: u.toLowerCase(), code: 'Key' + u, vk: u.charCodeAt(0), text: u.toLowerCase() }; }
  if (/^[0-9]$/.test(s)) return { key: s, code: 'Digit' + s, vk: 48 + +s, text: s };
  if (/^digit[0-9]$/i.test(s)) return { key: s[5], code: 'Digit' + s[5], vk: 48 + +s[5], text: s[5] };
  const k = KEYS[s.toLowerCase()] || KEYS[s];
  if (!k) return null;
  return { key: k[0], code: k[1], vk: k[2], text: k[3] };
}

// ------------------------------------------------------------------ PNG decode (for screen stats)
function decodePng(buf) {
  let p = 8, w = 0, h = 0, bitDepth = 0, colorType = 0; const idat = [];
  while (p < buf.length) {
    const len = buf.readUInt32BE(p), type = buf.toString('ascii', p + 4, p + 8), data = buf.subarray(p + 8, p + 8 + len);
    if (type === 'IHDR') { w = data.readUInt32BE(0); h = data.readUInt32BE(4); bitDepth = data[8]; colorType = data[9]; }
    else if (type === 'IDAT') idat.push(data);
    else if (type === 'IEND') break;
    p += 12 + len;
  }
  if (bitDepth !== 8 || (colorType !== 2 && colorType !== 6)) return null;
  const bpp = colorType === 6 ? 4 : 3, stride = w * bpp, raw = zlib.inflateSync(Buffer.concat(idat));
  const out = Buffer.alloc(h * stride); let prev = Buffer.alloc(stride);
  for (let y = 0; y < h; y++) {
    const f = raw[y * (stride + 1)], line = raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1)), cur = out.subarray(y * stride, (y + 1) * stride);
    for (let x = 0; x < stride; x++) {
      const a = x >= bpp ? cur[x - bpp] : 0, b = prev[x], c = x >= bpp ? prev[x - bpp] : 0;
      let v = line[x];
      if (f === 1) v += a; else if (f === 2) v += b; else if (f === 3) v += (a + b) >> 1;
      else if (f === 4) { const pp = a + b - c, pa = Math.abs(pp - a), pb = Math.abs(pp - b), pc = Math.abs(pp - c); v += pa <= pb && pa <= pc ? a : pb <= pc ? b : c; }
      cur[x] = v & 255;
    }
    prev = cur;
  }
  return { w, h, bpp, px: out };
}
function screenStats(img) {
  if (!img) return null;
  const { w, h, bpp, px } = img, counts = new Map(), step = Math.max(1, Math.floor(Math.sqrt((w * h) / 40000)));
  let n = 0, lum = 0;
  for (let y = 0; y < h; y += step) for (let x = 0; x < w; x += step) {
    const i = (y * w + x) * bpp, r = px[i], g = px[i + 1], b = px[i + 2];
    const q = ((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4);
    counts.set(q, (counts.get(q) || 0) + 1); n++; lum += 0.299 * r + 0.587 * g + 0.114 * b;
  }
  let top = 0, topQ = 0; for (const [q, c] of counts) if (c > top) { top = c; topQ = q; }
  const hex = '#' + [topQ >> 8, (topQ >> 4) & 15, topQ & 15].map((v) => (v * 17).toString(16).padStart(2, '0')).join('');
  return { colors: counts.size, domPct: Math.round((100 * top) / n), domHex: hex, lum: Math.round(lum / n) };
}
function diffPct(a, b) {
  if (!a || !b || a.w !== b.w || a.h !== b.h) return null;
  let n = 0, d = 0; const step = Math.max(1, Math.floor(Math.sqrt((a.w * a.h) / 40000)));
  for (let y = 0; y < a.h; y += step) for (let x = 0; x < a.w; x += step) {
    const i = (y * a.w + x) * a.bpp, j = (y * b.w + x) * b.bpp; n++;
    if (Math.abs(a.px[i] - b.px[j]) + Math.abs(a.px[i + 1] - b.px[j + 1]) + Math.abs(a.px[i + 2] - b.px[j + 2]) > 24) d++;
  }
  return Math.round((1000 * d) / n) / 10;
}
function asciiThumb(img, cols = 48, rows = 14) {
  const ramp = ' .:-=+*#%@', out = [];
  for (let r = 0; r < rows; r++) {
    let line = '';
    for (let c = 0; c < cols; c++) {
      const x = Math.floor(((c + 0.5) * img.w) / cols), y = Math.floor(((r + 0.5) * img.h) / rows), i = (y * img.w + x) * img.bpp;
      const l = 0.299 * img.px[i] + 0.587 * img.px[i + 1] + 0.114 * img.px[i + 2];
      line += ramp[Math.min(9, Math.floor(l / 25.6))];
    }
    out.push(line.replace(/\s+$/, ''));
  }
  return out;
}

// ------------------------------------------------------------------ main run
async function run(cfg) {
  const R = { exceptions: new Map(), errors: new Map(), warnings: new Map(), logs: new Map(), net: [], steps: [], notes: [], fatalNet: 0 };
  let origin = '';
  const short = (u) => {
    if (!u) return '';
    if (origin && u.startsWith(origin)) u = u.slice(origin.length).replace(/^\//, '');
    return u.replace(/\?.*$/, '');
  };
  const loc = (url, line, col) => url ? `${short(url)}:${line + 1}${col != null ? ':' + (col + 1) : ''}` : '';
  const bump = (map, key, val) => { const e = map.get(key); if (e) { e.n++; e.last = Date.now(); } else map.set(key, { ...val, n: 1, first: Date.now() - T0 }); };

  if (cfg.file) {
    const { srv, port } = await startStatic(cfg.root);
    state.server = srv;
    const rel = path.relative(cfg.root, cfg.file).split(path.sep).map(encodeURIComponent).join('/');
    if (rel.startsWith('..')) throw new Error('file is outside root: ' + cfg.root);
    cfg.url = `http://127.0.0.1:${port}/${rel}`;
  }
  try { origin = new URL(cfg.url).origin + '/'; } catch {}

  const chrome = await launchChrome(cfg);
  const tab = await (await fetch(`http://127.0.0.1:${chrome.port}/json/new?about:blank`, { method: 'PUT' })).json();
  const cdp = cdpConnect(tab.webSocketDebuggerUrl);
  await cdp.ready;
  const { send, on } = cdp;

  const reqs = new Map();
  on('Runtime.consoleAPICalled', (p) => {
    const prev = (pv) => pv.subtype === 'array' ? '[' + (pv.properties || []).map((x) => x.value).join(',') + (pv.overflow ? ',...' : '') + ']'
      : '{' + (pv.properties || []).map((x) => x.name + ':' + (x.type === 'string' ? JSON.stringify(x.value) : x.value)).join(',') + (pv.overflow ? ',...' : '') + '}';
    const text = oneLine((p.args || []).map((a) => a.value !== undefined ? (typeof a.value === 'string' ? a.value : JSON.stringify(a.value))
      : a.unserializableValue || (a.preview && a.type === 'object' && a.subtype !== 'error' ? prev(a.preview) : a.description || a.type)).join(' '));
    const f = p.stackTrace && p.stackTrace.callFrames && p.stackTrace.callFrames[0];
    const where = f ? loc(f.url, f.lineNumber, f.columnNumber) : '';
    if (p.type === 'error' || p.type === 'assert') bump(R.errors, text + '|' + where, { text, where });
    else if (p.type === 'warning') { if (/GPU stall due to ReadPixels/.test(text)) return; bump(R.warnings, text, { text, where }); }
    else bump(R.logs, text, { text });
  });
  on('Runtime.exceptionThrown', (p) => {
    const d = p.exceptionDetails || {}, ex = d.exception || {};
    let msg = ex.description ? ex.description.split('\n')[0] : (ex.value !== undefined ? String(ex.value) : d.text);
    const promise = /in promise/i.test(d.text || '');
    if (promise && !/^Uncaught/.test(msg)) msg = 'Unhandled rejection: ' + msg;
    const frames = ((d.stackTrace && d.stackTrace.callFrames) || []).slice(0, 4).map((f) => `${f.functionName || '(anon)'} ${loc(f.url, f.lineNumber, f.columnNumber)}`);
    if (!frames.length && d.url) frames.push(loc(d.url, d.lineNumber, d.columnNumber));
    bump(R.exceptions, oneLine(msg) + '|' + (frames[0] || ''), { msg: oneLine(msg), frames });
  });
  on('Log.entryAdded', ({ entry: e }) => {
    if (e.source === 'network' || e.source === 'violation') return; // network covered by Network domain
    const where = e.url ? loc(e.url, e.lineNumber || 0) : '';
    if (e.level === 'error') bump(R.errors, e.text + '|' + where, { text: oneLine(e.text), where });
    else if (e.level === 'warning' && !/GPU stall due to ReadPixels/.test(e.text)) bump(R.warnings, e.text, { text: oneLine(e.text), where });
  });
  on('Network.requestWillBeSent', (p) => reqs.set(p.requestId, { url: p.request.url, method: p.request.method, type: p.type }));
  const netFail = (rq, why) => {
    if (!rq || /\/favicon\.ico$/.test(rq.url) || /^data:/.test(rq.url)) return;
    const fatal = ['Document', 'Script', 'Stylesheet'].includes(rq.type);
    if (fatal) R.fatalNet++;
    R.net.push(`${why} ${rq.method} ${short(rq.url)} (${rq.type || '?'})${rq.type === 'Script' ? ' -> modules importing it never ran' : ''}`);
  };
  on('Network.responseReceived', (p) => { if (p.response.status >= 400) netFail(reqs.get(p.requestId) || { url: p.response.url, method: 'GET', type: p.type }, String(p.response.status)); });
  on('Network.loadingFailed', (p) => { if (!p.canceled) netFail(reqs.get(p.requestId), p.blockedReason ? 'blocked:' + p.blockedReason : p.errorText); });
  let crashed = false;
  on('Inspector.targetCrashed', () => { crashed = true; });
  on('Page.javascriptDialogOpening', (p) => {
    R.notes.push(`${p.type} dialog: "${clip(oneLine(p.message), 100)}" (auto-accepted)`);
    send('Page.handleJavaScriptDialog', { accept: true, promptText: '' }).catch(() => {});
  });
  let loadAt = 0, dclAt = 0;
  on('Page.loadEventFired', () => { if (!loadAt) loadAt = Date.now(); });
  on('Page.domContentEventFired', () => { if (!dclAt) dclAt = Date.now(); });

  await Promise.all(['Page.enable', 'Runtime.enable', 'Log.enable', 'Network.enable', 'Inspector.enable'].map((m) => send(m)));
  await send('Network.setCacheDisabled', { cacheDisabled: true });
  await send('Emulation.setDeviceMetricsOverride', { width: cfg.width, height: cfg.height, deviceScaleFactor: 1, mobile: false });
  await send('Emulation.setFocusEmulationEnabled', { enabled: true }).catch(() => {});
  await send('Page.addScriptToEvaluateOnNewDocument', { source: INJECT });
  await send('Page.bringToFront').catch(() => {});

  const navAt = Date.now();
  const nav = await send('Page.navigate', { url: cfg.url }, 30000);
  if (nav.errorText) throw new Error(`navigation failed: ${nav.errorText} (${cfg.url})`);
  const loadDeadline = Date.now() + 30000;
  while (!loadAt && Date.now() < loadDeadline && !crashed) await sleep(50);
  if (!loadAt) R.notes.push('load event did not fire within 30s');
  await sleep(300);

  const evaluate = async (expr, timeout = 10000) => {
    const r = await send('Runtime.evaluate', { expression: String(expr), awaitPromise: true, replMode: true, userGesture: true, timeout }, timeout + 2000);
    if (r.exceptionDetails) {
      const d = r.exceptionDetails, ex = d.exception || {};
      return { error: oneLine((ex.description || d.text || 'error').split('\n')[0]) };
    }
    const o = r.result || {};
    let shown, truthy;
    if (o.type === 'undefined') { shown = 'undefined'; truthy = false; }
    else if ('value' in o) { shown = typeof o.value === 'string' ? JSON.stringify(o.value) : JSON.stringify(o.value); truthy = !!o.value; }
    else if (o.unserializableValue) { shown = o.unserializableValue; truthy = !/^(-?0|NaN|0n)$/.test(o.unserializableValue); }
    else if (o.objectId) {
      truthy = true;
      try {
        const s = await send('Runtime.callFunctionOn', { objectId: o.objectId, functionDeclaration: SAFE_STRINGIFY, returnByValue: true });
        shown = s.result && s.result.value;
      } catch { shown = o.description; }
      if (o.subtype === 'null') truthy = false;
    } else { shown = o.description || o.type; truthy = o.subtype !== 'null'; }
    return { shown: String(shown), truthy };
  };
  const ev = async (expr) => { const r = await evaluate(expr); return r.error ? undefined : JSON.parse(r.shown === 'undefined' ? 'null' : r.shown); };
  const shot = async () => { try { const s = await send('Page.captureScreenshot', { format: 'png' }, 20000); return Buffer.from(s.data, 'base64'); } catch { return null; } };

  let firstImg = null;
  try { const b = await shot(); firstImg = b && decodePng(b); } catch {}
  await ev('window.__pt && (__pt.markT = performance.now(), __pt.markF = __pt.frames, __pt.maxGap = 0, 1)').catch(() => {});
  const runStart = Date.now();

  const mouse = (type, x, y, extra = {}) => send('Input.dispatchMouseEvent', { type, x, y, button: 'left', ...extra });
  const keyEv = (type, k, mods) => send('Input.dispatchKeyEvent', {
    type, key: k.key, code: k.code, windowsVirtualKeyCode: k.vk, nativeVirtualKeyCode: k.vk, modifiers: mods,
    ...(type === 'keyDown' && k.text && !(mods & 2) ? { text: mods & 8 && k.text.length === 1 ? k.text.toUpperCase() : k.text, unmodifiedText: k.text } : {}),
    ...(k.code.startsWith('Arrow') || k.code === 'Home' || k.code === 'End' ? { location: 0 } : {}),
  });
  const pointOf = async (target) => {
    if (Array.isArray(target)) return { x: +target[0], y: +target[1] };
    const r = await evaluate(`(() => { const el = document.querySelector(${JSON.stringify(String(target))}); if (!el) return null; el.scrollIntoView({block:'center',inline:'center'}); const b = el.getBoundingClientRect(); return {x: Math.round(b.x + b.width/2), y: Math.round(b.y + b.height/2)}; })()`);
    if (r.error) throw new Error(r.error);
    const p = JSON.parse(r.shown);
    if (!p) throw new Error('selector not found: ' + target);
    return p;
  };

  let stepFail = 0, expectPass = 0, expectTotal = 0;
  for (let i = 0; i < cfg.steps.length; i++) {
    if (crashed) { R.steps.push(`${i + 1}. SKIP (page crashed)`); continue; }
    if (Date.now() - T0 > cfg.timeout - 15000) { R.steps.push(`${i + 1}+. SKIP (overall timeout near)`); stepFail++; break; }
    const s = cfg.steps[i] || {};
    const n = `${i + 1}.`;
    const errsBefore = R.exceptions.size;
    try {
      if (typeof s !== 'object' || Array.isArray(s)) throw new Error('step must be an object like {"wait":500}');
      if ('wait' in s && Object.keys(s).length === 1) {
        const ms = Math.min(60000, Math.max(0, +s.wait || 0)); await sleep(ms); R.steps.push(`${n} wait ${ms}ms`);
      } else if ('key' in s || 'keys' in s) {
        const names = [].concat(s.key ?? s.keys);
        const ks = names.map((k) => { const i2 = keyInfo(k); if (!i2) throw new Error('unknown key ' + JSON.stringify(k)); return i2; });
        const ms = Math.min(30000, Math.max(0, +(s.ms ?? s.hold ?? 100)));
        let mods = 0;
        for (const k of ks) { if (MODBIT[k.key]) mods |= MODBIT[k.key]; await keyEv('keyDown', k, mods); }
        await sleep(ms);
        for (const k of ks.slice().reverse()) { await keyEv('keyUp', k, mods); if (MODBIT[k.key]) mods &= ~MODBIT[k.key]; }
        R.steps.push(`${n} key ${names.join('+')} ${ms}ms`);
      } else if ('type' in s) {
        const text = String(s.type);
        for (const ch of text) {
          const k = keyInfo(ch) || (ch === ' ' ? keyInfo('Space') : ch === '\n' ? keyInfo('Enter') : null);
          if (k && (/^[a-z0-9 \n]$/i.test(ch))) {
            const up = /^[A-Z]$/.test(ch), kk = { ...k, key: up ? ch : k.key, text: ch === '\n' ? '\r' : ch };
            await keyEv('keyDown', kk, up ? 8 : 0); await keyEv('keyUp', kk, up ? 8 : 0);
          } else await send('Input.insertText', { text: ch });
          await sleep(15);
        }
        R.steps.push(`${n} type ${JSON.stringify(clip(text, 40))}`);
      } else if ('click' in s) {
        const p = await pointOf(s.click);
        await mouse('mouseMoved', p.x, p.y, { button: 'none' });
        await mouse('mousePressed', p.x, p.y, { clickCount: 1, buttons: 1 });
        await sleep(40);
        await mouse('mouseReleased', p.x, p.y, { clickCount: 1, buttons: 0 });
        R.steps.push(`${n} click ${Array.isArray(s.click) ? `${p.x},${p.y}` : JSON.stringify(s.click) + ` @${p.x},${p.y}`}`);
      } else if ('drag' in s) {
        const d = s.drag;
        if (!Array.isArray(d) || d.length < 4) throw new Error('drag needs [x1,y1,x2,y2]');
        const [x1, y1, x2, y2] = d.map(Number), ms = Math.min(10000, +(s.ms ?? 300)), N = Math.max(5, Math.round(ms / 20));
        await mouse('mouseMoved', x1, y1, { button: 'none' });
        await mouse('mousePressed', x1, y1, { clickCount: 1, buttons: 1 });
        for (let k = 1; k <= N; k++) { await mouse('mouseMoved', x1 + ((x2 - x1) * k) / N, y1 + ((y2 - y1) * k) / N, { buttons: 1 }); await sleep(ms / N); }
        await mouse('mouseReleased', x2, y2, { clickCount: 1, buttons: 0 });
        R.steps.push(`${n} drag ${x1},${y1}->${x2},${y2}`);
      } else if ('eval' in s) {
        const r = await evaluate(s.eval);
        if (r.error) { stepFail++; R.steps.push(`${n} eval ERROR ${clip(r.error, 200)}`); }
        else R.steps.push(`${n} eval ${clip(oneLine(s.eval), 60)} => ${clip(r.shown, 300)}`);
      } else if ('expect' in s) {
        expectTotal++;
        const label = s.label ? String(s.label) : clip(oneLine(s.expect), 80);
        const r = await evaluate(s.expect);
        if (r.error) { stepFail++; R.steps.push(`${n} FAIL ${label}: threw ${clip(r.error, 200)}`); }
        else if (r.truthy) { expectPass++; R.steps.push(`${n} PASS ${label}`); }
        else { stepFail++; R.steps.push(`${n} FAIL ${label} (got ${clip(r.shown, 200)})`); }
      } else if ('wait_for' in s) {
        const to = Math.min(60000, Math.max(100, +(s.timeout ?? 5000))), t1 = Date.now();
        let r = { truthy: false, shown: '' };
        while (Date.now() - t1 < to) { r = await evaluate(s.wait_for, 5000); if (r.truthy) break; await sleep(100); }
        if (r.truthy) R.steps.push(`${n} wait_for ok after ${Date.now() - t1}ms: ${clip(oneLine(s.wait_for), 60)}`);
        else { stepFail++; R.steps.push(`${n} FAIL wait_for timed out ${to}ms: ${clip(oneLine(s.wait_for), 60)} (last ${clip(r.error || r.shown, 120)})`); }
      } else if ('screenshot' in s) {
        const b = await shot();
        if (!b) throw new Error('screenshot failed');
        let f = String(s.screenshot || 'playtest.png'); if (!/\.png$/i.test(f)) f += '.png';
        f = path.resolve(cfg.base, f);
        fs.mkdirSync(path.dirname(f), { recursive: true }); fs.writeFileSync(f, b);
        const st = screenStats(decodePng(b));
        R.steps.push(`${n} screenshot ${f}${st ? ` (${st.colors} colors, ${st.domPct}% ${st.domHex})` : ''}`);
      } else throw new Error('unknown step ' + clip(JSON.stringify(s), 80) + ' (use wait,key,type,click,drag,eval,expect,wait_for,screenshot)');
    } catch (e) { stepFail++; R.steps.push(`${n} ERROR ${clip(e.message, 200)}`); }
    if (R.exceptions.size > errsBefore) R.steps[R.steps.length - 1] += ' [new exception]';
  }

  const remain = cfg.seconds * 1000 - (Date.now() - runStart);
  if (remain > 0) {
    const budget = Math.max(0, cfg.timeout - (Date.now() - T0) - 15000);
    if (remain > budget) R.notes.push(`run shortened to fit the ${Math.round(cfg.timeout / 1000)}s timeout`);
    await sleep(Math.min(remain, budget));
  }

  // ---- final probes
  let perf = null, canv = [], title = '', bodyText = '';
  if (!crashed) {
    perf = await ev(`(() => { const P = window.__pt; if (!P) return null; const t = performance.now(), dt = (t - P.markT) / 1000;
      return { fps: dt > 0 ? (P.frames - P.markF) / dt : 0, dt, maxGap: P.maxGap, lt: P.lt, ltMax: P.ltMax, ltTotal: P.ltTotal }; })()`).catch(() => null);
    canv = await ev(`(() => { const P = window.__pt || { ctx: [] }; const all = new Set([...document.querySelectorAll('canvas'), ...P.ctx]);
      return [...all].slice(0, 6).map((c) => { const r = c.getBoundingClientRect(); const o = { t: c.__ptType || 'none', w: c.width, h: c.height, cw: Math.round(r.width), ch: Math.round(r.height), vis: r.width > 0 && r.height > 0 && getComputedStyle(c).visibility !== 'hidden' && c.isConnected, id: c.id || '' };
        const g = c.__ptCtx; if (g && /webgl/.test(o.t)) { try { o.lost = g.isContextLost(); const e = g.getExtension('WEBGL_debug_renderer_info'); o.r = e ? g.getParameter(e.UNMASKED_RENDERER_WEBGL) : g.getParameter(g.RENDERER); } catch (e) {} }
        return o; }); })()`).catch(() => []) || [];
    title = (await ev('document.title').catch(() => '')) || '';
    bodyText = (await ev(`(document.body && document.body.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 200)`).catch(() => '')) || '';
  }
  let lastImg = null;
  if (!crashed) { const b = await shot(); lastImg = b && decodePng(b); }

  // ---- report
  const exc = [...R.exceptions.values()], errs = [...R.errors.values()], warns = [...R.warnings.values()], logs = [...R.logs.values()];
  const failed = exc.length > 0 || errs.length > 0 || stepFail > 0 || crashed || R.fatalNet > 0;
  const loadMs = loadAt ? loadAt - navAt : null;
  const fps = perf && perf.dt >= 0.5 ? perf.fps : null;
  const head = [];
  head.push(`${failed ? 'FAIL' : 'PASS'} ${cfg.url} | load ${loadMs != null ? loadMs + 'ms' : 'n/a'} | fps ~${fps != null ? Math.round(fps) : '?'}${cfg.gpu ? '' : ' (cpu-rendered, relative only)'} | ran ${((Date.now() - runStart) / 1000).toFixed(1)}s`);
  const counts = [`exceptions ${exc.reduce((a, e) => a + e.n, 0)}`, `console errors ${errs.reduce((a, e) => a + e.n, 0)}`, `warnings ${warns.reduce((a, e) => a + e.n, 0)}`, `net fails ${R.net.length}`];
  if (expectTotal) counts.push(`expects ${expectPass}/${expectTotal} passed`);
  if (stepFail - (expectTotal - expectPass) > 0) counts.push(`step errors ${stepFail - (expectTotal - expectPass)}`);
  head.push(counts.join(', '));
  if (cfg.file) head.push(`served ${cfg.root} (paths below are relative to it)`);
  const info = [];
  if (title) info.push(`title "${clip(title, 60)}"`);
  if (perf) info.push(`worst frame gap ${Math.round(perf.maxGap)}ms` + (perf.lt ? `, long tasks ${perf.lt} (max ${Math.round(perf.ltMax)}ms, total ${Math.round(perf.ltTotal)}ms)` : ', no long tasks'));
  if (info.length) head.push(info.join(' | '));
  if (canv.length) head.push('canvas: ' + canv.map((c) => `${c.id ? '#' + c.id + ' ' : ''}${c.t} ${c.w}x${c.h}${c.cw !== c.w || c.ch !== c.h ? ` (css ${c.cw}x${c.ch})` : ''}${c.vis ? '' : ' HIDDEN'}${c.lost ? ' CONTEXT LOST' : ''}${c.r ? ' [' + (/swiftshader/i.test(c.r) ? 'SwiftShader' : clip(c.r, 50)) + ']' : ''}`).join('; '));
  else if (!crashed) head.push('canvas: none found');
  const st = screenStats(lastImg);
  if (st) {
    const d = diffPct(firstImg, lastImg);
    head.push(`screen: ${st.colors} colors, ${st.domPct}% is ${st.domHex}, brightness ${st.lum}/255${st.domPct >= 97 ? ' -> LOOKS BLANK' : ''}${d != null ? `, ${d}% pixels changed since load` : ''}`);
  }
  if (bodyText && bodyText.length > 0) head.push(`page text: "${clip(bodyText, 160)}"`);
  if (crashed) head.push('PAGE CRASHED (renderer died)');

  const sec = [];
  const cnt = (e) => (e.n > 1 ? ` (x${e.n})` : '');
  if (exc.length) sec.push({ name: 'Exceptions', pri: 1, lines: exc.map((e) => `- ${clip(e.msg, 200)}${cnt(e)}\n    at ${e.frames.slice(0, 3).join(' < ') || '?'}`) });
  if (errs.length) sec.push({ name: 'Console errors', pri: 2, lines: errs.map((e) => `- ${clip(e.text, 240)}${e.where ? ' @' + e.where : ''}${cnt(e)}`) });
  if (R.net.length) {
    const nm = new Map(); for (const x of R.net) nm.set(x, (nm.get(x) || 0) + 1);
    sec.push({ name: 'Network failures', pri: 3, lines: [...nm].map(([x, c]) => `- ${clip(x, 200)}${c > 1 ? ` (x${c})` : ''}`) });
  }
  if (R.steps.length) sec.push({ name: 'Steps', pri: 1, lines: R.steps.map((s) => clip(s, 360)) });
  if (R.notes.length) sec.push({ name: 'Notes', pri: 3, lines: R.notes.map((s) => '- ' + s) });
  if (warns.length) sec.push({ name: 'Console warnings', pri: 4, lines: warns.map((e) => `- ${clip(e.text, 180)}${e.where ? ' @' + e.where : ''}${cnt(e)}`) });
  if (logs.length) sec.push({ name: `Logs (last ${Math.min(12, logs.length)} of ${logs.length} distinct)`, pri: 5, lines: logs.sort((a, b) => a.first - b.first).slice(-12).map((e) => `- ${clip(e.text, 140)}${cnt(e)}`) });
  if (cfg.ascii && lastImg) sec.push({ name: 'Screen (ascii, dark=space)', pri: 6, lines: asciiThumb(lastImg) });

  const CAP = { Exceptions: 8, 'Console errors': 10, 'Network failures': 10, 'Console warnings': 6, Notes: 6, Steps: 60 };
  for (const s of sec) { const c = CAP[s.name]; if (c && s.lines.length > c) { s.cut = s.lines.length - c; s.lines = s.name === 'Steps' ? s.lines.slice(0, c) : s.lines.slice(0, c); } }
  const render = () => [head.join('\n'), ...sec.filter((s) => s.lines.length).map((s) => `== ${s.name} ==\n` + s.lines.join('\n') + (s.cut ? `\n(+${s.cut} more)` : ''))].join('\n');
  let out = render();
  // Smart truncation: drop lines from the least important sections first (and keep at least 2 lines of each).
  while (out.length > MAX_REPORT) {
    const cands = sec.filter((s) => s.lines.length > (s.pri <= 2 ? 3 : 1)).sort((a, b) => b.pri - a.pri || b.lines.length - a.lines.length);
    if (!cands.length) { out = out.slice(0, MAX_REPORT - 20) + '\n(report truncated)'; break; }
    const s = cands[0];
    if (s.name === 'Steps') { const k = s.lines.findIndex((l) => !/FAIL|ERROR|exception/.test(l)); s.lines.splice(k >= 0 ? k : 1, 1); }
    else s.lines.splice(s.name.startsWith('Logs') ? 0 : s.lines.length - 1, 1);
    s.cut = (s.cut || 0) + 1;
    out = render();
  }
  return { text: out, code: failed ? 1 : 0 };
}

// ------------------------------------------------------------------ entry
const cfg = loadConfig();
let finished = false;
const hard = setTimeout(async () => {
  if (finished) return;
  finished = true;
  console.log(`FAIL playtest hard timeout after ${Math.round(cfg.timeout / 1000)}s (page hung or steps too long)`);
  await cleanup(); process.exit(1);
}, cfg.timeout);
// With PLAYTEST_WATCH_STDIN=1 (set by playtest.py): when the parent closes our stdin (or dies) we clean up and exit.
if (process.env.PLAYTEST_WATCH_STDIN === '1') try { process.stdin.on('end', async () => { if (finished) return; finished = true; await cleanup(); process.exit(3); }); process.stdin.on('error', () => {}); process.stdin.resume(); } catch {}
for (const sig of ['SIGINT', 'SIGTERM', 'SIGBREAK', 'SIGHUP']) { try { process.on(sig, async () => { finished = true; await cleanup(); process.exit(130); }); } catch {} }

try {
  const r = await run(cfg);
  if (!finished) { finished = true; clearTimeout(hard); await cleanup(); console.log(r.text); process.exit(r.code); }
} catch (e) {
  if (!finished) { finished = true; clearTimeout(hard); await cleanup(); console.log('PLAYTEST ERROR: ' + (e && e.message || e)); process.exit(2); }
}
