// Unified diff hunk renderer: line numbers + red/green background lines, capped.
import chalk from 'chalk';
import stringWidth from 'string-width';
import wrapAnsi from 'wrap-ansi';
import { h, Box, Text, expandTabs } from './h.mjs';

const MAX_LINES = 30;

export function parseDiff(text) {
  const rows = [];
  let oldN = 1, newN = 1;
  for (const raw of String(text ?? '').replace(/\r/g, '').split('\n')) {
    if (raw.startsWith('---') || raw.startsWith('+++')) continue;
    const m = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(raw);
    if (m) {
      if (rows.length) rows.push({ type: 'gap' });
      oldN = Number(m[1]); newN = Number(m[2]);
      continue;
    }
    if (raw.startsWith('\\')) continue; // "\ No newline at end of file"
    const sign = raw[0];
    const body = expandTabs(raw.slice(1));
    if (sign === '+') rows.push({ type: 'add', n: newN++, body });
    else if (sign === '-') rows.push({ type: 'del', n: oldN++, body });
    else if (raw === '' && rows.length === 0) continue;
    else { rows.push({ type: 'ctx', n: newN, body: sign === ' ' ? body : expandTabs(raw) }); oldN++; newN++; }
  }
  while (rows.length && rows[rows.length - 1].type === 'ctx' && rows[rows.length - 1].body === '') rows.pop();
  return rows;
}

export function diffLines(d, t, width, indent = 5) {
  const rows = parseDiff(d.diff);
  const long = rows.length > MAX_LINES;
  const shown = rows.slice(0, MAX_LINES);
  const numW = Math.max(3, ...shown.filter((r) => r.n).map((r) => String(r.n).length));
  const avail = Math.max(10, width - indent - numW - 3);
  const out = [];
  for (const r of shown) {
    if (r.type === 'gap') { out.push(' '.repeat(indent) + chalk.hex(t.faint)(' '.repeat(numW - 1) + '⋮')); continue; }
    const sign = r.type === 'add' ? '+' : r.type === 'del' ? '-' : ' ';
    const parts = wrapAnsi(r.body || ' ', avail, { hard: true, trim: false }).split('\n');
    parts.forEach((p, i) => {
      const num = i === 0 ? String(r.n).padStart(numW) : ' '.repeat(numW);
      let line = `${num} ${i === 0 ? sign : ' '} ${p}`;
      line += ' '.repeat(Math.max(0, numW + 3 + avail - stringWidth(line)));
      if (r.type === 'add') line = chalk.bgHex(long ? t.addBgDim : t.addBg).hex(t.addFg)(line);
      else if (r.type === 'del') line = chalk.bgHex(long ? t.delBgDim : t.delBg).hex(t.delFg)(line);
      else line = chalk.hex(t.dim)(line);
      out.push(' '.repeat(indent) + line);
    });
  }
  if (long) out.push(' '.repeat(indent) + chalk.hex(t.dim)(`… ${rows.length - MAX_LINES} more lines`));
  return out;
}

export function diffHeader(d) {
  const a = Number(d.added) || 0, r = Number(d.removed) || 0;
  if (d.created) return `Created ${d.path} (+${a} line${a === 1 ? '' : 's'})`;
  return `Updated ${d.path} with +${a} -${r}`;
}

export function DiffBlock({ d, t, width, first = true }) {
  return h(Box, { flexDirection: 'column' },
    h(Text, null, chalk.hex(t.dim)(first ? '  ⎿  ' : '     ') + chalk.bold(diffHeader(d))),
    h(Text, null, diffLines(d, t, width).join('\n')));
}
