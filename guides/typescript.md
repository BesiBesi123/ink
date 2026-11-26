# TypeScript projects (Vite, vitest, tsx, Node servers) - setup, pitfalls that compile but are wrong, testing

Read when creating or editing .ts/.tsx code.

## 0. Top rules (work loop)
- Typecheck early and often: `npx tsc --noEmit` after each piece (the harness also runs it after your edits when
  the project has a tsconfig). Don't write 300 lines before the first compile.
- Run one test file while working: `npx vitest run tests/foo.test.ts` (fast) - the whole suite at the end.
- Scratch experiments: put a `.ts` file INSIDE the project (relative imports resolve from the file, not the cwd),
  run it with `npx tsx file.ts`, delete it after. /tmp files can't import project modules.

Pitfalls that type-check but are wrong:
- Numeric enums have reverse mappings: `enum Rank { Two = 2 }` -> `Object.values(Rank)` returns BOTH names and
  numbers (twice the items). Iterate an explicit array (`const RANKS = [Rank.Two, ...] as const`), or use a
  string enum / `as const` object.
- Several enum members with the same value (`Jack = 10, Queen = 10`) are indistinguishable at runtime. Model rank
  and value separately (`rank: 'J'`, `value(rank) = 10`).
- `array.pop()` takes from the END, `shift()` from the front; decide which end is the "top" and test it.
- `sort()` without a comparator sorts numbers as strings; it also mutates in place.
- `==`/truthiness on 0 and "": `if (amount)` is false for 0 - use explicit checks.
- `JSON.stringify` drops undefined and throws on bigint - serialize bigint as a string.
- Floating point: never for money (see real-money-games.md); `Number.EPSILON` comparisons for physics only.
- `as` casts silence the compiler - prefer narrowing (`in`, `typeof`, discriminated unions).

## Arithmetic you state in comments or tests
- Compute, don't recall: 8 decks x 52 = 416, not 520. When a test needs an exact value (hash, total, count),
  derive it by running code (`node -e`, a reference implementation) before hard-coding it.

## Project setup that works
- `"type": "module"`, `"moduleResolution": "bundler"` (Vite) or `"NodeNext"` (Node with explicit .js extensions).
- `strict: true`, `noUnusedLocals` can turn work-in-progress into errors - fix them, don't disable strict.
- Shared code between browser and server: keep it free of DOM and node: imports; put platform code at the edges.

## Checklist
- [ ] `npx tsc --noEmit` clean
- [ ] tests for every rule/branch, with exact expected values computed by code, not by memory
- [ ] no Object.values on numeric enums, no float money, no `as any` to silence errors
