Read when writing, reviewing or testing Solidity contracts (tokens, staking, presale, DEX, game/NFT contracts) or Hardhat tests - security checklist with bug/fix code.

# Solidity Security Playbook (OZ v5, solc 0.8.x, Hardhat 2 + ethers v6)

## 0. Top rules (most bugs come from these)
- Checks-Effects-Interactions: validate -> update ALL state -> external calls/transfers LAST. Add `nonReentrant` to every external state-changing function that transfers value or calls out.
- Use `SafeERC20` (`safeTransfer`, `safeTransferFrom`, `forceApprove`) for any token you don't control. `require(token.transfer(...))` breaks on tokens that return nothing (USDT-style).
- Credit what was RECEIVED, not the `amount` argument, if the token may be fee-on-transfer: measure `balanceOf` before/after.
- Never use AMM spot price (`getReserves`, `getAmountsOut`) as an oracle for value decisions. Flash loans move it in one tx.
- Never use `block.timestamp`, `block.prevrandao`, `blockhash`, `msg.sender` hashes for randomness with value.
- Every reward/payout must be FUNDED and accounted separately from user principal, especially when stake token == reward token.
- Round in favor of the protocol: user receives round DOWN, user pays round UP.
- Every privileged function: who can call it, what is the worst they can do, is it bounded (caps, timelock), is there an event.
- Unbounded loops over user-growable arrays = DoS. Use pull patterns and mappings.
- Write a test for every revert path and every attack listed below that applies.

## 1. Reentrancy
Bug:
```solidity
function withdraw() external {
    uint256 bal = balances[msg.sender];
    (bool ok,) = msg.sender.call{value: bal}("");   // attacker re-enters here
    require(ok);
    balances[msg.sender] = 0;                          // too late
}
```
Fix (CEI + guard):
```solidity
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol"; // OZ v5 path
function withdraw() external nonReentrant {
    uint256 bal = balances[msg.sender];
    if (bal == 0) revert NothingToWithdraw();
    balances[msg.sender] = 0;
    (bool ok,) = payable(msg.sender).call{value: bal}("");
    if (!ok) revert TransferFailed();
}
```
- Cross-function reentrancy: guard all functions sharing state (`nonReentrant` is per-contract, covers that).
- Read-only reentrancy: view functions read mid-update state from another contract during a callback. Update state before calls; don't expose views computed from half-updated state.
- ERC777/ERC1363/ERC721 `safeTransferFrom`/`onERC1155Received` hooks also call out -> same rules.
- `ReentrancyGuardTransient` needs EIP-1153 (Cancun). Only use if the target chain supports it; otherwise `ReentrancyGuard`.
- Internal helpers called from a `nonReentrant` function must not themselves be `nonReentrant` (nested call reverts). Pattern: external `nonReentrant` wrappers -> internal `_foo` logic. `exit()` calling public `withdraw()` + `claim()` that both have `nonReentrant` works only if `exit` itself has no guard.

## 2. Access control and ownership
- `Ownable(initialOwner)` in OZ v5 takes the owner in the constructor; reverts `OwnableUnauthorizedAccount(account)`.
- Prefer `Ownable2Step` (`transferOwnership` + `acceptOwnership`) to avoid sending ownership to a typo address.
- `renounceOwnership()` permanently removes admin: only do it once nothing needs admin (no stuck funds, no needed setters). Override to revert if renouncing would brick the contract.
- Bound every setter: `require(newFeeBps <= 500)`, `require(maxTx >= supply/1000)`. Emit an event on every config change.
- Owner must never be able to take user principal. Recovery functions must exclude principal + reserved rewards:
```solidity
function recover(address token, uint256 amt) external onlyOwner {
    if (token == address(stakeToken)) {
        uint256 locked = totalStaked + rewardReserve;
        if (stakeToken.balanceOf(address(this)) - locked < amt) revert NotSurplus();
    }
    IERC20(token).safeTransfer(owner(), amt);
}
```
- Multi-role: `AccessControl` with `DEFAULT_ADMIN_ROLE` held by a multisig; operational roles separate.
- Pausing: pause new deposits, never block withdrawals of principal (users must always be able to exit).
- `tx.origin` for auth is wrong (phishing). Use `msg.sender`.
- Initializers/constructors: check zero addresses.

## 3. Randomness
Bug: `uint256 r = uint256(keccak256(abi.encode(block.timestamp, block.prevrandao, msg.sender))) % 100;`
- Validators/sequencers influence timestamp/prevrandao; a contract caller can compute the same value in the same tx and only proceed when it wins (revert otherwise). On non-Ethereum chains `prevrandao` may be constant or weak.
Fixes:
- Oracle VRF (e.g. Chainlink VRF) where available on the chain: request -> callback fulfills; never let the requester cancel/retry after seeing the result.
- Commit-reveal:
```solidity
// 1. commit: user (or server) posts hash(secret, user) and stake; store block number
function commit(bytes32 h) external { commits[msg.sender] = Commit(h, uint64(block.number)); }
// 2. reveal after >= 2 blocks and before 256 blocks (blockhash returns 0 for current block and after 256)
function reveal(bytes32 secret) external {
    Commit memory c = commits[msg.sender];
    require(keccak256(abi.encode(secret, msg.sender)) == c.h, "bad reveal");
    require(block.number > c.block + 1 && block.number <= c.block + 250, "window"); // blockhash(current block) == 0
    delete commits[msg.sender];
    uint256 r = uint256(keccak256(abi.encode(secret, blockhash(c.block + 1))));
    ...
}
```
- Selective-reveal attack: a user who sees a losing outcome just doesn't reveal. Fix: collateral forfeited on non-reveal, or outcome defaults to loss after the window.
- Server-seeded (offchain RNG, signed result): acceptable for low value if seed is committed (hash published) before play; users must be able to verify.

## 4. Integer math, precision, rounding
- 0.8.x reverts on overflow; `unchecked` only where provably safe (loop counters, after explicit check).
- Multiply before divide: `a * b / c`, not `a / c * b`. Use `Math.mulDiv(a, b, c)` (OZ) when `a*b` can overflow; `Math.mulDiv(a, b, c, Math.Rounding.Ceil)` for round-up.
- Scale accumulators by 1e18 (or 1e12 for 6-decimal tokens) so small rates don't round to 0.
- Check decimals: USDC 6, most tokens 18. Normalize before comparing/pricing.
- Dust/zero checks: `require(tokens > 0)` after computing amounts out.
- Casts: `uint64(x)` silently truncates in 0.8 explicit casts! Use `SafeCast.toUint64(x)`.
- Basis points: `fee = amount * feeBps / 10_000`; ensure `feeBps <= 10_000` and sum of fees <= amount.
- Share-based vaults: first-depositor inflation attack (deposit 1 wei, donate to inflate share price). Fix: virtual shares/offset (OZ ERC4626 `_decimalsOffset`), or seed initial deposit, or minimum shares.

## 5. Weird tokens: fee-on-transfer, rebasing, no-return
Bug: `token.transferFrom(user, this, amount); balances[user] += amount;` with a taxed token -> contract credits more than it holds; last users can't withdraw.
Fix:
```solidity
uint256 before = token.balanceOf(address(this));
token.safeTransferFrom(msg.sender, address(this), amount);
uint256 received = token.balanceOf(address(this)) - before;
balances[msg.sender] += received; totalStaked += received;
```
- Rebasing tokens (balance changes without transfers): track shares or disallow.
- Your own taxed token with Uniswap V2: router swaps from user -> pair fail with `UniswapV2: K` because pair receives less than computed. Users must call `swapExactTokensForETHSupportingFeeOnTransferTokens` / `swapExactTokensForTokensSupportingFeeOnTransferTokens` / `swapExactETHForTokensSupportingFeeOnTransferTokens`. Making the pair fee-exempt avoids the issue but also disables the tax on trades - decide deliberately.
- Reflection/tax tokens: exclude pairs/contract/burn from reward denominators, or rewards get stuck; test totals with many holders.
- Max-tx / max-wallet limits: exempt router, pair, staking, presale, or they brick liquidity adds/claims.

## 6. Approvals and permit
- Approve exact amounts in UI by default; infinite approval only on user opt-in.
- Approve race (change N->M lets spender spend N+M): use `forceApprove` or go via 0. OZ v5 removed `safeApprove`; use `forceApprove`/`safeIncreaseAllowance`.
- Never let a contract `transferFrom(arbitraryFrom, ...)` where `from` is a user parameter - anyone can drain users who approved it. `from` must be `msg.sender` (or verified signer).
- Permit (EIP-2612): permit tx can be front-run (griefing) -> wrap in try/catch and proceed if allowance is already enough:
```solidity
try IERC20Permit(token).permit(owner, address(this), v_, deadline, v, r, s) {} catch {}
if (IERC20(token).allowance(owner, address(this)) < v_) revert NoAllowance();
```

## 7. Front-running, MEV, sandwiches
- Any swap: user-supplied `amountOutMin` (slippage) + `deadline`. Contracts calling routers must not pass `amountOutMin = 0` or `deadline = block.timestamp` from inside (useless).
- Presale/first-trade sniping: trading enabled flag + launch block anti-bot are weak; better: liquidity added and trading opened in one tx, or fair launch mechanics.
- Commit-reveal for bids/answers that others could copy.
- Approve-then-deposit ordering is fine; don't rely on tx ordering for fairness.

## 8. Oracles and flash loans
Bug: `price = reserve1 * 1e18 / reserve0;` used to value collateral or mint rewards.
- Attacker flash-borrows, swaps to skew reserves, exploits, swaps back - all in one tx.
Fixes: Chainlink-style feed with staleness check (`updatedAt` recent, `answer > 0`); Uniswap V2 TWAP using `price0CumulativeLast` sampled over >= 30 min; never use `balanceOf(this)` as an accounting source (donations manipulate it).

## 9. Staking reward math (Synthetix accumulator)
```solidity
uint256 public rewardRate;            // reward wei per second
uint256 public periodFinish, lastUpdate, rewardPerTokenStored; // stored scaled 1e18
function lastTimeRewardApplicable() public view returns (uint256) { return Math.min(block.timestamp, periodFinish); }
function rewardPerToken() public view returns (uint256) {
    if (totalStaked == 0) return rewardPerTokenStored;
    return rewardPerTokenStored + (lastTimeRewardApplicable() - lastUpdate) * rewardRate * 1e18 / totalStaked;
}
function earned(address a) public view returns (uint256) {
    return balances[a] * (rewardPerToken() - paid[a]) / 1e18 + rewards[a];
}
modifier updateReward(address a) {
    rewardPerTokenStored = rewardPerToken(); lastUpdate = lastTimeRewardApplicable();
    if (a != address(0)) { rewards[a] = earned(a); paid[a] = rewardPerTokenStored; }
    _;
}
function notifyRewardAmount(uint256 reward, uint256 duration) external onlyOwner updateReward(address(0)) {
    if (block.timestamp < periodFinish) reward += (periodFinish - block.timestamp) * rewardRate;
    rewardRate = reward / duration;
    // SOLVENCY: promised rewards must be held, separate from principal
    uint256 available = rewardToken.balanceOf(address(this)) - (address(rewardToken) == address(stakeToken) ? totalStaked : 0); // contract types need address() to compare
    // also subtract rewards already accrued but unclaimed (track a rewardOwed counter), or the check over-counts
    require(rewardRate * duration <= available, "underfunded");
    lastUpdate = block.timestamp; periodFinish = block.timestamp + duration;
}
```
Pitfalls -> fixes:
- No `periodFinish` (rewards accrue forever at `rewardRate`) -> rewards exceed funding; when stake token == reward token, payouts eat other stakers' principal. Fix: finite periods + funded check + separate `rewardReserve` accounting.
- Unit confusion: `rewardRate` comment says "scaled 1e18" but formula multiplies by 1e18 again -> 1e18x over-payment or 0. Write the unit in the name/comment exactly ("wei per second") and test one full period pays ~reward total.
- `updateReward` must run BEFORE balance changes in stake/withdraw/claim and before rate changes.
- Rewards while `totalStaked == 0` are lost (not distributed). Acceptable if documented; or carry them into next period.
- Rounding: sum paid <= funded (floor). Test: after period, `sum(earned) <= reward` and within `nStakers` wei.
- Lock/boost tiers: weight = amount * boost; recompute weight on every amount/tier change; total weight updated consistently. Prevent lowering tier while locked.
- `emergencyWithdraw()`: returns principal without rewards, bypasses reward math, works while paused. Must not be blocked by reward insolvency.
- `exit()` should not revert when only rewards (no principal) exist.
- Reward APR depends on TVL: `APR = rewardRate * 365 days * price_reward / (totalStaked * price_stake)`. See game-economy.md.

## 10. Presale
Checklist of bugs seen in presales:
- Token supply for sale not verified: require `token.balanceOf(this) >= totalClaimable` before finalize, or pull the sale tokens in at construction/start.
- Owner-only finalize with no deadline -> funds stuck forever if owner disappears. Fix: anyone can finalize after `endTime` if softcap met; refunds open automatically if not finalized by `endTime + grace`.
- No soft cap: refunds and finalize both valid after end -> race. Define: `raised >= softCap` -> success path only; else refund path only.
- Refund must decrement `raised`, zero `contributed` and `claimable`, emit event.
- Hard cap: last buyer partial fill (refund the excess) instead of reverting, or revert clearly.
- Per-wallet caps are bypassed by multiple wallets (sybil) - treat as soft fairness, not security.
- Liquidity: finalize should add liquidity in the same tx and lock/burn LP tokens (send LP to a timelock or `0x...dEaD`), with `amountMin` values set, not 0.
- Claim only after liquidity exists (prevents pre-listing dumps); vesting optional.
- Use `call{value:}` with success check for native transfers; `transfer()` (2300 gas) breaks for contract wallets.
- Check `startTime < endTime`, `minBuy <= maxBuy`, `price > 0` in constructor.

## 11. DEX (Uniswap V2 style)
- Pair invariant: after swap `balance0Adj * balance1Adj >= reserve0 * reserve1 * 1000^2` with 0.3% fee. Do not modify core math.
- `MINIMUM_LIQUIDITY = 1000` burned on first mint (prevents share inflation).
- `skim(to)` sends excess balance over reserves; `sync()` sets reserves to balances. Donations + `sync` can move price: never trust reserves as oracle.
- Router `pairFor` uses CREATE2 with the factory's init code hash. If you compile your own pair, the hash in `UniswapV2Library.pairFor` must equal `keccak256(type(UniswapV2Pair).creationCode)` of YOUR build, or router calls hit a wrong/empty address. Compute it in a test and assert, don't hardcode blindly.
- Router addLiquidity/swap need `deadline` in future and sensible `amountMin`; scripts often pass 0 - OK on local, dangerous on mainnet.
- WETH/WCRO path: native swaps use the wrapped native token address from the router (`router.WETH()`).
- Compilers: core 0.5.16, periphery 0.6.6 - keep separate compiler entries in hardhat config; don't bump them.

## 12. External calls and return values
- Low-level `call` returns `(bool, bytes)`; always check `bool`. Calls to an address with no code SUCCEED -> check `addr.code.length > 0` for contract targets.
- `SafeERC20` handles no-return tokens and checks code existence.
- Don't forward all gas to untrusted callbacks if a revert there must not block you; use pull-payments.
- Return-bomb: untrusted callee returns huge data -> copy cost. Use assembly call with limited return copy only if relevant (rare).

## 13. DoS
- Loops over all stakers/holders/players in one tx -> gas limit DoS. Use accumulators (section 9) or paginate with `start, count`.
- Push payments to many recipients: one reverting recipient blocks all. Use pull (`claim()`).
- Unbounded arrays users can append to (e.g. `players.push`) and later iterate -> cap or avoid.
- Block stuffing on deadlines: give windows of hours, not blocks.
- `require(address(this).balance == x)` breaks with forced ETH (selfdestruct/coinbase). Use internal accounting.

## 14. Signatures (EIP-712, replay)
```solidity
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {Nonces} from "@openzeppelin/contracts/utils/Nonces.sol";
contract Claims is EIP712("Game", "1"), Nonces {
    bytes32 constant CLAIM_TYPEHASH = keccak256("Claim(address player,uint256 amount,uint256 nonce,uint256 deadline)");
    function claim(uint256 amount, uint256 deadline, bytes calldata sig) external {
        if (block.timestamp > deadline) revert Expired();
        bytes32 digest = _hashTypedDataV4(keccak256(abi.encode(CLAIM_TYPEHASH, msg.sender, amount, _useNonce(msg.sender), deadline)));
        if (ECDSA.recover(digest, sig) != signer) revert BadSig();
        ...
    }
}
```
- Must include: nonce (replay), chainId + contract address (EIP712 domain does this), deadline, the beneficiary (`msg.sender` or explicit field).
- `ecrecover` returns `address(0)` on bad sig -> if `signer` unset (0) anything passes. OZ `ECDSA.recover` reverts on invalid; also require `signer != address(0)`.
- `abi.encodePacked` with two dynamic types can collide; use `abi.encode`.
- Server signer key: hot key with limited power (per-claim caps, daily caps, rotatable by owner).

## 15. Upgradeability (only if needed; immutable is simpler and safer)
- Use `@openzeppelin/contracts-upgradeable` + `initializer`; constructor calls `_disableInitializers()`.
- Never reorder/remove storage variables; append only. OZ v5 upgradeable uses ERC-7201 namespaced storage.
- No `immutable` state expected to differ per proxy; no `selfdestruct`/`delegatecall` in implementation.
- Upgrade admin = multisig + timelock. Users should be able to exit before an upgrade takes effect.

## 16. Events and hygiene
- Emit events for every state change users/indexers care about (stake, withdraw, claim, config change, ownership).
- Custom errors (`error Locked(uint64 until);`) are cheaper and testable.
- Pin pragma for deploys (`pragma solidity 0.8.24;`). Set `evmVersion` explicitly in hardhat config if the target chain may not support newer opcodes (PUSH0 = shanghai, TSTORE = cancun). Cronos (chainIds 25 / 338) is EVM-compatible; confirm supported evmVersion before using `ReentrancyGuardTransient`.
- OZ v5 ERC20 customizations override `_update(from, to, value)` (the v4 `_beforeTokenTransfer` hooks are gone).
- No private keys in repo; deployer key from env only; `.env` gitignored.
- Verify contracts on the explorer; save deployed addresses + chainId to a config file consumed by the frontend.

## 17. Hardhat testing
```js
const { expect } = require("chai");
const { ethers } = require("hardhat");
const { loadFixture, time, mine, impersonateAccount, setBalance } =
  require("@nomicfoundation/hardhat-toolbox/network-helpers");
const { anyValue } = require("@nomicfoundation/hardhat-chai-matchers/withArgs");

async function deployFixture() {
  const [owner, alice, bob] = await ethers.getSigners();
  const token = await ethers.deployContract("CrokToken", [owner.address, ethers.parseEther("1000000")]);
  const staking = await ethers.deployContract("CrokStaking", [await token.getAddress(), owner.address]);
  await token.transfer(alice.address, ethers.parseEther("1000"));
  return { token, staking, owner, alice, bob };
}

it("pays the season reward over time", async () => {
  const { token, staking, owner, alice } = await loadFixture(deployFixture); // snapshot-reverted per test
  await token.connect(alice).approve(staking, ethers.MaxUint256);
  await staking.connect(alice).stake(ethers.parseEther("100"), 0);
  await time.increase(7 * 24 * 3600);          // evm_increaseTime + mine
  await expect(staking.connect(alice).claim())
    .to.emit(staking, "RewardPaid").withArgs(alice.address, anyValue);
});

it("reverts for non-owner", async () => {
  const { staking, alice } = await loadFixture(deployFixture);
  await expect(staking.connect(alice).setPaused(true)).to.be.revertedWith("STK: owner");
  // OZ Ownable: .to.be.revertedWithCustomError(contract, "OwnableUnauthorizedAccount").withArgs(alice.address)
});
```
- Balance assertions: `await expect(tx).to.changeTokenBalances(token, [alice, staking], [amt, -amt]);`, `changeEtherBalance(s)`.
- Time: `time.latest()`, `time.increaseTo(ts)`, `mine(n)`. Remember the next tx is mined at +1 s; use approximate checks (`closeTo`) for time-based rewards.
- Impersonation (forking/whales/contracts as callers): `await impersonateAccount(addr); await setBalance(addr, ethers.parseEther("10")); const s = await ethers.getSigner(addr);` (or `ethers.getImpersonatedSigner(addr)`).
- Panics: `.to.be.revertedWithPanic(0x11)` (overflow), `0x12` (div by zero).
- Reentrancy test: attack contract that re-enters in `receive()` / token hook, assert revert:
```solidity
contract Attacker { IVault v; uint n;
  constructor(IVault _v) { v = _v; }
  function attack() external payable { v.deposit{value: msg.value}(); v.withdraw(); }
  receive() external payable { if (n++ < 3) v.withdraw(); }
}
```
- Fee-on-transfer test: deploy a mock taxed ERC20 (override `_update` to burn 1%) and assert accounting matches balances.
- Invariant-style tests in JS: random sequence of N actions (stake/withdraw/claim/increaseTime) by several users with a seeded RNG; after each step assert:
  - `token.balanceOf(staking) >= totalStaked + rewardsOwedOutstanding`
  - `sum(user balances) == totalStaked`
  - no user can withdraw more than deposited; total paid <= funded.
- Test every require/custom error, boundary (exact cap, cap+1, endTime, endTime+1), zero amounts, and the second call (double claim/refund).
- `npx hardhat test`, `npx hardhat coverage` (toolbox includes solidity-coverage), `REPORT_GAS=true npx hardhat test` for gas reporter.

## Checklist
- [ ] CEI order in every function; `nonReentrant` on value-moving externals; no nested guarded calls.
- [ ] SafeERC20 for external tokens; received amount measured if fee-on-transfer possible.
- [ ] No onchain pseudo-randomness with value; VRF or commit-reveal with non-reveal penalty.
- [ ] No spot-price oracle; no `balanceOf(this)` as accounting truth.
- [ ] Rewards funded and tracked separately from principal; finite periods; solvency test passes.
- [ ] Rounding favors protocol; mulDiv where overflow possible; SafeCast for downcasts.
- [ ] All admin setters bounded, evented; owner can't touch principal; withdrawals never pausable.
- [ ] Presale: softcap/hardcap paths exclusive, refunds work without owner, tokens pre-funded, LP locked.
- [ ] Swaps have slippage + deadline; taxed tokens use SupportingFeeOnTransfer router functions.
- [ ] No unbounded loops over user-growable data; pull over push.
- [ ] Signatures: EIP-712 domain, nonce, deadline, beneficiary; signer != 0.
- [ ] Downcasts checked; zero-address and parameter sanity checks in constructor.
- [ ] Events for all state changes; custom errors.
- [ ] Pragma pinned; evmVersion compatible with target chain.
- [ ] Tests: fixtures, every revert path, time-travel boundaries, reentrancy attacker, taxed-token mock, randomized invariant run.
- [ ] Deployer key only in env; addresses + chainId written to frontend config.
