// Markdown -> ANSI string renderer (terminal-width aware). Uses marked's lexer only; rendering is ours
// so headings, lists, tables and code blocks look like Claude Code's output.
import { Lexer } from 'marked';
import chalk from 'chalk';
import stringWidth from 'string-width';
import wrapAnsi from 'wrap-ansi';
import { highlight } from './highlight.mjs';

const wrap = (s, w) => (w > 4 ? wrapAnsi(s, w, { hard: true, trim: false }) : s);

function inline(tokens, t, ctx = {}) {
  if (!tokens) return '';
  let s = '';
  for (const tok of tokens) {
    switch (tok.type) {
      case 'text':
      case 'escape':
        s += tok.tokens ? inline(tok.tokens, t, ctx) : decode(tok.text);
        break;
      case 'strong': s += chalk.bold(inline(tok.tokens, t, ctx)); break;
      case 'em': s += chalk.italic(inline(tok.tokens, t, ctx)); break;
      case 'del': s += chalk.strikethrough(inline(tok.tokens, t, ctx)); break;
      case 'codespan': s += chalk.hex(t.code)(decode(tok.text)); break;
      case 'br': s += '\n'; break;
      case 'link': {
        const label = inline(tok.tokens, t, ctx);
        const plain = (tok.text || '').trim();
        s += chalk.hex(t.link).underline(label);
        if (tok.href && plain !== tok.href && !tok.href.startsWith('mailto:') && `mailto:${plain}` !== tok.href)
          s += chalk.hex(t.dim)(` (${tok.href})`);
        break;
      }
      case 'image': s += chalk.hex(t.dim)(`[image: ${tok.text || tok.href}]`); break;
      case 'html': s += chalk.hex(t.dim)(tok.text); break;
      case 'checkbox': break;
      default: s += tok.raw || tok.text || '';
    }
  }
  return s;
}

function decode(s) {
  return String(s ?? '').replace(/&(lt|gt|amp|quot|#39);/g, (m, e) => ({ lt: '<', gt: '>', amp: '&', quot: '"', '#39': "'" })[e]);
}

function table(tok, t, width) {
  const ncol = tok.header.length;
  const cells = [tok.header.map((c) => chalk.bold(inline(c.tokens, t))), ...tok.rows.map((r) => r.map((c) => inline(c.tokens, t)))];
  const natural = Array.from({ length: ncol }, (_, i) => Math.max(3, ...cells.map((r) => maxLineWidth(r[i] ?? ''))));
  const overhead = ncol * 3 + 1;
  let widths = natural.slice();
  const avail = Math.max(ncol * 4, width - overhead);
  while (widths.reduce((a, b) => a + b, 0) > avail) {
    const i = widths.indexOf(Math.max(...widths));
    if (widths[i] <= 4) break;
    widths[i]--;
  }
  const b = (s) => chalk.hex(t.faint)(s);
  const line = (l, m, r) => b(l + widths.map((w) => '─'.repeat(w + 2)).join(m) + r);
  const pad = (s, w, align) => {
    const gap = Math.max(0, w - stringWidth(s));
    if (align === 'right') return ' '.repeat(gap) + s;
    if (align === 'center') return ' '.repeat(gap >> 1) + s + ' '.repeat(gap - (gap >> 1));
    return s + ' '.repeat(gap);
  };
  const out = [line('┌', '┬', '┐')];
  cells.forEach((row, ri) => {
    const wrapped = widths.map((w, i) => wrap(row[i] ?? '', w).split('\n'));
    const h = Math.max(...wrapped.map((x) => x.length));
    for (let k = 0; k < h; k++) {
      out.push(b('│') + wrapped.map((x, i) => ' ' + pad(x[k] ?? '', widths[i], tok.align[i]) + ' ').join(b('│')) + b('│'));
    }
    if (ri === 0) out.push(line('├', '┼', '┤'));
  });
  out.push(line('└', '┴', '┘'));
  return out;
}

function maxLineWidth(s) {
  return Math.max(0, ...String(s).split('\n').map((l) => stringWidth(l)));
}

const BULLETS = ['•', '◦', '▪'];

function blocks(tokens, t, width, depth = 0) {
  const out = [];
  let prevSpace = true;
  const gap = () => { if (out.length && !prevSpace) out.push(''); };
  for (const tok of tokens) {
    switch (tok.type) {
      case 'space': break;
      case 'heading': {
        gap();
        let s = inline(tok.tokens, t);
        if (tok.depth === 1) s = chalk.bold.underline.hex(t.accent)(s);
        else if (tok.depth === 2) s = chalk.bold(s);
        else if (tok.depth === 3) s = chalk.bold(s);
        else s = chalk.bold.hex(t.dim)(s);
        out.push(...wrap(s, width).split('\n'));
        break;
      }
      case 'paragraph':
        gap();
        out.push(...wrap(inline(tok.tokens, t), width).split('\n'));
        break;
      case 'text':
        out.push(...wrap(tok.tokens ? inline(tok.tokens, t) : decode(tok.text), width).split('\n'));
        break;
      case 'code': {
        gap();
        const code = tok.text.replace(/\n$/, '');
        const lines = highlight(code, tok.lang, t).split('\n');
        for (const l of lines) out.push(...wrap(l, width).split('\n'));
        break;
      }
      case 'blockquote': {
        gap();
        const inner = blocks(tok.tokens, t, width - 2, depth);
        for (const l of inner) out.push(chalk.hex(t.faint)('▎ ') + chalk.italic.hex(t.dim)(l));
        break;
      }
      case 'list': {
        if (depth === 0) gap();
        const start = Number(tok.start) || 1;
        tok.items.forEach((item, i) => {
          let marker = tok.ordered ? `${start + i}.` : BULLETS[depth % BULLETS.length];
          if (item.task) marker = item.checked ? chalk.hex(t.ok)('✔') : '☐';
          const mw = stringWidth(marker) + 1;
          const inner = blocks(item.tokens, t, width - mw, depth + 1);
          inner.forEach((l, k) => out.push((k === 0 ? marker + ' ' : ' '.repeat(mw)) + l));
        });
        break;
      }
      case 'table':
        gap();
        out.push(...table(tok, t, width));
        break;
      case 'hr':
        gap();
        out.push(chalk.hex(t.faint)('─'.repeat(Math.max(3, Math.min(width, 80)))));
        break;
      case 'html':
        gap();
        out.push(...wrap(chalk.hex(t.dim)(tok.text.replace(/\n$/, '')), width).split('\n'));
        break;
      case 'def': break;
      default:
        if (tok.raw) out.push(...wrap(tok.raw.replace(/\n$/, ''), width).split('\n'));
    }
    prevSpace = tok.type === 'space' && depth > 0 ? prevSpace : false;
  }
  return out;
}

export function renderMarkdown(text, theme, width = 80) {
  try {
    const tokens = new Lexer({ gfm: true }).lex(String(text ?? ''));
    return blocks(tokens, theme, Math.max(10, width)).join('\n').replace(/\s+$/, '');
  } catch (e) {
    return String(text ?? '');
  }
}

// Find the end of the longest prefix of streamed markdown that consists of complete blocks: a blank
// line outside any code fence, followed by a line that starts at column 0 (so indented list
// continuations are never cut off). Returns an index (0 = nothing safe yet).
export function safeSplit(text) {
  let inFence = null;
  let safe = 0;
  let i = 0;
  let prevBlank = false;
  let seenContent = false;
  while (i < text.length) {
    const ch = text[i];
    if (prevBlank && !inFence && seenContent && ch !== ' ' && ch !== '\t' && ch !== '\n' && ch !== '\r') safe = i;
    const nl = text.indexOf('\n', i);
    if (nl < 0) break; // last line is incomplete
    const line = text.slice(i, nl);
    const fm = /^\s{0,3}(`{3,}|~{3,})/.exec(line);
    if (fm) {
      if (!inFence) inFence = fm[1];
      else if (fm[1][0] === inFence[0] && fm[1].length >= inFence.length && line.trim().replace(/[`~]/g, '') === '') inFence = null;
    }
    const blank = line.trim() === '';
    if (!blank) seenContent = true;
    prevBlank = blank;
    i = nl + 1;
  }
  return safe;
}
