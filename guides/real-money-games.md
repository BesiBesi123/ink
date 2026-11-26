# Real-money and wagering games (casino, cards, dice, crash, PvP stakes) - fairness, money math, server authority

Read before writing game logic where players bet, win or lose value (CRO, tokens, chips that can be cashed out).
A bug here is not a glitch, it is lost money or a lawsuit. Practice mode may share the engine, but the engine
must be written to the cash-mode standard.

## 0. Top rules
- Money is integers: bigint wei or integer chips/cents; payouts with integer math and a documented rounding rule (3:2 = bet * 3n / 2n). Never float math on money.
- Outcomes come from a provably fair RNG: committed server seed (sha256 shown before the bet) + client seed + nonce -> HMAC-SHA256; Fisher-Yates; no modulo bias; the engine takes the RNG as a parameter with NO Math.random default; ship verify() and a test.
- The server is authoritative: clients send intents, the server validates turn/legality/balance/limits; hidden cards and seeds never reach a client early (privacy test).
- Bets are debited before dealing and settled exactly once; every action is idempotent; a crash mid-round is refundable from the ledger.
- Implement the full rule set (blackjack: hit, stand, double, split + resplit, insurance/even money, surrender, H17/S17, 3:2, cut card) or list what is out of scope to the user.
- Prove it: deterministic decks per rule path, money-conservation tests, and a simulation whose house edge lands in the expected range.

## 1. Money is integers, never floats
- Store and compute every amount in the smallest unit as an integer: `bigint` wei for on-chain values, or integer
  chips/cents off-chain. `0.1 + 0.2 !== 0.3`; `bet * 1.5` on a float can leak fractions.
- Payout ratios as integer math with explicit rounding: blackjack 3:2 = `bet * 3n / 2n` (decide and document who gets
  the remainder: usually round down = house keeps dust, or forbid odd bets in that unit).
- Never `Number(wei)`, `parseFloat` or `toFixed` on balances. Format only at the UI edge.
- Every function that moves money returns the exact deltas; a ledger sums them. Test: sum of all deltas in a round
  == -(house net). Conservation tests catch most payout bugs.

## 2. Randomness: provably fair, never Math.random
- Anything that decides an outcome comes from a seeded, verifiable RNG: server seed (secret, committed as
  `sha256(serverSeed)` BEFORE the bet), client seed (player-chosen), nonce (round counter).
  `HMAC_SHA256(serverSeed, clientSeed + ":" + nonce)` -> bytes -> numbers.
- Turn bytes into a range without modulo bias (rejection sampling: drop values >= the largest multiple of n).
- Shuffle = Fisher-Yates driven by that RNG. The engine takes the RNG as a parameter and has NO default to
  Math.random (a default is how practice code ends up in cash mode).
- After the round/shoe, reveal serverSeed; ship a `verify(serverSeed, clientSeed, nonce)` function and a test that
  re-derives a known round byte-for-byte.
- Rotate the server seed per shoe/session; never reuse a revealed seed.

## 3. Server authority
- The client sends intents (`bet 100`, `hit`, `stand`); the server validates (is it your turn, is the action legal,
  do you have the balance, is the bet within table limits) and computes results. Never trust a client total,
  card, payout or balance.
- Hidden information (dealer hole card, deck order, other players' cards) never leaves the server until revealed.
  Write a privacy test: serialize every message a client can receive and assert no hidden card/seed is in it.
- Idempotency: each action carries a round id + sequence; a replayed or double-clicked action does nothing twice.
- Debit the bet atomically before dealing; credit once at settlement; a crash mid-round must be resumable or
  refundable from the ledger, never double-paid.

## 4. Game rules completeness (cards)
- Blackjack: hit, stand, double (rules: any two / 9-11), split (re-split limit, split aces get one card), insurance
  and even money when dealer shows an ace, surrender (late/early), dealer hits/stands soft 17 (H17/S17 changes the
  edge ~0.2%), blackjack pays 3:2 (6:5 is a different game), natural vs 21-after-split (split 21 is not blackjack),
  reshuffle at the cut card (penetration). Write each rule as a named, tested function.
- Poker: side pots, all-in, odd-chip rule, split pots, kicker comparisons - test with explicit hands.
- Put the rule variant in a config object (decks, H17/S17, DAS, resplit, payout) and test both variants.

## 5. Tests that matter
- Deterministic decks: build shoes from an explicit order (top first) to test every rule path.
- Simulation: play 1e5-1e6 rounds with basic strategy against the engine; the house edge must land near the known
  value for the rules (blackjack 6 decks S17 DAS 3:2 ~0.4-0.6%). A wildly different edge = a rules/payout bug.
- Money conservation (see 1), fairness verification (see 2), privacy (see 3), replay/idempotency (see 3).

## Checklist
- [ ] all amounts are integers/bigint; no float math on money; rounding rule documented and tested
- [ ] outcome RNG is HMAC-seeded with commit/reveal; no Math.random default anywhere in engine code
- [ ] modulo-bias-free range mapping; Fisher-Yates
- [ ] server validates every action and balance; hidden info never sent early (privacy test)
- [ ] actions idempotent; bet debited before deal, settled once
- [ ] every rule of the game implemented or explicitly listed as out of scope to the user
- [ ] simulation test: house edge within the expected range
