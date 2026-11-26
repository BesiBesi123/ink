Read when choosing technologies, libraries or versions for a new project or feature: the modern default stack for web apps, browser games, realtime servers and EVM dapps, and how to check versions.

# Modern Stack Catalog (defaults; an existing project's stack always wins)

## 0. Top rules
- In an existing project use what it already uses (check package.json and imports) - never mix in a second framework, ORM or state library.
- Before adding or pinning any dependency: `npm view <pkg> version` (latest) and `npm view <pkg> time.modified` (maintained?). Never write versions from memory.
- Prefer few, well-maintained, typed dependencies; prefer the platform (fetch, Web Crypto, structuredClone, Intl, CSS grid/container queries) over a library.
- TypeScript strict everywhere, ESM everywhere (`"type": "module"`), Node LTS (>= 22).
- One state approach per kind: server state -> TanStack Query; client/UI state -> component state or Zustand; game state -> the engine's own state machine.
- Validate data crossing a boundary with zod (API bodies, websocket messages, env vars, localStorage).

## 1. Frontend
- Build: Vite + TypeScript. Apps/dashboards/menus: React 19. Pure canvas/3D games: vanilla TS + three.js, or react-three-fiber + drei when the game lives inside a React UI.
- Styling: Tailwind CSS v4 (CSS-first config) or CSS modules with design tokens as CSS variables; shadcn/ui-style components (Radix primitives) for accessible dialogs, menus, tooltips.
- Animation: Motion (motion.dev, formerly framer-motion) for UI; GSAP or the game loop for in-game tweens.
- Routing: React Router or TanStack Router. Forms: react-hook-form + zod.
- Audio: Howler.js or Web Audio API directly. Physics: Rapier (WASM) for 3D, matter.js for 2D.

## 2. Web3 (EVM, Cronos chain id 25 / testnet 338)
- wagmi + viem (see web3-frontend.md for the current wagmi major's API) with TanStack Query; wallet modal: Reown AppKit or RainbowKit.
- ethers v6 only where the project already uses it.
- Wallet auth: SIWE (EIP-4361) message signed by the wallet, verified on the server with a nonce.
- Amounts as bigint wei everywhere; format with viem's formatUnits only for display.

## 3. Backend / realtime
- Node LTS + TypeScript, run with tsx in dev, compiled or tsx in prod.
- HTTP: Fastify (schemas, speed) or Hono (tiny, runs everywhere). Realtime: ws (raw, light) or Socket.IO (rooms, reconnection) - server-authoritative.
- Database: PostgreSQL + Drizzle ORM (typed, SQL-like, migrations); SQLite (better-sqlite3) for small/self-hosted; Redis for sessions, rate limits, pub/sub, leaderboards.
- Logging: pino. Config: env vars parsed by zod at startup (fail fast). Jobs: BullMQ on Redis when needed.

## 4. Smart contracts
- Solidity 0.8.x (latest patch the toolchain supports), OpenZeppelin Contracts v5.
- Hardhat (the project's version; new projects: check the latest major) with ethers/viem; Foundry (forge) for fuzz and invariant tests.
- Slither for static analysis; deploy scripts idempotent and network-aware; verify sources on the explorer.

## 5. Testing and quality
- Vitest (unit/integration), Playwright (e2e and visual checks), fast-check (property tests for rules/math), Testing Library for React.
- Lint/format: ESLint flat config + typescript-eslint, or Biome; oxlint as a fast extra.
- CI: GitHub Actions - install, typecheck, lint, test, build.

## 6. Hosting
- Static frontends: Cloudflare Pages, Vercel or Netlify. Node servers with websockets: Render, Fly.io or Railway. Postgres: Neon/Supabase/Render. Assets: a CDN.
