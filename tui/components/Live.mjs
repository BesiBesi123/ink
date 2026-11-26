// The redrawable bottom area pieces: streaming thinking/answer, running tools, spinner.
import chalk from 'chalk';
import wrapAnsi from 'wrap-ansi';
import { h, Box, Text, clean } from './h.mjs';
import { labelText } from './Items.mjs';
import { fmtK, fmtSecs, SPIN_FRAMES } from '../lib/format.mjs';

function tailLines(text, width, n) {
  const lines = wrapAnsi(clean(text), Math.max(10, width), { hard: true, trim: false }).split('\n');
  while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
  return { lines: lines.slice(-n), hidden: Math.max(0, lines.length - n) };
}

export function LiveThinking({ thinking, t, width }) {
  const { lines, hidden } = tailLines(thinking.text, width - 4, 6);
  return h(Box, { flexDirection: 'column', marginTop: 1 },
    h(Text, { color: t.dim, italic: true }, `∴ Thinking… `, h(Text, { color: t.faint, italic: false }, fmtSecs(thinking.secs))),
    hidden ? h(Text, { color: t.faint }, '  …') : null,
    lines.length ? h(Box, { paddingLeft: 2 }, h(Text, { color: t.dim, italic: true }, lines.join('\n'))) : null);
}

export function LiveAnswer({ answer, t, width, maxLines }) {
  if (!answer.text.trim()) return null;
  const { lines, hidden } = tailLines(answer.text, width - 3, Math.max(3, maxLines));
  return h(Box, { flexDirection: 'row', marginTop: 1 },
    h(Box, { width: 2, flexShrink: 0 }, h(Text, null, answer.first && !hidden ? '●' : ' ')),
    h(Box, { flexDirection: 'column' },
      hidden ? h(Text, { color: t.faint }, `… ${hidden} more lines above`) : null,
      h(Text, null, lines.join('\n'))));
}

export function PendingTool({ p, t, tick, now }) {
  const on = Math.floor(tick / 5) % 2 === 0;
  const secs = (now - p.start) / 1000;
  return h(Box, { flexDirection: 'column', marginTop: 1 },
    h(Text, { wrap: 'truncate-end' }, chalk.hex(on ? t.accent : t.faint)('●') + ' ' + labelText(p.label, t)),
    h(Text, { color: t.dim }, `  ⎿  ${p.diffs.length ? 'writing…' : 'running…'}${secs >= 2 ? ' ' + fmtSecs(secs) : ''}`));
}

function shimmer(word, tick, t) {
  const pos = (tick % (word.length + 8)) - 4;
  let s = '';
  for (let i = 0; i < word.length; i++) {
    const d = Math.abs(i - pos);
    s += chalk.hex(d === 0 ? t.accentLight : d === 1 ? mix(t.accent, t.accentLight) : t.accent)(word[i]);
  }
  return s;
}

function mix(a, b) {
  const p = (x) => [1, 3, 5].map((i) => parseInt(x.slice(i, i + 2), 16));
  const [r1, g1, b1] = p(a), [r2, g2, b2] = p(b);
  const hx = (v) => Math.round(v).toString(16).padStart(2, '0');
  return `#${hx((r1 + r2) / 2)}${hx((g1 + g2) / 2)}${hx((b1 + b2) / 2)}`;
}

export function Spinner({ snap, t }) {
  const frame = SPIN_FRAMES[snap.tick % SPIN_FRAMES.length];
  const bits = [fmtSecs(snap.elapsed)];
  if (snap.progress && snap.progress.total) bits.push(`reading ${fmtK(snap.progress.done)}/${fmtK(snap.progress.total)}`);
  else if (snap.tokens > 0) bits.push(`↓ ${fmtK(snap.tokens)} tokens`);
  bits.push(chalk.bold('esc') + ' to interrupt');
  return h(Box, { marginTop: 1 },
    h(Text, { wrap: 'truncate-end' },
      chalk.hex(t.accent)(frame) + ' ' + shimmer(snap.verb + '…', snap.tick, t) + ' ' +
      chalk.hex(t.dim)('(' + bits.join(' · ') + ')')));
}

export function StatusText({ text, t }) {
  return h(Text, { color: t.dim, wrap: 'truncate-end' }, '  ' + clean(text));
}

export function Starting({ snap, t }) {
  const frame = SPIN_FRAMES[snap.tick % SPIN_FRAMES.length];
  return h(Box, { marginTop: 1 }, h(Text, null, chalk.hex(t.accent)(frame) + ' ' + chalk.hex(t.dim)(snap.status || 'Starting ink…')));
}
