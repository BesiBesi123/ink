Read when starting a new project, game or app from scratch (or a big new feature): how to go from the request to a spec, architecture, milestone plan and a professional scaffold.

# New Project Blueprint

## 0. Top rules
- Before any code: write docs/SPEC.md - goal, users, features (must/should/later), rules, data model, architecture, API/protocol, milestones. Keep it short (1-3 pages) and update it when decisions change.
- If the project relates to an existing one of the user's (same studio, same backend, a sibling game), read that project first and mirror its stack, structure and conventions.
- Plan in vertical slices: each milestone runs end to end (UI -> logic -> server -> storage) and is demoable; never "all the backend, then all the frontend".
- Scaffold with the full toolchain on day one: strict TypeScript, lint, format, test runner, scripts, .gitignore, .env.example, README. Then the first slice.
- Pick the stack from stack.md; for an existing project, its stack wins.
- Each plan step is small enough to build and test in one go (one module, one screen, one endpoint) and has a way to verify it.
- At the end of every milestone: typecheck + lint + tests + a real run (playtest for games), then update SPEC.md and the plan.

## 1. Spec (docs/SPEC.md)
```
# <Name>
Goal: one sentence. Users: who plays/uses it.
## Features
Must: ...   Should: ...   Later: ...
## Rules / mechanics      (exact numbers: payouts, timers, limits)
## Data model             (entities, fields with types, ownership: client/server/chain)
## Architecture           (modules and how they talk; diagram in text)
## API / protocol         (endpoints or message types with payload shapes)
## Security & fairness    (who is authoritative, what is validated, secrets)
## Milestones             (M1 playable core, M2 ..., each with acceptance criteria)
```

## 2. Architecture defaults
- Monorepo-lite for full-stack games: `src/engine` (pure rules, no IO), `src/shared` (protocol types + zod schemas used by client and server), `src/client` (UI), `server/` (authoritative server), `contracts/` (if on-chain), `tests/`.
- The engine is pure and deterministic (injected RNG and clock) so the server and practice mode run the same code.
- The protocol is typed once in `shared` and validated at both ends.
- State machine for game/round flow (explicit states and allowed transitions), not booleans.

## 3. Milestone template (games)
1. M1 core loop: engine rules + tests; a minimal playable screen locally.
2. M2 feel and UI: real UI, animation, sound hooks, responsive layout, all states.
3. M3 server/online: authoritative server, protocol, reconnect, persistence.
4. M4 economy/chain: wallet, deposits/withdrawals, ledger, contract integration (testnet first).
5. M5 polish and delivery: e2e smoke test, performance pass, README, deploy config.

## 4. Scaffold checklist
- [ ] package.json scripts: dev, build, preview, test, lint, typecheck, format
- [ ] tsconfig strict (+ noUncheckedIndexedAccess), path aliases if useful
- [ ] ESLint flat config (typescript-eslint) or Biome/oxlint; Prettier or Biome format
- [ ] vitest configured; first test passes
- [ ] .gitignore (node_modules, dist, .env, coverage), .env.example, README with run instructions
- [ ] git init + first commit when the user wants git
