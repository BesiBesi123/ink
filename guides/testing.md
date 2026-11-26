Read when writing or fixing tests or setting up a test strategy: unit, property, integration, protocol, e2e (Playwright), contract tests, deterministic tests.

# Testing Playbook

## 0. Top rules
- Write the test with the code, run it immediately, and see it fail for the right reason at least once (break the code or assert first).
- Test behaviour, not implementation: names read like rules (`dealer stands on soft 17`, `rejects bet above table max`).
- Expected values come from the spec or from running a reference (node -e, a second implementation) - never from mental arithmetic or memory.
- Deterministic: inject RNG (seeded) and clock; no real network, no sleeps, no order dependence between tests.
- Cover the edges for every rule: zero, one, max, just over max, empty, duplicate, concurrent, invalid input, failure of a dependency.
- Never weaken, skip or delete a failing test to get green; fix the code or, if the test is wrong, explain why and fix the test.
- Keep tests fast (unit suite in seconds) so they run after every step.

## 1. Layers
- Unit (vitest / node --test): pure engine/rules/math - the bulk of tests.
- Property-based (fast-check): invariants over random inputs - money conserved, total never > 21 after soft adjust, shuffle is a permutation, serialization round-trips.
- Integration: services with a real DB (test schema) or in-memory equivalent; HTTP via Fastify inject / supertest; websockets with a real client against a started server on a random port.
- E2E (Playwright): the main user flow in a real browser (start -> play a round -> see result); also checks console errors.
- Contracts: Hardhat/Foundry unit tests per function and revert reason, fuzz tests for math, invariant tests for balances/supply; fork tests for integrations.
- Simulation: run thousands of rounds/turns to check economy, house edge, balance curves.

## 2. Structure
- Arrange-act-assert; one behaviour per test; small builders/factories for test data (`hand('A', 'K')`, `shoeFrom([...])`).
- Shared fixtures in tests/helpers; no copy-pasted setup blocks.

## Checklist
- [ ] every rule and edge case has a test; all pass
- [ ] at least one property test for core invariants (money, permutations, round-trips)
- [ ] protocol/privacy/integration tests for servers; e2e smoke for the main flow
- [ ] no flaky timing, no network, seeded randomness
