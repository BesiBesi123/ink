Read when structuring or debugging JS/three.js game code: game loop, state machines, entities, events, RNG, save/load, tests with node --test, three.js performance and GPU memory leaks.

# JS / three.js Game Architecture Playbook

## 0. Core rules
- Split every feature into PURE LOGIC (`js/world/foo.js`: plain data in -> data out, no DOM, no THREE) and VIEW (`foo3d.js`: reads state, builds/updates meshes). Logic is tested with `node --test`; view is checked with probes/screenshots.
- Pure functions take `rand` and `now` as parameters (never call `Math.random()`/`Date.now()` inside logic) -> deterministic tests.
- State is plain JSON-serializable objects (no class instances, Maps, Sets, THREE objects in saved state).
- One source of truth per value. View caches are derived and rebuildable.
- Prefer direct function calls; add an event bus only for one-to-many notifications across modules.
- ES modules with explicit imports; shared helpers (rng, math) in tiny leaf modules to avoid import cycles.
- Use `??` for numeric defaults, not `||`: `(t.boldness || 50)` turns a real 0 into 50. Write `(t.boldness ?? 50)`.

## 1. Game loop
Variable dt is fine for animation/non-physics. Clamp it (tab switches give huge dt):
```js
let prev = performance.now();
function frame(now) {
  requestAnimationFrame(frame);
  const dt = Math.min(0.05, Math.max(0, (now - prev) / 1000)); // clamp 50 ms
  prev = now;
  update(dt); render();
}
requestAnimationFrame(frame);
```
Fixed timestep for physics, collisions, deterministic sims, networked/replayable logic:
```js
const STEP = 1 / 60; let acc = 0, prev = performance.now();
function frame(now) {
  requestAnimationFrame(frame);
  acc += Math.min(0.25, (now - prev) / 1000); prev = now; // clamp avoids spiral of death
  while (acc >= STEP) { savePrev(state); simulate(state, STEP); acc -= STEP; }
  const alpha = acc / STEP;                    // 0..1 between last two sim states
  renderInterpolated(prevState, state, alpha); // pos = lerp(prev.pos, cur.pos, alpha)
}
```
- Pitfall: per-frame lerp `x += (t-x)*0.1` is FPS dependent -> use `x += (t-x)*(1-Math.exp(-rate*dt))`.
- Pitfall: timers via `setTimeout` desync with paused game -> keep timers in state, decrement by dt.
- Pitfall: background tab stops rAF; long-absence logic must use wall clock (`Date.now()` passed in) and cap catch-up (e.g. simulate at most 8 h, in coarse ticks).
- Hit-stop / slow-mo: multiply dt by `timeScale` for sim; keep UI/camera on real dt.

## 2. State machines
Use explicit states for anything with modes (creature AI, player, UI flows, tx flows):
```js
const FSM = {
  idle:   { enter(e){ e.t = 0; }, update(e, dt, ctx){ if (ctx.hunger < 30) return 'seekFood'; } },
  seekFood:{ update(e, dt, ctx){ if (e.atFood) return 'eat'; } },
  eat:    { enter(e){ e.t = 0; }, update(e, dt){ if ((e.t += dt) > 2) return 'idle'; } },
};
export function stepFsm(e, dt, ctx) {
  const next = FSM[e.state].update(e, dt, ctx);
  if (next && next !== e.state) { FSM[e.state].exit?.(e); e.state = next; FSM[next].enter?.(e); }
}
```
- Store `state` as a string in data (serializable). Log transitions in debug mode.
- Add hysteresis (enter < 30, leave > 80) and minimum dwell times to prevent flip-flopping.
- Pitfall: booleans `isWalking && !isEating && ...` combos -> impossible states. Fix: one `state` field.

## 3. Entities without over-engineering
- < ~50 entity kinds: plain objects + per-kind update functions. No ECS framework.
- Many identical things (particles, grass, coins): struct-of-arrays (Float32Array for x,y,z,vx...) + InstancedMesh.
- Composition via optional fields: `{ id, kind, pos:{x,z}, health?, ai? }`; systems iterate entities that have the field.
- Stable string ids (`'toy' + now.toString(36) + rand`), never array indices as ids in saved data.
- Pitfall: removing from an array while iterating forward -> iterate backwards or swap-remove: `a[i] = a[a.length-1]; a.pop();`.

## 4. Events vs direct calls
- Direct call when there is one obvious receiver. Easier to follow and grep.
- Event bus for broadcast (`'arrived'`, `'coinEarned'` consumed by sound, UI, achievements).
```js
export function createBus() {
  const m = new Map();
  return {
    on(t, f) { (m.get(t) ?? m.set(t, new Set()).get(t)).add(f); return () => m.get(t).delete(f); },
    emit(t, p) { for (const f of [...(m.get(t) ?? [])]) { try { f(p); } catch (e) { console.error(t, e); } } },
  };
}
```
- Always keep the unsubscribe function and call it on teardown (leak source #1).
- Don't mutate state from many listeners in unknown order; listeners should react (sound, UI), logic stays in one place.

## 5. Deterministic RNG
```js
export function rngFrom(seed) { // mulberry32
  let a = seed >>> 0;
  return () => { a = (a + 0x6D2B79F5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}
const int = (rand, n) => Math.floor(rand() * n);            // 0..n-1
const pick = (rand, arr) => arr[Math.floor(rand() * arr.length)];
```
- Seed per subsystem (`rngFrom(hash(worldSeed + ':weather'))`) so adding a roll in one system doesn't shift others.
- Hash strings to seeds (e.g. FNV-1a) for "same day -> same weather": `seed = hash(dayKey)`.
- Never use seeded/`Math.random` RNG for anything with real value onchain (see solidity-security.md).
- Weighted pick: guard `total <= 0` and return a fallback.

## 6. Save / load with versioned migration
```js
export const SAVE_VERSION = 3;
const MIGRATIONS = {          // from version N to N+1
  1: (s) => ({ ...s, bag: s.bag ?? [] }),
  2: (s) => ({ ...s, coins: Math.max(0, s.coins | 0), v: undefined }),
};
export function serialize(S) { return JSON.stringify({ v: SAVE_VERSION, savedAt: Date.now(), state: S }); }
export function parse(text) {
  let o; try { o = JSON.parse(text); } catch { return null; }
  let v = o?.v ?? 1, s = o?.state ?? o;
  if (!s || typeof s !== 'object' || v > SAVE_VERSION) return null; // newer save: refuse, don't corrupt
  while (v < SAVE_VERSION) s = MIGRATIONS[v++](s);
  return validate(s) ? s : null;   // type-check required fields, clamp numbers, default arrays
}
```
- Validate and clamp on load (imports are untrusted: NaN, negative coins, huge arrays). Cap array lengths.
- Write atomically: serialize fully, then one `localStorage.setItem` inside try/catch (quota errors, private mode).
- Keep last-good backup key (`save.bak`) before overwriting.
- Test each migration with a fixture save file from that version.
- Pitfall: adding a field and only defaulting it in UI code -> old saves crash elsewhere. Fix: default in `parse`/migration.

## 7. Testing game logic with node --test
```js
// tests/economy.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { printToy } from '../js/world/economy.js';
import { rngFrom } from '../js/world/rng.js';

test('printing costs coins and materials', () => {
  const S = { bag: ['printer3d'], coins: 10, buildMaterials: 5, toys: [] };
  const { S: out, toy, error } = printToy(S, rngFrom(1), 1700000000000);
  assert.equal(error, null);
  assert.equal(out.coins, 6);
  assert.ok(toy.price >= 15 && toy.price < 25);
  assert.equal(S.coins, 10, 'input state not mutated');
});

test('invariants hold over many seeds', () => {
  for (let seed = 0; seed < 200; seed++) { /* run sim; assert coins >= 0, arrays capped */ }
});
```
- Run: `node --test "tests/*.test.mjs"` (quote the glob on Windows). Single file: `node --test tests/x.test.mjs`. Filter: `--test-name-pattern="printing"`.
- Test types: unit (one function), invariant sweep (many seeds, assert bounds), regression (exact bug input), golden (seeded output snapshot).
- Pass `now` explicitly; never depend on real time or test order.
- Logic modules must not import `three` or touch `window`/`document`; if they do, tests fail on import. Guard browser-only code: `typeof window !== 'undefined'`.
- Browser/render checks: separate probe scripts (CDP/puppeteer-style) that load the page, read `window.__world` state, check console errors and `renderer.info`.

## 8. three.js performance (r161)
Measure first: `renderer.info.render.calls`, `.triangles`, `renderer.info.memory.geometries/.textures`. Log every few seconds in debug mode.
Budgets (mid-range laptop / phone): draw calls < 200 (phone < 100), triangles < 500k (phone < 200k), textures mostly <= 1024^2.

Draw calls:
- Same geometry many times -> `InstancedMesh`:
```js
const mesh = new THREE.InstancedMesh(geo, mat, COUNT);
const m = new THREE.Matrix4();
for (let i = 0; i < COUNT; i++) { m.compose(pos[i], quat[i], scl[i]); mesh.setMatrixAt(i, m); }
mesh.instanceMatrix.needsUpdate = true;
mesh.computeBoundingSphere(); // after moving instances, or culling uses stale bounds
// per-instance color: mesh.setColorAt(i, color); mesh.instanceColor.needsUpdate = true;
```
- Static different geometries with same material -> merge: `mergeGeometries([...])` from `examples/jsm/utils/BufferGeometryUtils.js` (`three/addons/...` with an importmap; vendor a copy next to `three.module.js` if the project copies three locally; old name `mergeBufferGeometries` was removed). Apply each mesh's world matrix to a cloned geometry before merging.
- Share materials: one material per look, created once in a cache (`resources.js`-style `standardMaterial(color)` memoized by key).
- Shadows: each shadow-casting light renders the scene again. Limit `castShadow` to important objects, keep shadow map 1024-2048, tight shadow camera bounds. Phones: blob shadow (transparent radial texture quad).

GPU memory / leaks:
- three.js never frees GPU memory automatically. When removing objects permanently: `geometry.dispose()`, `material.dispose()`, each texture `.dispose()`.
```js
export function disposeDeep(root) {
  root.traverse((o) => {
    if (o.geometry && !o.geometry.userData.shared) o.geometry.dispose();
    for (const m of [].concat(o.material ?? [])) {
      if (m.userData?.shared) continue;
      for (const k in m) if (m[k]?.isTexture && !m[k].userData?.shared) m[k].dispose();
      m.dispose();
    }
  });
  root.removeFromParent();
}
```
- Mark cached/shared geometry/material/texture with `userData.shared = true` so dispose helpers skip them. Disposing a shared resource makes other meshes re-upload or render black.
- Leak check: `renderer.info.memory.geometries` must return to baseline after create/remove cycles. Write a probe that builds and removes 50 objects and compares.
- Render targets: `rt.dispose()`. `CanvasTexture` for text labels: dispose when label removed; reuse canvases.

Per-frame allocations (GC stutter):
- Never `new THREE.Vector3()`/`Matrix4`/arrays/closures inside update loops. Hoist scratch objects to module scope:
```js
const _v = new THREE.Vector3(), _q = new THREE.Quaternion();
function update(dt) { _v.copy(target).sub(obj.position); ... }
```
- Avoid `.clone()`, spread `[...]`, `.map/.filter` in hot paths; use indexed loops.
- Pool frequently created objects (particles, popups, projectiles):
```js
function createPool(make, reset) { const free = [];
  return { get: () => free.pop() ?? make(), put: (o) => { reset(o); free.push(o); } }; }
```
Other:
- `renderer.setPixelRatio(Math.min(devicePixelRatio, 1.5))` (2 on high-end at most). Adaptive DPR: drop 0.1-0.15 when FPS < ~40 for 2 s, raise slowly after several good windows. Call `setSize` after changing it.
- Frustum culling is automatic per object (`frustumCulled = true`) but uses bounding spheres; huge merged meshes are never culled -> chunk the world (terrain chunks) so off-screen chunks cull.
- LOD: `THREE.LOD` with `addLevel(mesh, distance)`, or swap to impostor/billboard far away; hide (`visible = false`) distant small props.
- Textures: power-of-two, compressed (KTX2) when many; `texture.colorSpace = THREE.SRGBColorSpace` for color maps only (not normal/roughness). Cap anisotropy at 4-8.
- Materials: `MeshLambertMaterial`/`MeshBasicMaterial` are much cheaper than `MeshStandardMaterial` on phones. Transparent objects are sorted and can't batch well - minimize.
- Don't change `material` properties that trigger recompile (e.g. toggling `fog`, `vertexColors`, defines) at runtime; set `needsUpdate` only when necessary.
- Shader time uniforms: update one shared uniform object (`U.uTime.value = t`) referenced by all materials.
- Update matrices manually for static objects: `obj.matrixAutoUpdate = false; obj.updateMatrix();`.
- Raycasting: raycast against a small list of interactable meshes, not `scene.children` recursively; throttle hover raycasts to pointer moves.

## 9. Asset loading
- Load once, cache by URL in a Map of Promises (dedupes concurrent requests):
```js
const cache = new Map();
export const loadTex = (url) => cache.get(url) ?? (cache.set(url, new THREE.TextureLoader().loadAsync(url)), cache.get(url));
```
- Show a loading state; start the world with placeholders, swap in when loaded.
- Use `THREE.LoadingManager` for overall progress. Handle errors (fallback material), never leave an unhandled promise.
- Procedural assets (geometry built in code) are cheap to ship but expensive to build: build once, cache, reuse.

## 10. Input
- Collect raw input into a state object in event handlers; read it in update (don't run game logic inside event handlers).
```js
const input = { keys: new Set(), pressed: new Set() };
addEventListener('keydown', (e) => { if (!e.repeat) input.pressed.add(e.code); input.keys.add(e.code); });
addEventListener('keyup', (e) => input.keys.delete(e.code));
addEventListener('blur', () => input.keys.clear()); // avoid stuck keys
// end of frame: input.pressed.clear();
```
- Use `e.code` (layout-independent physical key) for movement, `e.key` for text.
- Pointer Events (`pointerdown/move/up`) cover mouse + touch + pen; `setPointerCapture` for drags; `touch-action: none` on canvas.
- Buffer actions with timestamps for input buffering (see game-design.md).
- Don't capture keys while a text input/textarea is focused (`if (e.target.closest('input,textarea')) return;`).

## 11. Process / dev tooling
- One control script (`ctl.mjs start|stop|restart|status <svc>`) owns child processes and PID files; don't spawn servers ad hoc.
- Logs per service; tail the right log before guessing.
- Probes: headless Chrome via CDP, load page, wait for a ready promise (`window.__worldReady`), assert state + no console errors, take a screenshot.
- Expose a debug API on `window.__world` (read state, trigger actions) for probes; strip or guard it in production builds.

## Checklist
- [ ] New logic lives in a pure module (no DOM/THREE imports) with `rand`/`now` injected.
- [ ] `node --test` file added: unit + invariant sweep over many seeds; input state not mutated.
- [ ] dt clamped; physics/sim uses fixed step if determinism or stability matters.
- [ ] Lerps frame-rate independent.
- [ ] Modes use a single `state` field / FSM, with hysteresis where needed.
- [ ] Event subscriptions return and use unsubscribe on teardown.
- [ ] Saved state is plain JSON; version bumped + migration + validation/clamping when shape changes.
- [ ] Numeric defaults use `??` not `||`.
- [ ] No `new Vector3/Matrix4`, closures, `.map/.filter` inside per-frame loops.
- [ ] Repeated objects use InstancedMesh or merged geometry; materials shared from a cache.
- [ ] Removed objects disposed (geometry, material, textures); shared resources marked and skipped.
- [ ] `renderer.info.memory` returns to baseline after create/remove cycle.
- [ ] Draw calls and triangles within budget; DPR capped (<= 1.5-2).
- [ ] Shadows limited; phones use cheaper path.
- [ ] Assets cached by URL; load errors handled.
- [ ] Input read in update, stuck keys cleared on blur, text fields excluded.
- [ ] Probe/screenshot run with zero console errors.
