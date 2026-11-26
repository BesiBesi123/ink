// Syntax highlighting for fenced code blocks. highlight.js is loaded lazily (after first paint) so
// startup stays fast; until it is loaded code is shown unhighlighted.
import chalk from 'chalk';

let hljs = null;
let loading = null;

export function preloadHighlighter() {
  if (!loading) {
    loading = import('highlight.js/lib/common')
      .then((m) => { hljs = m.default || m; })
      .catch(() => { hljs = null; });
  }
  return loading;
}

const ENT = { '&lt;': '<', '&gt;': '>', '&amp;': '&', '&quot;': '"', '&#x27;': "'", '&#39;': "'" };
const unescape = (s) => s.replace(/&(lt|gt|amp|quot|#x27|#39);/g, (m) => ENT[m] || m);

function colourFor(cls, theme) {
  // cls like "hljs-title function_" or "hljs-keyword"
  const names = cls.split(/\s+/).map((c) => c.replace(/^hljs-/, '').replace(/_+$/, ''));
  for (const n of names) if (theme.hl[n]) return theme.hl[n];
  return null;
}

// Convert highlight.js HTML to ANSI, keeping every output line self-contained (styles re-opened per line).
function htmlToAnsi(html, theme) {
  const out = [];
  const stack = [];
  const re = /<span class="([^"]+)">|<\/span>|([^<]+)/g;
  let m;
  while ((m = re.exec(html))) {
    if (m[1] !== undefined) stack.push(colourFor(m[1], theme) || stack[stack.length - 1] || null);
    else if (m[0] === '</span>') stack.pop();
    else {
      const text = unescape(m[2]);
      const col = stack[stack.length - 1];
      out.push(text.split('\n').map((p) => (p && col ? chalk.hex(col)(p) : p)).join('\n'));
    }
  }
  return out.join('');
}

const ALIAS = { js: 'javascript', mjs: 'javascript', cjs: 'javascript', ts: 'typescript', py: 'python',
  sh: 'bash', shell: 'bash', zsh: 'bash', ps1: 'powershell', yml: 'yaml', rs: 'rust', md: 'markdown',
  jsonc: 'json', cs: 'csharp', 'c++': 'cpp', h: 'c', html: 'xml', htm: 'xml', svg: 'xml', console: 'bash' };

export function highlight(code, lang, theme) {
  const plain = () => code.split('\n').map((l) => (l ? chalk.hex(theme.text)(l) : l)).join('\n');
  if (!hljs || !lang) return plain();
  let l = String(lang).toLowerCase().trim().split(/\s+/)[0];
  l = ALIAS[l] || l;
  try {
    if (!hljs.getLanguage(l)) return plain();
    if (code.length > 60000) return plain();
    return htmlToAnsi(hljs.highlight(code, { language: l, ignoreIllegals: true }).value, theme);
  } catch {
    return plain();
  }
}
