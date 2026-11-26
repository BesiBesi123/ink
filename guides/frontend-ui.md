Read when building or restyling a user interface (screens, menus, HUDs, lobbies, dashboards, forms): professional modern UI, design tokens, layout, states, motion, accessibility, performance.

# Frontend UI Playbook - product-quality interfaces

## 0. Top rules
- Design tokens first: CSS variables (or Tailwind theme) for colors, spacing scale (4/8 px), radii, shadows, type scale, z-index layers. Never hard-code one-off values.
- Every screen has all states designed: loading (skeletons), empty, error (with a retry), success, disabled, and for web3: wallet not connected, wrong network, tx pending/confirmed/failed.
- Responsive from 360 px to wide screens with grid/flex and container queries; test both a phone and a desktop size (playtest page sizes).
- Interactive feedback within 100 ms: hover, pressed, focus-visible states; optimistic updates where safe; never a dead click.
- Accessibility basics: semantic elements (button, not div), labels on inputs, alt text, focus order, visible focus, contrast >= 4.5:1, `prefers-reduced-motion` respected.
- Motion with purpose: 150-300 ms ease-out for UI, springs for playful game UI; animate transform/opacity only.
- Components small and reusable (Button, Card, Modal, Toast, Stat, Avatar); layout components separate from content.
- No default-looking UI: a consistent visual identity (palette, font pairing, iconography - lucide icons), real copy, no lorem ipsum.

## 1. Layout and visual hierarchy
- One primary action per screen, visually dominant; secondary actions quieter.
- Group with spacing before borders; align to a grid; limit to 2 typefaces and ~5 text sizes.
- Dark themes: not pure black (#0b0d12-ish surfaces), elevation by lighter surfaces, accent color used sparingly.

## 2. Game UI / HUD
- HUD readable at a glance: big numbers for what matters (balance, bet, timer), icons + numbers, color + shape (not color alone).
- Juice: number tick-ups, chip/card motion along arcs, subtle screen shake/flash on big wins - see game-design.md for timings.
- Overlay UI (DOM) over the canvas for text-heavy panels; keep the canvas for the game itself.
- Pause/blur handling: stop timers and audio when the tab is hidden (visibilitychange).

## 3. Data and forms
- TanStack Query for server data (caching, retries, invalidation); never fetch in useEffect by hand in React apps.
- Forms: react-hook-form + zod schema shared with the server; inline errors next to fields; disable submit while pending.
- Numbers: Intl.NumberFormat; crypto amounts via formatUnits with sensible decimals; dates via Intl.DateTimeFormat.

## 4. Performance
- Route-level code splitting; lazy-load 3D models, audio and big images; preload what the next screen needs.
- Images: modern formats (webp/avif), explicit width/height; fonts: woff2 with font-display: swap.
- Avoid re-render storms: memoize expensive lists, keep fast-changing game state out of React state (refs or the engine).
- Lighthouse/Web Vitals targets: LCP < 2.5 s, CLS < 0.1, INP < 200 ms.

## Checklist
- [ ] tokens used everywhere; consistent spacing/type
- [ ] loading/empty/error/success (+ wallet/network/tx) states
- [ ] phone and desktop layouts checked
- [ ] keyboard + focus + labels + contrast + reduced motion
- [ ] no console errors; fast first load
