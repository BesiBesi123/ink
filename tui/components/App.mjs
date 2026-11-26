// Root component: <Static> scrollback + the live area at the bottom.
import React, { useSyncExternalStore, useEffect, useState } from 'react';
import { h, Box, Static } from './h.mjs';
import { Item } from './Items.mjs';
import { LiveThinking, LiveAnswer, PendingTool, Spinner, StatusText, Starting } from './Live.mjs';
import { PlanPanel, AskBox, Palette, Completions, Queued, Shortcuts } from './Panels.mjs';
import { InputBox, StatusLine } from './InputBox.mjs';

export function App({ ctrl, theme: t, stdout }) {
  const snap = useSyncExternalStore(ctrl.subscribe, ctrl.getSnapshot);
  const [, setSize] = useState(0);
  useEffect(() => {
    const onResize = () => { setSize((n) => n + 1); ctrl.notify(true); };
    stdout?.on?.('resize', onResize);
    return () => stdout?.off?.('resize', onResize);
  }, [stdout]);

  const width = Math.max(20, (stdout?.columns || snap.width) - 1);
  const rows = stdout?.rows || snap.rows;
  const now = Date.now();

  // Budget for the streaming answer so the live area never exceeds the screen (Ink would clear it).
  let reserved = 6 + (snap.input.text.split('\n').length);
  if (snap.thinking) reserved += 9;
  if (snap.plan) reserved += Math.min(12, (snap.plan.steps?.length || 0) + 2);
  reserved += snap.pending.length * 3 + snap.queue.length + (snap.queue.length ? 1 : 0);
  if (snap.palette.length) reserved += snap.palette.length;
  if (snap.ask) reserved += 30;
  const answerLines = Math.max(3, rows - reserved - 3);

  const live = [];
  if (snap.thinking) live.push(h(LiveThinking, { key: 'th', thinking: snap.thinking, t, width }));
  if (snap.answer) live.push(h(LiveAnswer, { key: 'an', answer: snap.answer, t, width, maxLines: answerLines }));
  for (const p of snap.pending) live.push(h(PendingTool, { key: 'p' + p.id, p, t, tick: snap.tick, now }));
  if (snap.starting) live.push(h(Starting, { key: 'st', snap, t }));
  else if (snap.busy && !snap.ask) live.push(h(Spinner, { key: 'sp', snap, t }));
  if (snap.status && !snap.starting && (snap.busy || snap.status)) live.push(h(StatusText, { key: 'stx', text: snap.status, t }));
  if (snap.busy && snap.plan && !snap.ask) live.push(h(PlanPanel, { key: 'pl', plan: snap.plan, t }));

  let bottom;
  if (snap.ask) {
    bottom = [h(AskBox, { key: 'ask', ask: snap.ask, t, width })];
  } else {
    bottom = [];
    if (snap.queue.length) bottom.push(h(Box, { key: 'q', marginTop: 1 }, h(Queued, { queue: snap.queue, t })));
    bottom.push(h(Box, { key: 'in', marginTop: 1 }, h(InputBox, { input: snap.input, t, width, busy: snap.busy })));
    if (snap.palette.length) bottom.push(h(Palette, { key: 'pal', items: snap.palette, sel: Math.min(snap.paletteSel, snap.palette.length - 1), t, width }));
    else if (snap.completions?.items?.length) bottom.push(h(Completions, { key: 'cmp', comp: snap.completions, t }));
    else if (snap.input.text === '?') bottom.push(h(Shortcuts, { key: 'sc', t }));
    bottom.push(h(StatusLine, { key: 'sl', snap, t, width }));
  }

  return h(React.Fragment, null,
    h(Static, { items: snap.items }, (it) => h(Item, { key: it.key, it, t, width })),
    h(Box, { flexDirection: 'column', width }, ...live, ...bottom));
}
