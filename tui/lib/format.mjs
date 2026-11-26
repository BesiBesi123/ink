export function fmtK(n) {
  n = Number(n) || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace(/\.0$/, '') + 'M';
  if (n >= 1000) return (n / 1000).toFixed(n >= 1e5 ? 0 : 1).replace(/\.0$/, '') + 'k';
  return String(Math.round(n));
}

export function fmtSecs(s) {
  s = Math.max(0, Math.round(Number(s) || 0));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

// Fuzzy subsequence score: higher is better, -1 = no match.
export function fuzzyScore(query, target) {
  const q = query.toLowerCase();
  const t = target.toLowerCase();
  if (!q) return 1;
  if (t.startsWith(q)) return 1000 - t.length;
  const idx = t.indexOf(q);
  if (idx >= 0) return 500 - idx - t.length;
  let ti = 0, score = 0, last = -1;
  for (const c of q) {
    const f = t.indexOf(c, ti);
    if (f < 0) return -1;
    score += f === last + 1 ? 5 : 1;
    last = f;
    ti = f + 1;
  }
  return score - t.length;
}

export const VERBS = ['Forging', 'Pondering', 'Brewing', 'Tinkering', 'Inking', 'Scribbling', 'Musing',
  'Conjuring', 'Whittling', 'Percolating', 'Noodling', 'Cogitating', 'Crafting', 'Mulling', 'Simmering',
  'Sketching', 'Puzzling', 'Hatching', 'Weaving', 'Kneading', 'Distilling', 'Untangling', 'Calibrating',
  'Marinating', 'Spelunking', 'Wrangling', 'Doodling', 'Churning'];

export function pickVerb(prev) {
  let v;
  do v = VERBS[Math.floor(Math.random() * VERBS.length)]; while (v === prev && VERBS.length > 1);
  return v;
}

export const SPIN_FRAMES = ['·', '✢', '✳', '✶', '✻', '✽', '✻', '✶', '✳', '✢'];
