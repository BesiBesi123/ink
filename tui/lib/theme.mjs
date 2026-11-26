// Colour themes (INK_THEME=light|dark, default dark). Values are hex strings usable by both
// Ink's <Text color=…> and chalk.hex().
const dark = {
  name: 'dark',
  accent: '#D97757',      // Claude-ish orange
  accentLight: '#F2B9A0', // shimmer highlight
  text: '#E6E6E6',
  dim: '#8C8C8C',
  faint: '#5F5F5F',
  ok: '#4EBA65',
  err: '#FF6B80',
  warn: '#FFC107',
  info: '#8C8C8C',
  code: '#B1B9F9',        // inline code
  link: '#7AB4E8',
  plan: '#48968C',        // plan mode (teal)
  auto: '#AF87FF',        // auto mode (violet)
  safe: '#8C8C8C',
  userBg: '#373737',
  addBg: '#225C2B',
  delBg: '#7A2936',
  addBgDim: '#2C3F2F',
  delBgDim: '#4A2A30',
  addFg: '#B6E3BC',
  delFg: '#F3B9C1',
  bash: '#FD5DB1',
  border: '#888888',
  hl: { keyword: '#C678DD', built_in: '#E5C07B', type: '#E5C07B', literal: '#56B6C2', number: '#D19A66',
        string: '#98C379', regexp: '#98C379', comment: '#7F848E', meta: '#61AFEF', title: '#61AFEF',
        function: '#61AFEF', attr: '#D19A66', attribute: '#D19A66', variable: '#E06C75', params: '#ABB2BF',
        symbol: '#56B6C2', tag: '#E06C75', name: '#E06C75', selector: '#E06C75', property: '#E06C75',
        subst: '#E06C75', addition: '#98C379', deletion: '#E06C75', section: '#61AFEF', operator: '#56B6C2' },
};

const light = {
  ...dark,
  name: 'light',
  accent: '#C15F3C',
  accentLight: '#E89A7A',
  text: '#1F1F1F',
  dim: '#6B6B6B',
  faint: '#A0A0A0',
  ok: '#2C7A3F',
  err: '#C4283C',
  warn: '#9A6700',
  info: '#6B6B6B',
  code: '#4B5BD6',
  link: '#1F6FC4',
  plan: '#1F7A70',
  auto: '#7A4FD0',
  safe: '#6B6B6B',
  userBg: '#ECECEC',
  addBg: '#C8F0CF',
  delBg: '#F8C9CF',
  addBgDim: '#E3F5E6',
  delBgDim: '#FBE4E7',
  addFg: '#1E4D27',
  delFg: '#6B1A25',
  bash: '#C2187A',
  border: '#999999',
  hl: { keyword: '#A626A4', built_in: '#C18401', type: '#C18401', literal: '#0184BC', number: '#986801',
        string: '#50A14F', regexp: '#50A14F', comment: '#A0A1A7', meta: '#4078F2', title: '#4078F2',
        function: '#4078F2', attr: '#986801', attribute: '#986801', variable: '#E45649', params: '#383A42',
        symbol: '#0184BC', tag: '#E45649', name: '#E45649', selector: '#E45649', property: '#E45649',
        subst: '#E45649', addition: '#50A14F', deletion: '#E45649', section: '#4078F2', operator: '#0184BC' },
};

export function getTheme(name = process.env.INK_THEME) {
  return String(name || '').toLowerCase() === 'light' ? light : dark;
}
