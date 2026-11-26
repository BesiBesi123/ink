// Plan panel, permission box, command palette / completions, queued prompts.
import chalk from 'chalk';
import { h, Box, Text, clean } from './h.mjs';

const MAX_STEPS = 10;

export function PlanPanel({ plan, t }) {
  const steps = plan.steps || [];
  if (!steps.length && !plan.goal) return null;
  let start = 0;
  if (steps.length > MAX_STEPS) {
    const doing = Math.max(0, steps.findIndex((s) => s.status === 'doing'));
    start = Math.max(0, Math.min(doing - 3, steps.length - MAX_STEPS));
  }
  const shown = steps.slice(start, start + MAX_STEPS);
  const rows = shown.map((s, i) => {
    const txt = clean(s.step ?? s.text ?? '');
    switch (s.status) {
      case 'done': return h(Text, { key: i, color: t.faint, strikethrough: true, wrap: 'truncate-end' }, `✔ ${txt}`);
      case 'doing': return h(Text, { key: i, color: t.accent, bold: true, wrap: 'truncate-end' }, `▶ ${txt}`);
      case 'dropped': return h(Text, { key: i, color: t.faint, strikethrough: true, wrap: 'truncate-end' }, `✘ ${txt}`);
      default: return h(Text, { key: i, wrap: 'truncate-end' }, `☐ ${txt}`);
    }
  });
  return h(Box, { flexDirection: 'column', paddingLeft: 2, marginTop: 0 },
    plan.goal ? h(Text, { color: t.dim, wrap: 'truncate-end' }, '⎿  ' + clean(plan.goal)) : null,
    h(Box, { flexDirection: 'column', paddingLeft: 3 },
      start > 0 ? h(Text, { color: t.faint }, `… ${start} earlier`) : null,
      ...rows,
      start + MAX_STEPS < steps.length ? h(Text, { color: t.faint }, `… ${steps.length - start - MAX_STEPS} more`) : null));
}

const OPT_LABEL = {
  yes: 'Yes',
  always: "Yes, and don't ask again",
  no: 'No, and tell ink what to do differently',
};

export function AskBox({ ask, t, width }) {
  const detail = clean(ask.detail).replace(/\s+$/, '');
  let lines = detail ? detail.split('\n') : [];
  const more = lines.length - 16;
  if (more > 0) lines = lines.slice(0, 16);
  return h(Box, { borderStyle: 'round', borderColor: t.warn, flexDirection: 'column', paddingX: 1, marginTop: 1, width: Math.min(width, 120) },
    h(Text, { bold: true, color: t.warn }, clean(ask.title)),
    lines.length ? h(Box, { flexDirection: 'column', paddingLeft: 2, marginTop: 1 },
      h(Text, null, lines.join('\n')),
      more > 0 ? h(Text, { color: t.dim }, `… ${more} more lines`) : null) : null,
    h(Box, { marginTop: 1 }, h(Text, null, 'Do you want to proceed?')),
    ...ask.options.map((o, i) => {
      const sel = i === ask.sel;
      const label = `${i + 1}. ${OPT_LABEL[o] || o}${o === 'no' ? chalk.hex(t.dim)(' (esc)') : ''}`;
      return h(Text, { key: o, color: sel ? t.accent : undefined }, (sel ? '❯ ' : '  ') + label);
    }),
    ask.typing ? h(Box, { borderStyle: 'round', borderColor: t.border, paddingX: 1, marginTop: 1 },
      h(Text, null, '> ' + withCursor(ask.inputText, ask.inputCur, 'what should ink do instead? (enter to send, esc to just deny)', t))) : null);
}

// Text with an inverse block cursor at `cur` (placeholder shown when empty).
export function withCursor(text, cur, placeholder, t) {
  if (!text) {
    if (!placeholder) return chalk.inverse(' ');
    return chalk.inverse(placeholder[0]) + chalk.hex(t.dim)(placeholder.slice(1));
  }
  const before = text.slice(0, cur);
  let at = text.slice(cur, cur + 1);
  const after = text.slice(cur + 1);
  if (at === '' ) return before + chalk.inverse(' ');
  if (at === '\n') return before + chalk.inverse(' ') + '\n' + after;
  return before + chalk.inverse(at) + after;
}

export function Palette({ items, sel, t, width }) {
  const nameW = Math.min(24, Math.max(...items.map((c) => String(c.name).length + 1 + (c.args ? String(c.args).length + 1 : 0))) + 2);
  return h(Box, { flexDirection: 'column', paddingLeft: 2 },
    ...items.map((c, i) => {
      const on = i === sel;
      const name = ('/' + c.name + (c.args ? ' ' + c.args : '')).padEnd(nameW);
      return h(Text, { key: c.name, wrap: 'truncate-end' },
        on ? chalk.hex(t.accent)(name) + chalk.hex(t.text)(clean(c.desc || '')) : chalk.hex(t.dim)(name + clean(c.desc || '')));
    }));
}

export function Completions({ comp, t }) {
  const items = comp.items.slice(0, 8);
  const start = Math.max(0, Math.min(comp.sel - 7, comp.items.length - 8));
  const shown = comp.items.slice(start, start + 8);
  const w = Math.min(50, Math.max(...shown.map((x) => String(x.label ?? x.value).length)) + 2);
  return h(Box, { flexDirection: 'column', paddingLeft: 2 },
    ...shown.map((x, i) => {
      const on = start + i === comp.sel;
      const label = String(x.label ?? x.value).padEnd(w);
      return h(Text, { key: start + i, wrap: 'truncate-end' },
        on ? chalk.hex(t.accent)('+ ' + label) + chalk.hex(t.dim)(clean(x.desc || '')) : chalk.hex(t.dim)('  ' + label + clean(x.desc || '')));
    }),
    items.length < comp.items.length ? h(Text, { color: t.faint }, `  … ${comp.items.length} matches`) : null);
}

export const SHORTCUTS = [
  ['! for bash mode', 'double tap esc to show checkpoints'],
  ['/ for commands', 'shift+tab to cycle modes'],
  ['@ for file paths', 'ctrl+o to expand transcript'],
  ['# to memorize', '\\⏎ or ctrl+enter for newline'],
  ['& background task', 'ctrl+enter / alt+s to steer (busy)'],
  ['esc to interrupt', 'ctrl+c twice to exit'],
];

export function Shortcuts({ t }) {
  return h(Box, { flexDirection: 'column', paddingLeft: 2 },
    ...SHORTCUTS.map(([a, b], i) => h(Text, { key: i, color: t.dim }, a.padEnd(24) + b)));
}

export function Queued({ queue, t }) {
  return h(Box, { flexDirection: 'column' },
    ...queue.map((q, i) => h(Text, { key: i, color: t.dim, wrap: 'truncate-end' }, `  queued: ${clean(q).split('\n')[0]}`)),
    h(Text, { color: t.faint }, '  (sent when the current turn ends · ↑ to edit)'));
}
