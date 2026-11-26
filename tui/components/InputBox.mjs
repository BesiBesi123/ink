// Rounded input box with "> " prompt, multi-line text and a block cursor; plus the status line under it.
import chalk from 'chalk';
import stringWidth from 'string-width';
import { h, Box, Text } from './h.mjs';
import { withCursor } from './Panels.mjs';
import { fmtK } from '../lib/format.mjs';

export function prefixKind(text) {
  const c = text[0];
  if (c === '!') return 'bash';
  if (c === '#') return 'memory';
  if (c === '&') return 'background';
  return null;
}

export function InputBox({ input, t, width, busy }) {
  const kind = prefixKind(input.text);
  const color = kind === 'bash' ? t.bash : kind === 'memory' ? t.code : kind === 'background' ? t.auto : t.border;
  const placeholder = busy ? 'Type to queue a message, ctrl+enter to steer' : 'Try "fix the failing test"';
  const body = withCursor(input.text, input.cur, placeholder, t);
  const lines = body.split('\n');
  const promptCh = kind === 'bash' ? '!' : '>';
  const shown = lines.map((l, i) => (i === 0 ? chalk.hex(kind ? color : t.text)(promptCh) + ' ' : '  ') + l).join('\n');
  return h(Box, { borderStyle: 'round', borderColor: color, paddingX: 1, width, flexDirection: 'column' },
    h(Text, null, shown));
}

export function StatusLine({ snap, t, width }) {
  const { mode, stats, hello } = snap;
  const kind = prefixKind(snap.input.text);
  let left;
  if (snap.ctrlCArmed) left = chalk.hex(t.dim)('Press Ctrl-C again to exit');
  else if (kind === 'bash') left = chalk.hex(t.bash)('! bash mode');
  else if (kind === 'memory') left = chalk.hex(t.code)('# memory');
  else if (kind === 'background') left = chalk.hex(t.auto)('& background task');
  else if (snap.flash) left = chalk.hex(t.dim)(snap.flash);
  else if (mode === 'plan') left = chalk.hex(t.plan)('⏸ plan mode') + chalk.hex(t.dim)(' (shift+tab to cycle)');
  else if (mode === 'safe') left = chalk.hex(t.warn)('safe mode') + chalk.hex(t.dim)(' (shift+tab to cycle)');
  else left = chalk.hex(t.auto)('⏵⏵ auto mode') + chalk.hex(t.dim)(' (shift+tab to cycle)');

  const parts = [];
  const model = stats.model || hello?.model;
  if (model) parts.push(chalk.hex(t.text)(model));
  const effort = stats.effort || hello?.effort;
  if (effort) parts.push(effort);
  const ctx = Number(stats.ctx || hello?.ctx) || 0;
  if (ctx) {
    const used = Number(stats.ctx_used) || 0;
    const pct = Math.min(100, Math.round((used / ctx) * 100));
    const cells = 5;
    const fill = Math.min(cells, Math.round((pct / 100) * cells));
    const barCol = pct >= 85 ? t.err : pct >= 65 ? t.warn : t.dim;
    parts.push(`ctx ${chalk.hex(barCol)('▓'.repeat(fill) + '░'.repeat(cells - fill))} ${pct}% of ${fmtK(ctx)}`);
  }
  if (stats.tps) parts.push(`${Number(stats.tps).toFixed(Number(stats.tps) < 10 ? 1 : 0)} tok/s`);
  if (stats.branch) parts.push(`⎇ ${stats.branch}`);
  if (Number(stats.jobs) > 0) parts.push(chalk.hex(t.accent)(`${stats.jobs} job${stats.jobs === 1 ? '' : 's'}`));

  const sep = chalk.hex(t.faint)(' · ');
  let right = parts.join(sep);
  const room = width - 2 - stringWidth(left) - 2;
  while (parts.length > 1 && stringWidth(right) > room) {
    // drop the least important bits first: branch, tps, effort
    const order = [parts.findIndex((p) => p.includes('⎇')), parts.findIndex((p) => p.includes('tok/s')), 1];
    const idx = order.find((i) => i > 0);
    parts.splice(idx ?? parts.length - 1, 1);
    right = parts.join(sep);
  }
  if (stringWidth(right) > room) right = '';
  return h(Box, { width, paddingX: 2, justifyContent: 'space-between' },
    h(Text, { wrap: 'truncate-end' }, left),
    h(Text, { color: t.dim, wrap: 'truncate-end' }, right));
}
