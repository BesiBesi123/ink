import React from 'react';
export { Box, Text, Static } from 'ink';
export const h = React.createElement;
export const Fragment = React.Fragment;

export const expandTabs = (s) => String(s ?? '').replace(/\t/g, '    ');
// Ink cannot lay out raw control characters; keep \n, drop the rest (except ESC used by SGR).
export const clean = (s) => expandTabs(s).replace(/\r\n?/g, '\n').replace(/[\x00-\x08\x0b-\x1a\x1c-\x1f\x7f]/g, '');
