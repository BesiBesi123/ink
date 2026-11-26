// Multi-line input buffer with a cursor, paste placeholders and history navigation.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

const PASTE_RX = /\[Pasted text #(\d+) \+\d+ lines\]/g;

export class Editor {
  constructor() {
    this.text = '';
    this.cur = 0;
    this.pastes = new Map();
    this.pasteN = 0;
  }

  set(text, cur = text.length) { this.text = text; this.cur = Math.max(0, Math.min(cur, text.length)); }
  clear() { this.set(''); this.pastes.clear(); }
  isEmpty() { return this.text.length === 0; }

  insert(s) {
    this.text = this.text.slice(0, this.cur) + s + this.text.slice(this.cur);
    this.cur += s.length;
  }

  paste(s) {
    const lines = s.split('\n').length;
    if (s.length > 800 || lines > 10) {
      const n = ++this.pasteN;
      this.pastes.set(n, s);
      this.insert(`[Pasted text #${n} +${lines} lines]`);
    } else this.insert(s);
  }

  // Full text with paste placeholders expanded.
  value() {
    return this.text.replace(PASTE_RX, (m, n) => this.pastes.get(Number(n)) ?? m);
  }

  backspace() {
    if (this.cur === 0) return;
    const before = this.text.slice(0, this.cur);
    const m = /\[Pasted text #\d+ \+\d+ lines\]$/.exec(before);
    const n = m ? m[0].length : prevCharLen(before);
    this.text = before.slice(0, -n) + this.text.slice(this.cur);
    this.cur -= n;
  }

  del() {
    if (this.cur >= this.text.length) return;
    const n = nextCharLen(this.text, this.cur);
    this.text = this.text.slice(0, this.cur) + this.text.slice(this.cur + n);
  }

  left() { if (this.cur > 0) this.cur -= prevCharLen(this.text.slice(0, this.cur)); }
  right() { if (this.cur < this.text.length) this.cur += nextCharLen(this.text, this.cur); }
  lineStart() { return this.text.lastIndexOf('\n', this.cur - 1) + 1; }
  lineEnd() { const e = this.text.indexOf('\n', this.cur); return e < 0 ? this.text.length : e; }
  home() { this.cur = this.lineStart(); }
  end() { this.cur = this.lineEnd(); }
  onFirstLine() { return this.text.lastIndexOf('\n', this.cur - 1) < 0; }
  onLastLine() { return this.text.indexOf('\n', this.cur) < 0; }

  up() {
    const ls = this.lineStart();
    if (ls === 0) return false;
    const col = this.cur - ls;
    const pls = this.text.lastIndexOf('\n', ls - 2) + 1;
    this.cur = Math.min(pls + col, ls - 1);
    return true;
  }

  down() {
    const le = this.lineEnd();
    if (le >= this.text.length) return false;
    const col = this.cur - this.lineStart();
    const nls = le + 1;
    const nle = this.text.indexOf('\n', nls);
    this.cur = Math.min(nls + col, nle < 0 ? this.text.length : nle);
    return true;
  }

  wordLeft() {
    let i = this.cur;
    while (i > 0 && /\s/.test(this.text[i - 1])) i--;
    while (i > 0 && !/\s/.test(this.text[i - 1])) i--;
    this.cur = i;
  }

  wordRight() {
    let i = this.cur;
    while (i < this.text.length && /\s/.test(this.text[i])) i++;
    while (i < this.text.length && !/\s/.test(this.text[i])) i++;
    this.cur = i;
  }

  deleteWordLeft() {
    const end = this.cur;
    this.wordLeft();
    this.text = this.text.slice(0, this.cur) + this.text.slice(end);
  }

  killToStart() { const s = this.lineStart(); this.text = this.text.slice(0, s) + this.text.slice(this.cur); this.cur = s; }
  killToEnd() { const e = this.lineEnd(); this.text = this.text.slice(0, this.cur) + this.text.slice(e === this.cur ? e + 1 : e); }

  // The "@token" (or other) word ending at the cursor.
  tokenBeforeCursor() {
    const before = this.text.slice(0, this.cur);
    const m = /(^|\s)(@[^\s]*)$/.exec(before);
    return m ? { start: this.cur - m[2].length, text: m[2] } : null;
  }

  replaceRange(start, end, s) {
    this.text = this.text.slice(0, start) + s + this.text.slice(end);
    this.cur = start + s.length;
  }
}

function prevCharLen(s) {
  const c = s.charCodeAt(s.length - 1);
  return c >= 0xdc00 && c <= 0xdfff && s.length > 1 ? 2 : 1;
}
function nextCharLen(s, i) {
  const c = s.charCodeAt(i);
  return c >= 0xd800 && c <= 0xdbff && i + 1 < s.length ? 2 : 1;
}

// Prompt history persisted as JSON lines in ~/.ink/tui-history.jsonl
export class History {
  constructor(file = path.join(os.homedir(), '.ink', 'tui-history.jsonl')) {
    this.file = file;
    this.items = [];
    this.pos = -1; // -1 = editing the draft
    this.draft = '';
    try {
      const lines = fs.readFileSync(file, 'utf8').split('\n').filter(Boolean).slice(-1000);
      for (const l of lines) {
        try { const o = JSON.parse(l); if (o && typeof o.text === 'string') this.items.push(o.text); } catch {}
      }
    } catch {}
  }

  add(text) {
    if (!text.trim()) return;
    if (this.items[this.items.length - 1] !== text) {
      this.items.push(text);
      try {
        fs.mkdirSync(path.dirname(this.file), { recursive: true });
        fs.appendFileSync(this.file, JSON.stringify({ text, cwd: process.cwd(), ts: Date.now() }) + '\n');
      } catch {}
    }
    this.reset();
  }

  reset() { this.pos = -1; this.draft = ''; }

  prev(current) {
    if (!this.items.length) return null;
    if (this.pos === -1) { this.draft = current; this.pos = this.items.length; }
    if (this.pos <= 0) return null;
    this.pos--;
    return this.items[this.pos];
  }

  next() {
    if (this.pos === -1) return null;
    this.pos++;
    if (this.pos >= this.items.length) { this.pos = -1; return this.draft; }
    return this.items[this.pos];
  }
}
