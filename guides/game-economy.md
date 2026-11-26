Read when designing or tuning a game/token economy: currencies, faucets and sinks, emissions, staking APR, rewards, prices, anti-bot design, economy simulations, onchain vs offchain state.

# Game & Token Economy Playbook

## 0. Top rules
- The game must be worth playing if the token is worth 0. Earnings are a bonus on fun, never the reason to play. If players are only extracting value, the economy is a countdown.
- Every token that enters (faucet) needs a reason to leave (sink) that players WANT (status, utility, convenience, progress), not just fees they resent.
- Real-value tokens are paid by someone: new buyers, sinks, or treasury. Name the payer for every reward. If the answer is "new buyers", it is a Ponzi dynamic and will collapse when inflow slows.
- Fixed budgets, not open-ended promises: emissions are funded upfront with end dates (seasons). Never promise a fixed APR.
- Simulate before shipping (section 9). Ship conservative numbers; raising rewards later is welcome, cutting them causes revolt.
- Separate soft currency (offchain, earned in play, unlimited, can be tuned freely) from hard/token currency (onchain, scarce, tradable). Most gameplay uses soft currency.

## 1. Faucets and sinks
Faucets (create currency): quest/job rewards, drops, staking emissions, daily login, selling items to NPCs, offline accumulation.
Sinks (destroy or lock currency): crafting costs, upgrades with rising cost, repairs/durability, consumables (food, energy), cosmetics, housing/land upkeep, fees on trade (burned or to treasury), entry fees for tournaments (partially redistributed), rerolls, name changes, speed-ups, burns for crafting rare items, staking locks (temporary sink).
- Track: `net_flow = faucets - sinks` per day, and `currency per active player`. Target: per-player supply grows slowly (mild inflation ~0-5% per week in soft currency is fine); hard token supply in circulation should not grow faster than real demand.
- Sinks must scale with wealth: percentage-based fees, upgrade costs that grow (`cost = base * 1.5^level`), luxury items for whales. Flat sinks stop mattering for rich players.
- Pitfall: rewards scale with playtime but sinks are one-time -> hyperinflation. Fix: recurring sinks (consumables, upkeep, durability).
- Pitfall: NPC buy-back at fixed price = infinite faucet for any farmable item. Fix: diminishing NPC prices per day, demand caps.
- Pitfall: crafting outputs worth more than inputs with no limit -> arbitrage loop. Fix: verify `value(out) <= value(in) + labor_reward` and cap per day.

## 2. Inflation control
- Cap per-player earnings per day (soft cap with diminishing returns: full reward first N actions, then 50%, then 10%).
- Decay unused offline accumulation (cap at e.g. 8 h).
- Rising costs (price scales with total owned or level).
- Seasonal resets for leaderboards/soft currency with conversion of a small fraction to permanent rewards.
- Burn a share of every trade fee.
- Monitor median and p90 wealth; if p90 / median grows each week, add high-end sinks.

## 3. Emissions schedules and APR math
Definitions:
- `E` = reward tokens emitted per year. `R` = rewardRate per second = `E / 31_536_000`.
- `TVL` = total staked value = `totalStaked * P_stake`.
- `APR = E * P_reward / TVL`. Same token: `APR = E / totalStaked`.
- `APY` (auto-compounded n times/year) = `(1 + APR/n)^n - 1`.
Key consequence: for a fixed emission, APR falls as more people stake. Show APR as a live estimate, never as a promise.
Examples:
- 10M tokens/year to stakers, 50M staked -> APR 20%. If 200M staked -> 5%.
- Want ~30% APR at expected 20M staked: `E = 0.30 * 20M = 6M/yr` -> `R = 6e6 / 31_536_000 ≈ 0.19 tokens/s` (in wei: `0.19e18`).
- Funding check: season of D seconds needs `R * D` tokens in the contract BEFORE starting (see solidity-security.md section 9).
Schedules:
- Constant per season (simple, predictable). Recommended: 30-90 day seasons, each funded separately, adjustable between seasons.
- Halving / decay: `E_n = E_0 * k^n` (k=0.5 per year, or 0.9 per month) - front-loads rewards to early users; ensure later APR still attractive enough.
- Supply share rule: total emissions to staking over its lifetime typically <= 10-30% of supply; check fully diluted outcome.
- Lock boosts: weight = amount * boost (e.g. 1.0x flex, 1.25x 30d, 1.6x 90d, 2.0x 180d). Effective APR for tier = `baseAPR_per_weight * boost`. Boosts redistribute the same emission; they don't add tokens.
- Pitfall: rate set as "tokens per second" without an end -> unfunded liabilities. Fix: periodFinish + funded reserve.
- Pitfall: stake token == reward token and rewards paid from the same balance -> principal used as rewards. Fix: separate reserve accounting.

## 4. Staking incentives that help the game
- Staking should grant in-game utility (cosmetic tiers, access, small boosts, governance of game events), not only yield. Utility demand is stickier than yield demand.
- Locks reduce sell pressure but create unlock cliffs: stagger unlock dates (lock from stake time, not global date).
- Early-exit penalty (e.g. 10% of rewards forfeited) -> redistribute to remaining stakers or burn. Never penalize principal without very clear UX.
- Avoid "stake to earn more of the same token" as the only loop: it inflates supply while adding no demand.

## 5. Play-to-earn failure modes
- Mercenary farmers: arrive for yield, dump rewards, leave when APR drops. Signs: rewards sold within hours, low session depth. Fix: rewards in soft currency, convertible to token with limits; vesting/claim delays; utility sinks.
- Death spiral: token price falls -> rewards worth less -> players leave -> fewer sinks -> price falls. Fix: rewards denominated in game value (items, soft currency), token emissions capped by sink revenue (e.g. emit at most X% of last week's burns/fees), treasury buffer.
- Bots / sybils: farm per-account rewards with many wallets. Fixes: rewards proportional to stake or skill, not per-account; diminishing returns per wallet don't help vs sybils - cost per account does (entry fee, staked minimum, NFT required, time-gated progress); server-side behavior checks; rate limits.
- Whale capture: big holders take most rewards. Accept for stake-weighted emissions; for gameplay rewards cap per player.
- Hyperinflation of items: every player farms the same items. Fix: item durability/consumption, crafting that consumes items, drop rates decreasing with supply.
- Pay-to-win resentment: selling power for tokens alienates free players. Sell convenience and cosmetics; keep competitive power earned.
- Rug perception: owner can mint/withdraw. Fix: fixed supply, no owner mint, locked LP, transparent treasury, timelocks.

## 6. Pricing in-game items
- Anchor prices to time: `price ≈ minutes_of_play_to_earn * target_reward_per_minute`. Common item = 5-15 min, rare = 1-3 h, legendary = days.
- Price ladder: each tier ~2-3x the previous with clear capability/visual difference.
- Dynamic pricing for player-sold goods: bonding curve or decreasing NPC demand (`price = base * 0.97^sold_today`).
- Show prices in soft currency primarily; token prices fluctuate - peg token-priced items to a target value via oracle-free mechanisms (re-set prices per season by governance/admin within bounds) rather than live spot prices.
- Bonding curve for primary sales (e.g. linear `price = a + b * supply`): early buyers profit only if later buyers come; disclose.
- Avoid loot boxes for real money; if random paid outcomes exist, publish odds and pity rules (and check local law).

## 7. Reward balancing
- Compute reward per minute for each activity: `EV_per_min = (reward * p_success) / minutes`. Keep core activities within ~20-30% of each other; risky activities get higher EV but higher variance.
- First-time bonuses and daily first-win bonuses encourage variety without raising the grind ceiling.
- Diminishing returns per activity per day (e.g. 100% for first 10, 50% next 20, 10% after) protects against bots and burnout.
- Streaks: reward but forgive (1 grace day), otherwise streak loss makes players quit.
- Rewards visible before action (shows decision value), and earned feedback immediate (game-design.md juice).

## 8. Anti-bot and anti-exploit design
- Anything paying real value must be validated server-side or onchain; client is untrusted (devtools can call any function).
- Server-authoritative game results; client sends inputs, server simulates or verifies (replay seed + inputs).
- Signed claims (EIP-712) with per-user nonce, deadline, per-claim and per-day caps; signer key with limited blast radius.
- Rate limits per wallet and per IP; anomaly checks (actions/minute, perfect timing, identical paths).
- Proof of humanity cost: stake requirement, small entry fee, account age, NFT pass - make each sybil account cost more than it earns.
- Delays: claims vest over days -> exploits detectable before funds leave.
- Kill switch on reward claims (not on principal withdrawal).
- Never pay per transaction volume (wash trading); pay per unique, rate-limited, meaningful action.

## 9. Simulate before shipping
Write a small Node script (pure functions, seeded RNG) and run thousands of days of agents:
```js
// tools/sim-economy.mjs  - node tools/sim-economy.mjs
import { rngFrom } from '../js/world/rng.js';
const P = { players: 1000, days: 180, rewardPerAction: 5, actionsPerDay: [10, 40, 200], // casual, core, bot
  sinkPerDay: 30, upgradeBase: 100, upgradeMul: 1.5, botShare: 0.05, dailyCapFull: 50 };
const rand = rngFrom(42);
const ps = Array.from({ length: P.players }, (_, i) => ({ coins: 0, level: 0,
  type: rand() < P.botShare ? 2 : rand() < 0.7 ? 0 : 1 }));
for (let day = 0; day < P.days; day++) {
  let minted = 0, burned = 0;
  for (const p of ps) {
    const acts = P.actionsPerDay[p.type] * (0.5 + rand());
    const full = Math.min(acts, P.dailyCapFull), extra = acts - full;
    const earn = (full + extra * 0.1) * P.rewardPerAction;           // diminishing returns
    p.coins += earn; minted += earn;
    const sink = Math.min(p.coins, P.sinkPerDay); p.coins -= sink; burned += sink;
    const cost = P.upgradeBase * P.upgradeMul ** p.level;
    if (p.coins >= cost) { p.coins -= cost; burned += cost; p.level++; }
  }
  if (day % 30 === 29) {
    const c = ps.map(p => p.coins).sort((a, b) => a - b), q = (x) => c[Math.floor(x * (c.length - 1))];
    console.log({ day: day + 1, minted: Math.round(minted), burned: Math.round(burned),
      median: Math.round(q(0.5)), p90: Math.round(q(0.9)), botAvg: Math.round(avg(ps.filter(p => p.type === 2))) });
  }
}
function avg(a) { return a.reduce((s, p) => s + p.coins, 0) / Math.max(1, a.length); }
```
What to check:
- Supply curve (minted - burned cumulative): flattening, not exponential.
- Wealth spread (median vs p90): gap stable, not exploding.
- Bot earnings vs honest core player: bots should not earn > ~1.5x a core player.
- Time-to-milestone for casual players matches design (e.g. first upgrade day 1, level 5 by week 2).
- Staking: sweep TVL from 1% to 50% of supply and print APR; ensure funded emissions cover the full season.
- Stress: price drop 80%, player count halves, a whale stakes 30% - does anything break or become absurd?
- Put the sim and its key assertions into `node --test` so tuning changes are checked.

## 10. Onchain vs offchain decisions
Put ONCHAIN only what needs trustless ownership or value transfer:
- Token balances, staking positions, rewards owed, presale contributions.
- Ownership of scarce tradable items (NFTs) and their critical attributes.
- Final results of high-value competitions (or a Merkle root / signed result of them).
Keep OFFCHAIN (server or client) everything frequent, cheap or cosmetic:
- Movement, combat, crafting steps, pet needs, world state, chat, cosmetics not traded.
- Soft currency, XP, quests, daily limits.
Bridge patterns:
- Periodic claim: offchain earnings -> server signs EIP-712 claim (capped) -> user claims onchain.
- Merkle distributor: weekly root of rewards posted onchain; users claim with proof (cheap for operator, one tx per user).
- Onchain item, offchain metadata: store `tokenId -> hash` onchain, metadata served offchain/IPFS; verify hash.
- Commit outcomes: publish hash of server seed before season; reveal after for verifiability.
Rule: if the server can change it, players must not believe it is trustless - be explicit in UI.

## 11. Gas-aware design
- Cronos/EVM: every storage write is expensive relative to reads (SSTORE new slot ~20k gas). Minimize writes per user action.
- Use accumulators (rewardPerToken) instead of loops over users.
- Batch: one `claimAll` instead of per-item claims; Merkle claims instead of airdrops pushing to thousands.
- Pack structs (uint64 timestamps, uint128 amounts) in one slot when updated together.
- Avoid per-action onchain transactions in the core loop; players won't pay gas every 30 s. Aim for <= 1-2 wallet confirmations per session.
- Events instead of storage for history/logs (frontend indexes events).
- Let users choose when to claim (claiming costs gas; tiny claims waste money) - show "claim worth it?" estimate.

## 12. Tokenomics hygiene
- Publish allocation table (team, treasury, liquidity, staking emissions, presale) with vesting; team tokens vested (cliff + linear).
- Liquidity locked or LP burned; owner powers minimal and listed.
- Fixed supply + no mint is easiest to trust; any mint must be capped and timelocked.
- Transfer taxes reduce DEX usability (need special router functions, confuse aggregators) - prefer sinks inside the game over taxes on every transfer.
- Reflection tokens add gas to every transfer and complex accounting; avoid unless core to the concept.

## Checklist
- [ ] Game is fun with token value 0; earning is secondary.
- [ ] Every faucet listed with its payer; every faucet has matching sinks that scale with wealth.
- [ ] Soft vs hard currency separated; most gameplay uses soft.
- [ ] Emissions funded upfront, finite seasons, no promised APR.
- [ ] APR computed across a TVL sweep; same-token staking principal separated from rewards.
- [ ] Per-player daily caps / diminishing returns on reward activities.
- [ ] Sybil cost per account > its expected earnings.
- [ ] Real-value rewards validated server-side/onchain; signed claims capped with nonce + deadline.
- [ ] Simulation script run: supply curve, wealth spread, bot vs human earnings, stress cases.
- [ ] Sim assertions added to `node --test`.
- [ ] Item prices anchored to minutes of play; tier ladder 2-3x.
- [ ] Onchain only for ownership/value; frequent actions offchain; <= 1-2 wallet confirms per session.
- [ ] Storage writes minimized; accumulators instead of loops; Merkle/batch claims.
- [ ] Allocation, vesting, LP lock and owner powers documented.
- [ ] Taxes/reflections justified (or avoided) and DEX-compatible.
- [ ] Kill switch on claims only, never on principal.
