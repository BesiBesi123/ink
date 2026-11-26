// Raw terminal input -> key events. We own stdin (not Ink's useInput) so we can tell apart
// Enter ("\r") from Ctrl+Enter ("\n" on Windows consoles), handle bracketed paste, Esc, and
// kitty / xterm modifyOtherKeys encodings when a terminal sends them.
//
// Event shape: { name, ch, ctrl, meta, shift, text }   name in:
//   'char' (ch = text), 'paste' (text), 'enter', 'newline' (shift/alt+enter), 'ctrl-enter',
//   'escape', 'backspace', 'delete', 'tab', 'shift-tab', 'up', 'down', 'left', 'right',
//   'home', 'end', 'pageup', 'pagedown', or a ctrl letter: name='a'..'z' with ctrl=true.

const CSI_FINAL = { A: 'up', B: 'down', C: 'right', D: 'left', H: 'home', F: 'end', Z: 'shift-tab' };
const TILDE = { 1: 'home', 2: 'insert', 3: 'delete', 4: 'end', 5: 'pageup', 6: 'pagedown', 7: 'home', 8: 'end' };

function mods(n) {
  const m = Math.max(0, (Number(n) || 1) - 1);
  return { shift: !!(m & 1), meta: !!(m & 2), ctrl: !!(m & 4) };
}

function keyFromCode(code, mod) {
  const m = mods(mod);
  if (code === 13) {
    if (m.ctrl) return { name: 'ctrl-enter', ...m };
    if (m.shift || m.meta) return { name: 'newline', ...m };
    return { name: 'enter', ...m };
  }
  if (code === 27) return { name: 'escape', ...m };
  if (code === 9) return { name: m.shift ? 'shift-tab' : 'tab', ...m };
  if (code === 127 || code === 8) return { name: 'backspace', ...m };
  const ch = String.fromCodePoint(code);
  if (m.ctrl && /[a-z]/i.test(ch)) return { name: ch.toLowerCase(), ...m };
  return { name: 'char', ch: m.shift ? ch.toUpperCase() : ch, text: ch, ...m };
}

export class KeyParser {
  constructor() {
    this.pasting = false;
    this.pasteBuf = '';
  }

  feed(data) {
    const s = String(data);
    const out = [];
    let i = 0;
    // A chunk of plain text containing line breaks that arrives in one read is a paste in terminals
    // without bracketed paste support (e.g. legacy Windows consoles).
    if (!this.pasting && s.length > 2 && !s.includes('\x1b') && /[\r\n]/.test(s.replace(/[\r\n]+$/, '')) ) {
      out.push({ name: 'paste', text: s.replace(/\r\n?/g, '\n') });
      return out;
    }
    while (i < s.length) {
      if (this.pasting) {
        const end = s.indexOf('\x1b[201~', i);
        if (end < 0) { this.pasteBuf += s.slice(i); return out; }
        this.pasteBuf += s.slice(i, end);
        out.push({ name: 'paste', text: this.pasteBuf.replace(/\r\n?/g, '\n') });
        this.pasting = false;
        this.pasteBuf = '';
        i = end + 6;
        continue;
      }
      const c = s[i];
      if (c === '\x1b') {
        if (s.startsWith('\x1b[200~', i)) { this.pasting = true; i += 6; continue; }
        const next = s[i + 1];
        if (next === undefined) { out.push({ name: 'escape' }); i++; continue; }
        if (next === '\x1b') { out.push({ name: 'escape' }); i++; continue; }
        if (next === '[' || next === 'O') {
          // CSI / SS3: params then a final byte in @-~
          let j = i + 2;
          while (j < s.length && !/[@-~]/.test(s[j])) j++;
          if (j >= s.length) { out.push({ name: 'escape' }); i++; continue; }
          const params = s.slice(i + 2, j);
          const fin = s[j];
          i = j + 1;
          const p = params.replace(/^[<>?=]/, '').split(';');
          if (fin === 'u') { out.push(keyFromCode(Number(p[0]), p[1])); continue; }
          if (fin === '~') {
            if (p[0] === '27') { out.push(keyFromCode(Number(p[2]), p[1])); continue; } // modifyOtherKeys
            const name = TILDE[p[0]];
            if (name) out.push({ name, ...mods(p[1]) });
            continue;
          }
          if (fin === 'I' || fin === 'O') { out.push({ name: fin === 'I' ? 'focus-in' : 'focus-out' }); continue; }
          const name = CSI_FINAL[fin];
          if (name) out.push({ name, ...mods(p[1]) });
          continue;
        }
        // Alt+key
        i += 2;
        if (next === '\r' || next === '\n') out.push({ name: 'newline', meta: true });
        else if (next === '\x7f' || next === '\b') out.push({ name: 'backspace', meta: true });
        else out.push({ name: 'char', ch: next, text: next, meta: true });
        continue;
      }
      i++;
      if (c === '\r') { out.push({ name: 'enter' }); if (s[i] === '\n') i++; continue; }
      if (c === '\n') { out.push({ name: 'ctrl-enter', ctrl: true }); continue; }
      if (c === '\t') { out.push({ name: 'tab' }); continue; }
      if (c === '\x7f' || c === '\b') { out.push({ name: 'backspace' }); continue; }
      const code = c.charCodeAt(0);
      if (code < 32) { out.push({ name: String.fromCharCode(code + 96), ctrl: true }); continue; }
      // group printable runs into one 'char' event (fast typing / IME)
      let j = i;
      while (j < s.length && s.charCodeAt(j) >= 32 && s[j] !== '\x7f' && s[j] !== '\x1b') j++;
      const text = s.slice(i - 1, j);
      i = j;
      out.push({ name: 'char', ch: text, text });
    }
    return out;
  }
}
