Read when building or changing a server: HTTP APIs, websocket/realtime game servers, auth (SIWE), databases, ledgers, config, logging, rate limits.

# Backend & Realtime Playbook (Node + TypeScript)

## 0. Top rules
- The server is authoritative: clients send intents, the server validates (auth, turn, legality, balance, limits) and computes results. Never trust client-sent amounts, scores, cards, prices or roles.
- Validate every input at the edge with a schema (zod) - HTTP bodies, query params, websocket messages, env vars; reject unknown fields and oversize payloads.
- Structure: routes/handlers (thin) -> services (logic, transactions) -> repositories (DB). Domain rules live in pure modules shared with tests.
- Config from env parsed once at startup (zod), fail fast on missing values; `.env.example` lists every variable.
- Consistent error responses `{ error: { code, message } }` with correct HTTP status; never leak stack traces or internals.
- Money and balances: integer/bigint units, every change written to an append-only ledger in a DB transaction; balances are derived or updated in the same transaction.
- Every mutating action is idempotent (request id / round id + sequence) and rate-limited.
- Structured logging (pino) with request/session ids; no secrets or full tokens in logs.

## 1. Auth
- Wallet login with SIWE (EIP-4361): server issues a nonce, wallet signs, server verifies domain/nonce/expiry, then a session (httpOnly, secure, sameSite cookie or short-lived JWT + refresh).
- Authorize per action (is this player in this room, is it their turn), not only "is logged in".

## 2. Realtime (websockets)
- Typed message union in `shared/protocol.ts` with zod schemas; version field in the handshake.
- Per-connection state machine (connecting -> authed -> in-room); heartbeats/ping with timeout; reconnect with session resume and a full state snapshot.
- Send each client only what it may see (privacy snapshot per player); test it.
- Rooms own their state and a serialized action queue (no interleaved mutations); timers are server-side.
- Backpressure: cap message size and rate per connection; drop/kick abusers.

## 3. Data
- PostgreSQL + Drizzle (or the project's ORM); migrations checked in; indexes for every lookup; foreign keys and constraints for invariants (unique, check >= 0).
- Transactions for multi-row changes; SELECT ... FOR UPDATE (or optimistic version columns) for balances.
- Redis for sessions, rate limits, pub/sub between instances, leaderboards.

## 4. Chain integration
- Read deposits from events with confirmations (reorg-safe) and an idempotent processed-tx table; withdrawals queued, signed by a hot wallet with limits, recorded before broadcast and reconciled after.
- Keys from env/secret manager only; never in the repo or client.

## 5. Operations
- Health endpoint, graceful shutdown (stop accepting, finish rooms/flush, close DB).
- Tests: service-level integration tests against a real test DB (or SQLite/pg-mem), protocol tests with a real ws client.

## Checklist
- [ ] every handler validates input and authorizes the action
- [ ] ledger + transactions for money; idempotent mutations
- [ ] privacy: clients only receive what they may see (tested)
- [ ] rate limits, payload limits, heartbeats
- [ ] env validated; .env.example; structured logs; graceful shutdown
