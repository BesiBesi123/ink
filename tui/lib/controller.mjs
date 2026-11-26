// UI state machine: protocol events + key events in, immutable-ish snapshots out (consumed by React
// via useSyncExternalStore). Finished items are appended to `items` and printed once by <Static>.
import { Editor, History } from './editor.mjs';
import { safeSplit } from './markdown.mjs';
import { fuzzyScore, pickVerb } from './format.mjs';
import { preloadHighlighter } from './highlight.mjs';

const MODES = ['auto', 'safe', 'plan'];
const BUILTIN_COMMANDS = [{ name: 'exit', args: '', desc: 'Quit ink' }];

export class Controller {
  constructor({ engine, stdout, history = new History(), now = () => Date.now(), onQuit = () => {}, bell = () => {} }) {
    this.engine = engine;
    this.stdout = stdout;
    this.history = history;
    this.now = now;
    this.onQuit = onQuit;
    this.bell = bell;
    this.listeners = new Set();
    this.keyN = 0;

    this.items = [];
    this.hello = null;
    this.mode = 'auto';
    this.stats = {};
    this.busy = false;
    this.turnStart = 0;
    this.turnChars = 0;
    this.thinkTokens = 0;
    this.progress = null;
    this.status = '';
    this.thinking = null;
    this.answer = null;
    this.pending = new Map();
    this.plan = null;
    this.ask = null;
    this.queue = [];
    this.editor = new Editor();
    this.paletteSel = 0;
    this.paletteHiddenFor = null;
    this.completions = null;
    this.compReq = 0;
    this.compTimer = null;
    this.ctrlCAt = 0;
    this.escAt = 0;
    this.verb = pickVerb();
    this.verbAt = 0;
    this.turnLog = null;
    this.lastTurnLog = null;
    this.sent = [];
    this.tick = 0;
    this.quitting = false;
    this.flash = null; // {text, until}
    this.turnN = 0;

    this.snap = null;
    this.version = 0;
    this.notifyTimer = null;
    this.timer = setInterval(() => this.onTick(), 100);
    this.timer.unref?.();
    this.rebuild();
  }

  // ---------- store plumbing ----------
  subscribe = (fn) => { this.listeners.add(fn); return () => this.listeners.delete(fn); };
  getSnapshot = () => this.snap;

  notify(immediate = false) {
    if (immediate) { clearTimeout(this.notifyTimer); this.notifyTimer = null; this.flush(); return; }
    if (this.notifyTimer) return;
    this.notifyTimer = setTimeout(() => { this.notifyTimer = null; this.flush(); }, 16);
  }

  flush() {
    this.rebuild();
    for (const fn of this.listeners) fn();
  }

  rebuild() {
    this.version++;
    const width = Math.max(20, (this.stdout?.columns || 80));
    const rows = Math.max(10, (this.stdout?.rows || 24));
    this.snap = {
      version: this.version,
      width,
      rows,
      items: this.itemsView(),
      hello: this.hello,
      mode: this.mode,
      stats: this.stats,
      busy: this.busy,
      elapsed: this.busy ? (this.now() - this.turnStart) / 1000 : 0,
      tokens: Math.round(this.thinkTokens + this.turnChars / 4),
      progress: this.progress,
      status: this.status,
      thinking: this.thinking && { text: this.thinking.text, secs: (this.now() - this.thinking.start) / 1000 },
      answer: this.answer && { text: this.answer.buf, first: this.answer.first },
      pending: [...this.pending.values()],
      plan: this.plan,
      ask: this.ask && { ...this.ask, inputText: this.ask.editor.text, inputCur: this.ask.editor.cur },
      queue: this.queue.slice(),
      input: { text: this.editor.text, cur: this.editor.cur },
      palette: this.paletteItems(),
      paletteSel: this.paletteSel,
      completions: this.completions,
      ctrlCArmed: this.now() - this.ctrlCAt < 1500,
      verb: this.verb,
      tick: this.tick,
      flash: this.flash && this.flash.until > this.now() ? this.flash.text : null,
      starting: !this.hello,
    };
  }

  // <Static> memoises on the array identity, so hand it a new array whenever items were appended.
  itemsView() {
    if (!this._itemsCopy || this._itemsCopy.length !== this.items.length) this._itemsCopy = this.items.slice();
    return this._itemsCopy;
  }

  push(item) {
    item.key = `i${++this.keyN}`;
    this.items.push(item);
    this.notify();
  }

  onTick() {
    this.tick++;
    if (this.busy && this.now() - this.verbAt > 7000) { this.verb = pickVerb(this.verb); this.verbAt = this.now(); }
    const animating = this.busy || this.pending.size || !this.hello || this.ask;
    if (animating || (this.flash && this.flash.until > this.now() - 200) || this.ctrlCAt) {
      if (this.ctrlCAt && this.now() - this.ctrlCAt > 1600) this.ctrlCAt = 0;
      this.notify();
    }
  }

  dispose() { clearInterval(this.timer); clearTimeout(this.notifyTimer); clearTimeout(this.compTimer); }

  send(obj) { this.engine?.send(obj); }

  setFlash(text, ms = 2500) { this.flash = { text, until: this.now() + ms }; this.notify(); }

  // ---------- engine events ----------
  handleEvent(ev) {
    try {
      const fn = this['ev_' + ev.type];
      if (typeof fn === 'function') fn.call(this, ev);
    } catch (e) {
      this.push({ kind: 'notice', level: 'error', text: `tui: error handling ${ev?.type}: ${e?.message || e}` });
    }
    this.notify();
  }

  ev_hello(ev) {
    this.hello = ev;
    if (MODES.includes(ev.mode)) this.mode = ev.mode;
    this.push({ kind: 'welcome', hello: ev });
    if (ev.resumed > 0) this.push({ kind: 'notice', level: 'info', text: `Resumed session ${ev.session || ''} (${ev.resumed} prompt${ev.resumed === 1 ? '' : 's'} restored)` });
    preloadHighlighter();
  }

  ev_status(ev) { this.status = String(ev.text || ''); }

  ev_turn_start(ev) {
    const text = String(ev.text ?? '');
    const i = this.sent.indexOf(text);
    if (i >= 0) this.sent.splice(i, 1);
    else if (text) this.push({ kind: 'user', text });
    if (!this.busy) this.startTurnClock();
    this.busy = true;
    this.turnLog = { id: ev.id ?? ++this.turnN, prompt: text, entries: [] };
  }

  startTurnClock() {
    this.turnStart = this.now();
    this.turnChars = 0;
    this.thinkTokens = 0;
    this.verb = pickVerb(this.verb);
    this.verbAt = this.now();
    this.progress = null;
  }

  ev_progress(ev) { this.progress = { done: Number(ev.done) || 0, total: Number(ev.total) || 0 }; }

  ev_thinking_delta(ev) {
    this.progress = null;
    if (!this.thinking) this.thinking = { text: '', start: this.now(), chars: 0 };
    const t = String(ev.text ?? '');
    this.thinking.text += t;
    this.thinking.chars += t.length;
    this.turnChars += t.length;
  }

  ev_thinking_end(ev) {
    const th = this.thinking;
    this.thinking = null;
    if (!th && !ev.secs) return;
    const secs = Number(ev.secs) || (th ? (this.now() - th.start) / 1000 : 0);
    if (th && ev.tokens) { this.turnChars -= th.chars; this.thinkTokens += Number(ev.tokens) || 0; }
    this.push({ kind: 'thought', secs });
    this.log({ type: 'thinking', text: th?.text || '', secs });
  }

  flushThinking() { if (this.thinking) this.ev_thinking_end({}); }

  ev_text_delta(ev) {
    this.progress = null;
    this.flushThinking();
    if (!this.answer) this.answer = { buf: '', first: true };
    const t = String(ev.text ?? '');
    this.answer.buf += t;
    this.turnChars += t.length;
    const cut = safeSplit(this.answer.buf);
    if (cut > 0) {
      const chunk = this.answer.buf.slice(0, cut);
      this.answer.buf = this.answer.buf.slice(cut);
      this.pushMd(chunk);
    }
  }

  pushMd(text) {
    if (!text.trim()) return;
    this.push({ kind: 'md', text: text.replace(/\s+$/, ''), first: this.answer?.first ?? true });
    if (this.answer) this.answer.first = false;
  }

  ev_text_end() {
    if (this.answer) this.pushMd(this.answer.buf);
    this.answer = null;
  }

  flushAnswer() { if (this.answer) this.ev_text_end(); }

  ev_tool_start(ev) {
    this.flushThinking();
    this.flushAnswer();
    this.progress = null;
    const id = String(ev.id ?? `t${this.keyN}`);
    this.pending.set(id, { id, name: ev.name, label: ev.label || ev.name || 'tool', args: ev.args, start: this.now(), diffs: [] });
  }

  ev_diff(ev) {
    const d = { path: ev.path, diff: String(ev.diff ?? ''), added: ev.added, removed: ev.removed, created: !!ev.created };
    const p = this.pending.get(String(ev.id ?? ''));
    if (p) p.diffs.push(d);
    else this.push({ kind: 'diff', ...d });
  }

  ev_tool_end(ev) {
    const id = String(ev.id ?? '');
    const p = this.pending.get(id) || { label: ev.label || ev.name || id || 'tool', diffs: [] };
    this.pending.delete(id);
    const item = { kind: 'tool', label: p.label, ok: ev.ok !== false, summary: String(ev.summary ?? ''),
      preview: Array.isArray(ev.preview) ? ev.preview.map(String) : [], diffs: p.diffs };
    this.push(item);
    this.log({ type: 'tool', label: p.label, ok: item.ok, summary: item.summary, output: String(ev.output ?? (item.preview.join('\n'))), diffs: p.diffs });
  }

  ev_plan(ev) {
    this.plan = { goal: ev.goal || '', key_points: ev.key_points || [], steps: Array.isArray(ev.steps) ? ev.steps : [] };
  }

  ev_check(ev) { this.push({ kind: 'check', name: ev.name, ok: !!ev.ok, summary: ev.summary || '' }); }
  ev_notice(ev) { this.push({ kind: 'notice', level: ev.level || 'info', text: String(ev.text ?? '') }); }
  ev_output(ev) { this.push({ kind: 'output', text: String(ev.text ?? '') }); }

  ev_ask(ev) {
    this.flushThinking();
    this.flushAnswer();
    const options = Array.isArray(ev.options) && ev.options.length ? ev.options : ['yes', 'always', 'no'];
    this.ask = { id: ev.id, kind: ev.kind, title: ev.title || 'Permission required', detail: String(ev.detail ?? ''), options, sel: 0, typing: false, editor: new Editor() };
  }

  ev_stats(ev) {
    this.stats = { ...this.stats, ...ev };
    if (MODES.includes(ev.mode)) this.mode = ev.mode;
  }

  ev_turn_end(ev) {
    this.flushThinking();
    this.flushAnswer();
    for (const p of this.pending.values()) this.push({ kind: 'tool', label: p.label, ok: false, summary: 'interrupted', preview: [], diffs: p.diffs });
    this.pending.clear();
    if (this.ask) this.ask = null;
    const secs = Number(ev.secs) || (this.busy ? (this.now() - this.turnStart) / 1000 : 0);
    if (ev.interrupted) this.push({ kind: 'notice', level: 'warn', text: 'Interrupted · tell ink what to do instead' });
    this.busy = false;
    this.progress = null;
    this.status = '';
    this.plan = null;
    if (this.turnLog?.entries?.length) this.lastTurnLog = this.turnLog;
    this.turnLog = null;
    if (secs > 30) this.bell();
    if (this.queue.length) {
      const next = this.queue.shift();
      setTimeout(() => { this.submit(next); this.notify(); }, 0);
    }
  }

  ev_completions(ev) {
    if (!this.completions || ev.req !== this.completions.req) return;
    this.completions.items = Array.isArray(ev.items) ? ev.items.slice(0, 50) : [];
    this.completions.sel = 0;
  }

  ev_sessions(ev) {
    const items = Array.isArray(ev.items) ? ev.items : [];
    const lines = items.length
      ? items.map((s, i) => `${String(i + 1).padStart(2)}. ${s.id}  ${s.when || ''}  ${s.title || ''}  (${s.prompts ?? 0} prompts)`)
      : ['no saved sessions'];
    this.push({ kind: 'output', text: lines.join('\n') + (items.length ? '\n\nuse /resume <id> to continue one' : '') });
  }

  ev_bye() { this.byeSeen = true; }

  log(entry) {
    if (!this.turnLog) this.turnLog = { id: ++this.turnN, prompt: '', entries: [] };
    this.turnLog.entries.push(entry);
  }

  // ---------- palette / completions ----------
  commands() {
    const cmds = Array.isArray(this.hello?.commands) ? this.hello.commands : [];
    const names = new Set(cmds.map((c) => c.name));
    return [...cmds, ...BUILTIN_COMMANDS.filter((c) => !names.has(c.name))];
  }

  paletteItems() {
    const t = this.editor.text;
    if (!t.startsWith('/') || /\s/.test(t) || this.paletteHiddenFor === t) return [];
    const q = t.slice(1);
    return this.commands()
      .map((c) => ({ c, s: fuzzyScore(q, String(c.name || '')) }))
      .filter((x) => x.s >= 0)
      .sort((a, b) => b.s - a.s)
      .slice(0, 8)
      .map((x) => x.c);
  }

  afterEdit() {
    this.paletteSel = 0;
    const tok = this.editor.tokenBeforeCursor();
    if (tok && tok.text.startsWith('@')) {
      const prefix = tok.text.slice(1);
      if (!this.completions || this.completions.prefix !== prefix) {
        const req = ++this.compReq;
        this.completions = { req, prefix, start: tok.start, items: this.completions?.items || [], sel: 0 };
        clearTimeout(this.compTimer);
        this.compTimer = setTimeout(() => this.send({ type: 'complete', req, kind: 'file', prefix }), 60);
      }
    } else this.completions = null;
  }

  acceptCompletion() {
    const c = this.completions;
    const it = c?.items?.[c.sel];
    if (!it) return false;
    const val = String(it.value ?? it.label ?? '');
    const tok = this.editor.tokenBeforeCursor();
    const start = tok ? tok.start : c.start;
    const isDir = /[\\/]$/.test(val);
    this.editor.replaceRange(start, this.editor.cur, '@' + val + (isDir ? '' : ' '));
    this.completions = null;
    if (isDir) this.afterEdit();
    return true;
  }

  // ---------- submit ----------
  submit(text) {
    if (!text.trim()) return;
    // typed stop works where Esc can't be pressed (phone keyboards, remote desktop)
    if (this.busy && /^\s*\/(stop|esc|interrupt)\s*$/i.test(text)) { this.interrupt(); return; }
    if (this.busy) { this.queue.push(text); return; }
    if (text.startsWith('/') && !text.startsWith('//')) {
      const m = /^\/(\S+)\s*([\s\S]*)$/.exec(text);
      const name = m ? m[1] : text.slice(1);
      const arg = m ? m[2] : '';
      this.push({ kind: 'user', text });
      if (['exit', 'quit', 'q'].includes(name.toLowerCase())) { this.quit(); return; }
      this.send({ type: 'command', name, arg });
      // the engine answers every command with turn_end; until then show the spinner and queue new input
      this.busy = true;
      this.status = `/${name}…`;
      this.startTurnClock();
      return;
    }
    this.push({ kind: 'user', text });
    this.sent.push(text);
    if (this.sent.length > 20) this.sent.shift();
    this.send({ type: 'prompt', text });
    this.busy = true;
    this.startTurnClock();
    this.turnLog = { id: ++this.turnN, prompt: text, entries: [] };
  }

  quit() {
    if (this.quitting) return;
    this.quitting = true;
    this.send({ type: 'quit' });
    this.onQuit();
  }

  interrupt() {
    this.send({ type: 'interrupt' });
    this.status = 'interrupting…';
  }

  cycleMode() {
    const i = MODES.indexOf(this.mode);
    this.mode = MODES[(i + 1) % MODES.length];
    this.send({ type: 'mode', mode: this.mode });
  }

  expandTranscript() {
    const log = this.turnLog?.entries?.length ? this.turnLog : this.lastTurnLog;
    if (!log || !log.entries.length) { this.setFlash('nothing to expand yet'); return; }
    this.push({ kind: 'transcript', log: { ...log, entries: log.entries.slice() } });
  }

  // ---------- keys ----------
  handleKey(k) {
    try { this._key(k); } catch (e) {
      this.push({ kind: 'notice', level: 'error', text: `tui: key handler error: ${e?.message || e}` });
    }
    this.notify(true);
  }

  _key(k) {
    if (k.name === 'focus-in' || k.name === 'focus-out') return;
    if (this.ask) return this.askKey(k);
    const ed = this.editor;

    // ----- global -----
    if (k.ctrl && k.name === 'c') {
      if (!ed.isEmpty()) { ed.clear(); this.history.reset(); this.afterEdit(); return; }
      if (this.busy) { this.interrupt(); this.ctrlCAt = this.now(); return; }
      if (this.now() - this.ctrlCAt < 1500) { this.quit(); return; }
      this.ctrlCAt = this.now();
      return;
    }
    if (k.ctrl && k.name === 'd') { if (ed.isEmpty()) this.quit(); else ed.del(); return; }
    if (k.ctrl && k.name === 'o') { this.expandTranscript(); return; }
    if (k.name === 'shift-tab') { this.cycleMode(); return; }

    const palette = this.paletteItems();
    const comps = this.completions?.items?.length ? this.completions : null;

    if (k.name === 'escape') {
      if (palette.length) { this.paletteHiddenFor = ed.text; return; }
      if (this.completions) { this.completions = null; return; }
      if (this.busy) { this.interrupt(); this.escAt = 0; return; }
      if (this.now() - this.escAt < 600) {
        this.escAt = 0;
        this.push({ kind: 'user', text: '/checkpoints', dim: true });
        this.send({ type: 'command', name: 'checkpoints', arg: '' });
        return;
      }
      this.escAt = this.now();
      if (!ed.isEmpty()) this.setFlash('esc again to rewind (checkpoints)', 1200);
      else this.setFlash('esc again to show checkpoints', 1200);
      return;
    }

    // ----- palette / completion navigation -----
    if (palette.length) {
      if (k.name === 'up') { this.paletteSel = (this.paletteSel - 1 + palette.length) % palette.length; return; }
      if (k.name === 'down') { this.paletteSel = (this.paletteSel + 1) % palette.length; return; }
      if (k.name === 'tab' || k.name === 'enter') {
        const c = palette[Math.min(this.paletteSel, palette.length - 1)];
        const name = '/' + c.name;
        if (k.name === 'enter' && !c.args) { ed.clear(); this.history.add(name); this.submit(name); this.afterEdit(); return; }
        ed.set(name + ' ');
        this.afterEdit();
        return;
      }
    }
    if (comps) {
      if (k.name === 'up') { comps.sel = (comps.sel - 1 + comps.items.length) % comps.items.length; return; }
      if (k.name === 'down') { comps.sel = (comps.sel + 1) % comps.items.length; return; }
      if (k.name === 'tab' || k.name === 'enter') { this.acceptCompletion(); return; }
    }

    // ----- submit / newline / steer -----
    if (k.name === 'enter') {
      if (ed.cur > 0 && ed.text[ed.cur - 1] === '\\') { ed.backspace(); ed.insert('\n'); this.afterEdit(); return; }
      const text = ed.value();
      if (!text.trim()) return;
      this.history.add(ed.text.includes('[Pasted text #') ? text : ed.text);
      ed.clear();
      this.afterEdit();
      this.submit(text);
      return;
    }
    if (k.name === 'ctrl-enter' || (k.meta && k.name === 'char' && k.ch === 's')) {
      if (this.busy && !ed.isEmpty()) {
        const text = ed.value();
        ed.clear();
        this.afterEdit();
        this.send({ type: 'steer', text });
        this.push({ kind: 'steer', text });
        return;
      }
      if (k.name === 'ctrl-enter') { ed.insert('\n'); this.afterEdit(); }
      return;
    }
    if (k.name === 'newline') { ed.insert('\n'); this.afterEdit(); return; }

    // ----- history / cursor -----
    if (k.name === 'up') {
      if (ed.onFirstLine()) {
        if (ed.isEmpty() && this.queue.length) { ed.set(this.queue.pop()); return; }
        const h = this.history.prev(ed.text);
        if (h != null) ed.set(h);
      } else ed.up();
      return;
    }
    if (k.name === 'down') {
      if (ed.onLastLine()) {
        const h = this.history.next();
        if (h != null) ed.set(h);
      } else ed.down();
      return;
    }
    if (k.name === 'left') { if (k.ctrl || k.meta) ed.wordLeft(); else ed.left(); this.afterEdit(); return; }
    if (k.name === 'right') { if (k.ctrl || k.meta) ed.wordRight(); else ed.right(); this.afterEdit(); return; }
    if (k.name === 'home' || (k.ctrl && k.name === 'a')) { ed.home(); return; }
    if (k.name === 'end' || (k.ctrl && k.name === 'e')) { ed.end(); return; }
    if (k.name === 'backspace') { if (k.meta || k.ctrl) ed.deleteWordLeft(); else ed.backspace(); this.afterEdit(); return; }
    if (k.name === 'delete') { ed.del(); this.afterEdit(); return; }
    if (k.ctrl && k.name === 'w') { ed.deleteWordLeft(); this.afterEdit(); return; }
    if (k.ctrl && k.name === 'u') { ed.killToStart(); this.afterEdit(); return; }
    if (k.ctrl && k.name === 'k') { ed.killToEnd(); this.afterEdit(); return; }
    if (k.ctrl && k.name === 'j') { ed.insert('\n'); this.afterEdit(); return; }
    if (k.ctrl && k.name === 'l') { this.stdout?.write?.('\x1b[2J\x1b[3J\x1b[H'); return; }
    if (k.meta && k.name === 'char' && k.ch === 'b') { ed.wordLeft(); return; }
    if (k.meta && k.name === 'char' && k.ch === 'f') { ed.wordRight(); return; }
    if (k.name === 'tab') return;
    if (k.name === 'paste') { ed.paste(k.text); this.afterEdit(); return; }
    if (k.name === 'char' && !k.ctrl && !k.meta) {
      this.history.reset();
      ed.insert(k.text);
      this.afterEdit();
    }
  }

  askKey(k) {
    const a = this.ask;
    if (a.typing) {
      const ed = a.editor;
      if (k.name === 'escape') { this.answerAsk('no', ''); return; }
      if (k.name === 'enter') { this.answerAsk('no', ed.value()); return; }
      if (k.name === 'backspace') ed.backspace();
      else if (k.name === 'left') ed.left();
      else if (k.name === 'right') ed.right();
      else if (k.name === 'paste') ed.paste(k.text);
      else if (k.name === 'newline' || k.name === 'ctrl-enter') ed.insert('\n');
      else if (k.ctrl && k.name === 'u') ed.killToStart();
      else if (k.ctrl && k.name === 'c') { if (ed.isEmpty()) this.answerAsk('no', ''); else ed.clear(); }
      else if (k.name === 'char' && !k.ctrl && !k.meta) ed.insert(k.text);
      return;
    }
    const n = a.options.length;
    if (k.name === 'up') { a.sel = (a.sel - 1 + n) % n; return; }
    if (k.name === 'down' || k.name === 'tab') { a.sel = (a.sel + 1) % n; return; }
    if (k.name === 'escape' || (k.ctrl && k.name === 'c')) { this.answerAsk('no', ''); return; }
    if (k.name === 'char' && /^[1-9]$/.test(k.ch) && Number(k.ch) <= n) { a.sel = Number(k.ch) - 1; return this.confirmAsk(); }
    if (k.name === 'char' && (k.ch === 'y' || k.ch === 'Y')) { a.sel = Math.max(0, a.options.indexOf('yes')); return this.confirmAsk(); }
    if (k.name === 'char' && (k.ch === 'n' || k.ch === 'N')) { a.sel = Math.max(0, a.options.indexOf('no')); return this.confirmAsk(); }
    if (k.name === 'enter') return this.confirmAsk();
    if (k.name === 'shift-tab') { this.cycleMode(); }
  }

  confirmAsk() {
    const a = this.ask;
    const choice = a.options[a.sel];
    if (choice === 'no') { a.typing = true; return; }
    this.answerAsk(choice, '');
  }

  answerAsk(choice, text) {
    const a = this.ask;
    if (!a) return;
    this.ask = null;
    const msg = { type: 'answer', id: a.id, choice };
    if (text) msg.text = text;
    this.send(msg);
    this.push({ kind: 'askResult', title: a.title, choice, text });
  }
}
