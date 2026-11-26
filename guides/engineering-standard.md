Read when you want the full quality bar for any code you write or review: the rubric a senior full-stack engineer applies (correctness, design, types, errors, security, performance, tests, UX, delivery).

# Engineering Standard - the bar for every change

The user expects the work of a senior full-stack engineer at a top product company: modern stack, clean design,
complete features, verified behaviour. "It runs" is the minimum, not the goal.

## 0. Top rules
- Finish the whole feature: every state (loading, empty, error, success), every rule, every edge case the user would hit. No TODOs, stubs, placeholder data or "left as an exercise" in delivered code.
- Design before code: data model, module boundaries and the API/protocol first; then build in vertical slices that each run end to end.
- Types are the contract: strict TypeScript, no `any`/`as` escapes, validate every external input at the boundary (zod or equivalent).
- Errors are designed: fail loudly in development, recover gracefully for users, never swallow an error silently, never show a stack trace to a user.
- Security by default: server is the authority, secrets never in the client or the repo, inputs validated, outputs encoded, least privilege.
- Every behaviour you add is proven by a test or a real run that you executed; numbers in tests come from code, not memory.
- Readable over clever: small functions with one job, names that say what things are, no dead code, consistent with the existing project style.
- Modern, maintained dependencies only, checked with `npm view <pkg> version` before adding; prefer the platform (fetch, crypto.randomUUID, structuredClone, CSS grid) over a library.
- Leave it deployable: scripts work from a clean clone, README says how to run it, config comes from env with a `.env.example`.

## 1. Correctness
- Read the requirements back as a checklist (key points in the plan) and tick each one with evidence.
- Think through boundaries: 0, 1, many, max, negative, empty string, unicode, concurrent calls, network failure, double click, refresh mid-action, slow device.
- Money, time and randomness are special: integers for money, UTC + explicit timezones for time, injected seeded RNG.
- Idempotency for anything retried (payments, webhooks, game actions).

## 2. Design
- Separate pure logic (rules, calculations, state transitions) from IO (network, DOM, storage, chain). Pure logic gets unit tests; IO stays thin.
- One source of truth per piece of state; derive the rest. Server state (TanStack Query) is not UI state (useState/Zustand).
- Dependencies point inward: UI -> application -> domain. Domain code imports nothing from UI or frameworks.
- Configuration over constants scattered in code: one config module, typed.
- Public interfaces small and documented with types; internal details private.

## 3. Code quality
- Functions < ~40 lines, files < ~300 lines; split by responsibility when they grow.
- No magic numbers: named constants with units (`RESPAWN_MS = 3000`).
- Early returns over nested ifs; exhaustive `switch` on unions (`never` check).
- Comments explain why, not what. JSDoc on exported functions with non-obvious behaviour.
- Consistent formatting (Prettier/Biome) and lint clean (ESLint/oxlint) - run them.

## 4. Errors and resilience
- Typed errors or result objects at module boundaries; user-facing messages written for users.
- Timeouts and retries with backoff on network calls; abort controllers for cancellable requests.
- Log with context (ids, not secrets); structured logs on servers (pino).
- Degrade gracefully: wallet missing, RPC down, WebGL unavailable, offline.

## 5. Security
- Validate and authorize on the server for every request and message; never trust client-sent prices, scores, balances or roles.
- Auth: SIWE (EIP-4361) for wallets with nonce + expiry, httpOnly secure cookies or short-lived tokens.
- Rate-limit public endpoints; size-limit request bodies and websocket messages.
- No secrets in git: `.env` ignored, `.env.example` committed; run the secret scan before pushing.
- Dependencies: no abandoned packages, `npm audit` reviewed, lockfile committed.
- Contracts: see solidity-security.md - checks-effects-interactions, access control, no on-chain randomness from block values.

## 6. Performance
- Measure before optimizing, but design out the obvious: no N+1 requests, no work in render/animation loops that can be done once, pagination for lists.
- Frontend: code-split routes, lazy-load heavy assets (3D models, audio), images in modern formats with sizes, 60 fps target in games.
- Backend: indexes for every query filter, connection pooling, caching with explicit invalidation.

## 7. UX quality
- Every action gives feedback within 100 ms (pressed state, spinner, optimistic update).
- Responsive from 360 px phones to wide desktops; keyboard and screen-reader accessible (labels, focus, contrast).
- Consistent design tokens (spacing scale, colors, radii, type scale); no default-browser-looking UI in a product.
- Errors tell the user what happened and what to do next.

## 8. Tests
- Unit tests for domain logic (every rule and edge case), integration tests for API/protocol, an end-to-end smoke test for the main user flow (Playwright).
- Deterministic: seeded RNG, fixed clocks, no sleeps.
- Tests describe behaviour (`pays 3:2 on a natural`), not implementation.

## 9. Delivery
- README: what it is, how to run, how to test, how to deploy, env vars.
- Scripts: `dev`, `build`, `test`, `lint`, `typecheck` all work.
- CI runs typecheck + lint + tests on every push.
- Commit in logical units with clear messages (when the user wants commits).

## Checklist (definition of done)
- [ ] every key point of the request is implemented and verified
- [ ] typecheck, lint and tests pass; the app/game actually ran
- [ ] no TODO/stub/placeholder/mock data left in delivered paths
- [ ] all UI states handled; responsive; accessible basics
- [ ] inputs validated, server authoritative, no secrets in code
- [ ] README and .env.example up to date
