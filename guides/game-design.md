Read when designing, tuning or reviewing a game mechanic, progression, difficulty, onboarding or "game feel" (juice, feedback, timing numbers).

# Game Design Playbook

## 0. First rules (apply to every mechanic)
- State the mechanic in one sentence: "Player does X to get Y, which lets them Z." If you can't, it is not designed yet.
- Every mechanic needs: a player INPUT (decision or skill), a visible RESPONSE within 100 ms, a CONSEQUENCE that matters later.
- A choice is only interesting if options are close in value but differ in kind (risk vs safety, now vs later, power vs flexibility). If one option always wins, delete the others or rebalance.
- Prefer one deep mechanic that combines with others over three shallow isolated ones.
- Tune with named constants in one place (`TUNING = {...}`), never magic numbers scattered through code. Small LLM edits stay safe.
- Pure logic first (state in -> state out), so numbers can be tested and simulated with `node --test`.

## 1. Loops
- Core loop (seconds-minutes): the action repeated most. Example: walk -> find resource -> gather -> craft. Must be fun with zero rewards attached; test it with all progression disabled.
- Meta loop (hours-days): what core-loop output is spent on. Unlocks, upgrades, collections, home building, companion growth.
- Session loop: what pulls the player back. Daily state change, "while you were away" summary, growing things, timers. Cap offline accumulation (e.g. 8 h) so absence isn't optimal and returning isn't overwhelming.
- Each loop must FEED the next: core output -> meta currency -> unlocks that change the core loop (new verbs, not just bigger numbers).
- Pitfall: meta loop only gives +% stats -> Fix: unlocks give new verbs/options/places (qualitative change every 3-5 unlocks).
- Pitfall: one dominant farming spot -> Fix: diminishing returns per spot (depletion/regrowth), variety bonus, rotating demand.

## 2. Motivation (why players keep going)
- Competence: clear skill growth, readable failure ("I see why I lost").
- Autonomy: multiple valid paths; let players set their own goals (collections, building, naming).
- Relatedness: companion/pet reacting to the player, other players' traces (trails, notes, gifts).
- Curiosity: hide something partially visible (silhouette on map, locked door, "???" entry).
- Use short goals (1-5 min), medium (session), long (week) simultaneously. Always show the next one.
- Variable rewards (random drops) are strong but: guarantee pity timers (e.g. rare item guaranteed by 30th attempt) and never sell randomness for real money to minors / without disclosed odds.
- Avoid pure loss aversion (decay that punishes absence harshly) - players quit instead of returning. Soft decay with floor (e.g. mood decays to 40%, never 0).

## 3. Game feel ("juice") - concrete numbers
Input and control:
- Input-to-visible-response: <= 1 frame ideal, < 100 ms max. Start animation/sound immediately, resolve logic later.
- Input buffer: 100-150 ms. If jump/attack pressed slightly before allowed, remember it and fire when allowed.
- Coyote time: 80-120 ms after leaving a ledge the jump still works.
- Jump: variable height (cut upward velocity ~50% on release); gravity on fall ~1.5-2.5x gravity on rise; cap fall speed.
- Acceleration: reach top speed in ~0.1-0.2 s; decelerate faster than accelerate (stopping feels precise).
- Camera: follow with damping (lerp factor per second, frame-rate independent: `k = 1 - Math.exp(-rate * dt)`, rate 5-10); lead a bit in the move direction; never rotate camera without player intent.
- Corner correction: nudge player a few pixels/units around ledges and doorways instead of hard collision stops.

Impact and feedback:
- Hit-stop (freeze frame): 30-80 ms light hits, 100-150 ms heavy/final hits. Freeze sim time, keep rendering.
- Screen shake: trauma model. `trauma += 0.3..0.6` per event (clamp 1), `shake = trauma^2` (or ^3), decay trauma ~1.5-2/s; offset/rotation from smooth noise not white noise. Max offset small (0.5-2% of screen). Provide a settings toggle (accessibility).
- Flash: hit entity white/tinted 50-100 ms.
- Squash & stretch: preserve volume (scaleY = 1/scaleX); 80-150 ms ease back.
- Anticipation: 50-150 ms wind-up before strong actions (crouch before jump, pull-back before throw). Longer for enemy telegraphs (300-800 ms) so player can react (human reaction ~200-250 ms).
- Follow-through: overshoot and settle (easeOutBack), trailing parts lag (hair, tail, cloth via spring).
- Particles: burst on impact/collect (5-20 particles, 200-600 ms life, inherit some velocity, fade + shrink). Pool them.
- Numbers: damage/coin popups rise and fade 0.5-1 s; merge rapid gains into one counting number.
- Sound layering: each important event = transient (click/thump) + body (tone) + tail (sparkle/reverb). Randomize pitch +-5-10% and volume +-10% to avoid machine-gun repetition. Limit concurrent instances of the same sound (3-4).
- Pickup collection: magnet toward player last 0.2 s, pitch rises for consecutive pickups within ~1 s (combo pitch ladder, reset after gap).
- UI: buttons react on press (scale 0.95 + sound), not on release. Counters tick up over 0.3-0.8 s, not jump.

Easing cheat sheet:
- UI appear: easeOutCubic/easeOutBack 150-300 ms. Disappear: easeInCubic 100-200 ms (exits faster than entrances).
- Movement to target: exponential smoothing or easeInOutSine.
- Bouncy/cute: easeOutElastic sparingly (rewards, level-up).
```js
const easeOutCubic = t => 1 - (1 - t) ** 3;
const easeOutBack = (t, s = 1.70158) => 1 + (s + 1) * (t - 1) ** 3 + s * (t - 1) ** 2;
const damp = (a, b, rate, dt) => a + (b - a) * (1 - Math.exp(-rate * dt)); // frame-rate independent lerp
```
- Pitfall: `x += (target - x) * 0.1` per frame -> speed depends on FPS. Fix: `damp()` above.
- Pitfall: juice drowns information (shake on every tiny event) -> Fix: scale intensity by importance; the biggest effects only for rare, big moments.

## 4. Feedback and readability
- Every state change the player caused must be shown (visual + sound) and, if it matters, explained (text/icon).
- Silhouette and color code: interactables share one visual language (glow/outline/bob); hazards share another (red/orange, spikes). Never use the same cue for both.
- Telegraph threats: wind-up + ground indicator + sound cue. Dodge window must exceed ~250 ms reaction.
- Show cause of failure ("Out of energy", "Too heavy"), plus how to fix it.
- Show progress toward goals (bars, x/y counters). Near-completion is motivating - make it visible.
- Contrast: important gameplay elements must survive desaturation (test in grayscale) and colorblind modes (don't rely on red/green only).
- Diegetic over HUD where possible (companion's posture shows mood), but keep an explicit fallback (tooltip/stats panel).
- Disabled actions: show WHY disabled (tooltip "needs 3 wood"), not a dead grey button.

## 5. Difficulty and pacing
- Flow channel: challenge rises with skill. Use sawtooth: ramp up for 3-5 steps, then a relief/reward beat, then ramp higher.
- Introduce one new thing at a time: teach safely -> test -> combine with known things -> twist.
- Difficulty knobs: enemy count, speed, telegraph length, resource scarcity, time pressure, punishment size. Change one knob at a time when tuning.
- Dynamic difficulty: adjust hidden variables subtly (drop rates after repeated failures, spawn spacing). Never visibly rubber-band rewards in competitive/onchain-value contexts.
- Failure cost: small and fast to retry for skill content (restart < 3 s); bigger only where it creates meaningful tension, with players warned.
- Target first-attempt success rates: tutorial ~95%, normal content 70-85%, optional challenge 20-50%.
- Pitfall: difficulty = bigger HP numbers (sponge) -> Fix: new patterns, combined threats, positional demands.

## 6. Progression
- Curves: XP to next level often `base * level^1.5..2` (polynomial). Exponential (`base * 1.15^level`) only for idle/incremental games where output also grows exponentially.
- Reward cadence: something small every 1-3 min, medium every 10-20 min, big every session.
- Power growth should open options, not trivialize old content - or old content scales/retires gracefully.
- Horizontal progression (sidegrades, cosmetics, new tools) keeps balance; vertical (+stats) inflates and needs sinks.
- Collections: show total slots and empties; give set bonuses for completion.
- Pitfall: early game too slow -> players quit before the hook. Fix: front-load the first 10 minutes with fast rewards and the best mechanic.
- Pitfall: late-game grind wall -> Fix: catch-up mechanics, weekly rotating goals, prestige that grants permanent qualitative perks.

## 7. Onboarding
- Teach by doing in a safe space; text <= 1 short line at a time, shown right when needed.
- First 30 s: player performs the core verb. First 2-3 min: complete one full core loop and see the meta reward.
- Gate complex systems behind first use (crafting UI appears when first item is collectable).
- Use the environment: a lone glowing object teaches "interact with glowing things".
- Let skilled players skip (but don't let skipping break state).
- Track onboarding funnel events (reached step N) to see where players drop.

## 8. Emergent systems
- Make systems interact through shared properties (fire burns wood, wet stops fire, wind spreads fire), not special cases.
- Few rules, many combinations: N simple systems with shared properties give ~N^2 interactions.
- Agents (NPCs, pets) with needs + simple utility scoring (`score = need * weight * availability - distance cost`) produce believable behavior; add small randomness and cooldowns to avoid oscillation.
- Hysteresis on state switches (start eating at hunger < 30, stop at > 80) prevents flip-flopping each frame.
- Guard emergent exploits: cap rates, add diminishing returns, log unusual outcomes in playtests.
- Deterministic seeded RNG makes emergent bugs reproducible (log seed with every bug report).

## 9. Balancing method
- Put all numbers in a table/const object; derive secondary values with formulas (cost = value * 1.2) so one change propagates.
- Compute expected value per minute for each activity (reward * success chance / time). Activities should be within ~20-30% of each other unless intentionally risky (then higher EV for higher variance).
- Simulate: write a script that plays N=1000 sessions with simple bot policies (greedy, random, optimal) and prints distributions (median, p10, p90). See game-economy.md.
- Change numbers in steps of 10-25%, never 2x at once unless clearly broken.
- Dominant strategy check: if bots using one action beat all mixes, rebalance.

## 10. Playtesting heuristics
- Watch silently; don't explain. Where they hesitate > 5 s, the game failed to communicate.
- Ask after, not during: "What were you trying to do?" "What was most/least fun?" "What did X do?"
- Players are right about the problem, often wrong about the fix.
- Record metrics: time to first core action, session length, quit points, deaths per area, resource curves.
- Test with a fresh player every iteration; your own skill blinds you to difficulty (devs are ~2x better than new players).
- 5 testers find most usability issues; rebuild, test again.

## 11. Companion / creature / life-sim specifics
- Needs decay at different rates (hunger fast, social slow); show them diegetically AND numerically on demand.
- Creature must respond to the player within 1 s (head turn, sound) - the cheapest relatedness signal.
- Personality = weights on the same utility functions, not separate code paths.
- Surprise: low-probability idle animations/events (1-5% per minute) create stories.
- Never let neglect permanently ruin the companion; sadness recovers with care.
- Gifts from the creature (random small items) strongly motivate return visits.

## 12. Crypto / onchain game mechanics
- The game must be fun with tokens worth zero. If the only reason to play is earning, it is a job and will collapse (see game-economy.md).
- Put high-frequency play offchain; settle only meaningful, scarce outcomes onchain.
- Never require a wallet popup during the core loop; batch or defer onchain actions.
- Make wallet actions feel like game actions: anticipation (signing), suspense (confirming with progress), payoff (receipt -> big juice). Handle reverts with in-world explanations.
- Show clear odds and caps for any random reward with value.

## Checklist
- [ ] Mechanic stated in one sentence (input -> response -> consequence).
- [ ] Core loop is fun with rewards disabled.
- [ ] Each choice has trade-offs; no dominant option (verified by EV table or sim).
- [ ] Visible + audible response to input within 100 ms.
- [ ] Input buffer (100-150 ms) / coyote time (80-120 ms) where timing matters.
- [ ] Impacts have hit-stop/flash/particles/sound scaled to importance; shake toggleable.
- [ ] Lerps/damping are frame-rate independent (`1 - exp(-rate*dt)`).
- [ ] Threats telegraphed >= 300 ms; failure cause is shown with how to fix it.
- [ ] Disabled actions explain why.
- [ ] Difficulty ramps in sawtooth; one new idea at a time.
- [ ] Reward cadence: small every 1-3 min, medium every 10-20 min.
- [ ] Offline/idle accumulation is capped.
- [ ] Random rewards have pity timers and shown odds.
- [ ] Tuning numbers live in one constants object; logic is pure and unit-tested.
- [ ] Simulation or bot-run done for any economy-affecting number.
- [ ] Onboarding: core verb within 30 s, full loop within 3 min.
- [ ] Readable in grayscale / colorblind-safe for critical cues.
- [ ] Onchain actions are outside the core loop and wrapped in game feedback.
