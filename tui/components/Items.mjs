// Renderers for finished scrollback items (printed once via <Static>).
import chalk from 'chalk';
import stringWidth from 'string-width';
import wrapAnsi from 'wrap-ansi';
import { h, Box, Text, clean } from './h.mjs';
import { renderMarkdown } from '../lib/markdown.mjs';
import { DiffBlock } from './Diff.mjs';
import { fmtK, fmtSecs } from '../lib/format.mjs';

const MAX_OUTPUT_LINES = 400;

export function Welcome({ hello, t, width }) {
  const w = Math.min(width - 2, 76);
  const tips = (hello.tips || []).slice(0, 3);
  const model = [hello.model, hello.profile, hello.effort].filter(Boolean).join(' · ');
  const cwd = String(hello.cwd || process.cwd());
  return h(Box, { flexDirection: 'column', marginBottom: 0 },
    h(Box, { borderStyle: 'round', borderColor: t.accent, paddingX: 1, flexDirection: 'column', width: w },
      h(Text, null, h(Text, { color: t.accent }, '✻ '), h(Text, { bold: true }, 'ink'),
        h(Text, { color: t.dim }, hello.version ? `  protocol v${hello.version}` : '')),
      h(Text, null, ' '),
      h(Text, { color: t.dim, italic: true }, '  /help for help, ? in the prompt for shortcuts'),
      h(Text, null, ' '),
      model ? h(Text, { color: t.dim }, '  model: ', h(Text, { color: t.text }, model)) : null,
      hello.ctx ? h(Text, { color: t.dim }, '  ctx:   ', h(Text, { color: t.text }, `${fmtK(hello.ctx)} tokens`)) : null,
      h(Text, { color: t.dim, wrap: 'truncate-middle' }, '  cwd:   ', h(Text, { color: t.text }, cwd))),
    tips.length ? h(Box, { flexDirection: 'column', marginTop: 1, paddingLeft: 1 },
      h(Text, { color: t.dim }, 'Tips for getting started:'),
      ...tips.map((tip, i) => h(Box, { key: i, flexDirection: 'row' },
        h(Box, { width: 4, flexShrink: 0 }, h(Text, { color: t.dim }, ` ${i + 1}.`)),
        h(Text, { color: t.dim }, clean(tip))))) : null);
}

export function UserPrompt({ text, t, width, dim }) {
  const lines = clean(text).split('\n');
  const inner = Math.max(10, width - 4);
  const out = [];
  lines.forEach((l, i) => {
    for (const part of wrapAnsi(l, inner, { hard: true, trim: false }).split('\n')) {
      const prefix = out.length === 0 ? '> ' : '  ';
      const s = prefix + part;
      out.push(chalk.bgHex(t.userBg).hex(dim ? t.dim : t.text)(s + ' '.repeat(Math.max(0, inner + 2 - stringWidth(s)))));
    }
  });
  return h(Text, null, out.join('\n'));
}

export function labelText(label, t) {
  const s = clean(label);
  const i = s.indexOf('(');
  if (i <= 0) return chalk.bold(s);
  return chalk.bold(s.slice(0, i)) + chalk.hex(t.dim)(s.slice(i));
}

export function ToolRow({ it, t, width }) {
  const dot = chalk.hex(it.ok ? t.ok : t.err)('●');
  const head = h(Text, { wrap: 'truncate-end' }, `${dot} ${labelText(it.label, t)}`);
  const body = [];
  if (it.diffs?.length) {
    it.diffs.forEach((d, i) => body.push(h(DiffBlock, { key: 'd' + i, d, t, width, first: i === 0 })));
    if (!it.ok && it.summary) body.push(h(Text, { key: 's', color: t.err }, '     ' + clean(it.summary)));
  } else {
    const sum = clean(it.summary || (it.ok ? 'done' : 'failed')).split('\n')[0];
    body.push(h(Text, { key: 's', wrap: 'truncate-end' }, chalk.hex(t.dim)('  ⎿  ') + chalk.hex(it.ok ? t.dim : t.err)(sum)));
    const prev = (it.preview || []).slice(0, 6).map((l) => clean(l));
    if (prev.length) body.push(h(Box, { key: 'p', flexDirection: 'column', paddingLeft: 5 },
      ...prev.map((l, i) => h(Text, { key: i, color: t.dim, wrap: 'truncate-end' }, l || ' '))));
  }
  return h(Box, { flexDirection: 'column' }, head, ...body);
}

export function Md({ it, t, width }) {
  const body = renderMarkdown(it.text, t, width - 3);
  return h(Box, { flexDirection: 'row' },
    h(Box, { width: 2, flexShrink: 0 }, h(Text, null, it.first ? chalk.hex(t.text)('●') : ' ')),
    h(Box, { flexDirection: 'column', flexGrow: 1 }, h(Text, null, body)));
}

export function Notice({ it, t }) {
  const col = it.level === 'error' ? t.err : it.level === 'warn' ? t.warn : t.info;
  return h(Text, { color: col }, clean(it.text));
}

export function Output({ it, t }) {
  let lines = clean(it.text).replace(/\s+$/, '').split('\n');
  const more = lines.length - MAX_OUTPUT_LINES;
  if (more > 0) lines = lines.slice(0, MAX_OUTPUT_LINES);
  return h(Box, { flexDirection: 'column', paddingLeft: 2 },
    h(Text, null, lines.join('\n') || ' '),
    more > 0 ? h(Text, { color: t.dim }, `… ${more} more lines`) : null);
}

export function Check({ it, t }) {
  return h(Text, null, it.ok
    ? chalk.hex(t.ok)(`✔ ${clean(it.name)}`) + chalk.hex(t.dim)(it.summary ? ` ${clean(it.summary)}` : '')
    : chalk.hex(t.err)(`✗ ${clean(it.name)}`) + (it.summary ? ' ' + chalk.hex(t.err)(clean(it.summary)) : ''));
}

export function Thought({ it, t }) {
  return h(Text, { color: t.dim, italic: true }, `∴ Thought for ${fmtSecs(it.secs)}`, h(Text, { color: t.faint, italic: false }, '  (ctrl+o to expand)'));
}

export function AskResult({ it, t }) {
  const txt = it.choice === 'yes' ? 'Allowed' : it.choice === 'always' ? "Allowed (won't ask again)" : 'Denied';
  return h(Text, null, (it.choice === 'no' ? chalk.hex(t.err)('✗ ') : chalk.hex(t.ok)('✔ ')) + chalk.hex(it.choice === 'no' ? t.err : t.dim)(txt + ': ' + clean(it.title)) +
    (it.text ? chalk.hex(t.dim)(' — ' + clean(it.text)) : ''));
}

export function Steer({ it, t }) {
  return h(Text, { color: t.dim }, '  ↳ steer: ', h(Text, { color: t.text }, clean(it.text)));
}

export function Transcript({ it, t, width }) {
  const log = it.log;
  const rule = (s) => chalk.hex(t.faint)(`── ${s} ` + '─'.repeat(Math.max(3, Math.min(width, 100) - stringWidth(s) - 4)));
  const parts = [h(Text, { key: 'h' }, rule(`transcript · turn ${log.id ?? ''}`))];
  if (log.prompt) parts.push(h(Text, { key: 'p', color: t.dim }, '> ' + clean(log.prompt).split('\n')[0]));
  log.entries.forEach((e, i) => {
    if (e.type === 'thinking') {
      parts.push(h(Box, { key: i, flexDirection: 'column', marginTop: 1 },
        h(Text, { color: t.dim, italic: true }, `∴ Thinking (${fmtSecs(e.secs)})`),
        h(Box, { paddingLeft: 2 }, h(Text, { color: t.dim, italic: true }, clean(e.text).trim() || '(empty)'))));
    } else if (e.type === 'tool') {
      let lines = clean(e.output).replace(/\s+$/, '').split('\n');
      const more = lines.length - MAX_OUTPUT_LINES;
      if (more > 0) lines = lines.slice(0, MAX_OUTPUT_LINES);
      parts.push(h(Box, { key: i, flexDirection: 'column', marginTop: 1 },
        h(Text, null, `${chalk.hex(e.ok ? t.ok : t.err)('●')} ${labelText(e.label, t)}`),
        h(Text, { color: t.dim }, '  ⎿  ' + clean(e.summary || '')),
        h(Box, { paddingLeft: 5 }, h(Text, null, lines.join('\n') || ' ')),
        more > 0 ? h(Text, { color: t.dim }, `     … ${more} more lines`) : null));
    }
  });
  parts.push(h(Text, { key: 'f' }, rule('end of transcript')));
  return h(Box, { flexDirection: 'column' }, ...parts);
}

export function Item({ it, t, width }) {
  let body;
  switch (it.kind) {
    case 'welcome': body = h(Welcome, { hello: it.hello, t, width }); break;
    case 'user': body = h(UserPrompt, { text: it.text, t, width, dim: it.dim }); break;
    case 'tool': body = h(ToolRow, { it, t, width }); break;
    case 'diff': body = h(DiffBlock, { d: it, t, width }); break;
    case 'md': body = h(Md, { it, t, width }); break;
    case 'notice': body = h(Notice, { it, t }); break;
    case 'output': body = h(Output, { it, t }); break;
    case 'check': body = h(Check, { it, t }); break;
    case 'thought': body = h(Thought, { it, t }); break;
    case 'askResult': body = h(AskResult, { it, t }); break;
    case 'steer': body = h(Steer, { it, t }); break;
    case 'transcript': body = h(Transcript, { it, t, width }); break;
    default: body = h(Text, { color: t.dim }, `[${it.kind}]`);
  }
  // Blank line between blocks, like Claude Code (results of a tool sit flush under it).
  const tight = it.kind === 'diff' || it.kind === 'steer';
  return h(Box, { key: it.key, flexDirection: 'column', marginTop: tight ? 0 : 1, width }, body);
}
