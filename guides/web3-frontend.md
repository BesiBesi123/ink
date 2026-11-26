Read when building or fixing a dapp frontend with wagmi/viem/tanstack query (wallet connect, chain switching, contract reads/writes, tx status, approvals, BigInt amounts, errors).

# Web3 Frontend Playbook (wagmi 3.x + viem 2.x + @tanstack/react-query 5 + React 19)

Versions this guide was written against: wagmi 3.7.9, viem 2.57.14, @tanstack/react-query 5.105, react 19, vite 8, tailwind 4. Verify APIs in `frontend/node_modules/wagmi/dist/types/exports/index.d.ts` before using a hook you are unsure of.

## 0. Top rules
- Simulate before write: `simulateContract` -> `writeContract(request)` -> `waitForTransactionReceipt`. Simulation surfaces revert reasons before the wallet popup.
- Pin every read and write to the TARGET chain (`chainId: TARGET.id`). Unpinned reads use the wallet's current chain and show wrong data on the wrong network.
- Amounts are `bigint` end to end. Convert only at the edges: `parseUnits(inputString, decimals)` in, `formatUnits(value, decimals)` out. Never `Number(bigint)` for math.
- `useReadContract` does NOT auto-disable when `args` contain `undefined`. Always add `query: { enabled: !!address && !!contractAddr }`.
- After a confirmed tx, invalidate/refetch affected reads (balances, allowance, earned).
- Frontend checks are UX only. The contract must enforce everything.
- No private keys, mnemonics or RPC secrets in frontend code or `.env` files bundled by vite (`VITE_*` vars are public).

## 1. wagmi 3 renames (vs v2 tutorials)
- `useAccount` -> `useConnection` (old name still exported, deprecated). Returns `{ address, addresses, chainId, isConnected, status: 'connected'|'connecting'|'reconnecting'|'disconnected', connector }`.
- `useAccountEffect` -> `useConnectionEffect`; `useSwitchAccount` -> `useSwitchConnection`.
- Mutation hooks return the tanstack mutation plus named fns: `useWriteContract()` -> `{ writeContract, writeContractAsync, data: hash, isPending, error, reset }`; `useConnect()` -> `{ connect, connectAsync, connectors }`; `useSwitchChain()` -> `{ switchChain, switchChainAsync, chains }`.
- Connectors from `wagmi/connectors`: `injected()` needs nothing extra. `metaMask()`, `walletConnect({ projectId })`, `coinbaseWallet()`, `baseAccount()`, `safe()` need their optional peer packages installed (e.g. `@walletconnect/ethereum-provider`); importing without them fails at build/runtime.
- New `*Sync` variants exist (`useWriteContractSync`, `useSendTransactionSync`) for chains supporting sync tx; standard flow is still write + wait.

## 2. Config
```js
import { createConfig, http, fallback } from 'wagmi'
import { cronos, cronosTestnet } from 'viem/chains'   // include multicall3 address; prefer over hand-written chain objects
import { injected } from 'wagmi/connectors'
import { defineChain } from 'viem'

export const localhost = defineChain({ id: 31337, name: 'Localhost',
  nativeCurrency: { name: 'Ether', symbol: 'ETH', decimals: 18 },
  rpcUrls: { default: { http: ['http://127.0.0.1:8545'] } } })

export const config = createConfig({
  chains: [cronosTestnet, localhost],
  connectors: [injected()],          // EIP-6963 wallet discovery is on by default
  transports: {
    [cronosTestnet.id]: fallback([http(import.meta.env.VITE_RPC_338), http()]), // http() = chain default RPC
    [localhost.id]: http(),
  },
})
export const TARGET = cronosTestnet
```
- Hand-written chain objects without `contracts.multicall3` disable multicall batching (wagmi falls back to N separate calls). `viem/chains` definitions for cronos (25) and cronosTestnet (338) already include multicall3.
- Wrap app: `<WagmiProvider config={config}><QueryClientProvider client={queryClient}>...`. Create `queryClient` once at module scope, not inside a component.
- Addresses per chain in one config file (`config.json` written by the deploy script with `chainId`); refuse to run if `cfg.chainId` is not in `config.chains`.

## 3. Connecting and chain switching
```js
const { address, chainId, isConnected, status } = useConnection()
const { connect, connectors, isPending } = useConnect()
const { switchChain, isPending: switching } = useSwitchChain()
const wrongChain = isConnected && chainId !== TARGET.id

<button onClick={() => connect({ connector: connectors[0], chainId: TARGET.id })}>Connect</button>
{wrongChain && <button onClick={() => switchChain({ chainId: TARGET.id })}>Switch to {TARGET.name}</button>}
```
- List all `connectors` (EIP-6963 shows each installed wallet) instead of only `connectors[0]`.
- `status === 'reconnecting'` on page load: render a neutral state, not "Connect wallet" flicker.
- Disable all write buttons when `wrongChain`; keep reads working via pinned `chainId`.
- Switching may fail if the wallet doesn't know the chain; injected wallets get `wallet_addEthereumChain` from the chain object. Catch and show "Add network manually: RPC ..., chainId ...".

## 4. Reading
```js
const { data: bal, queryKey: balKey, isLoading, error } = useReadContract({
  address: cfg.token, abi: erc20Abi, functionName: 'balanceOf', args: [address],
  chainId: TARGET.id,
  query: { enabled: !!address && !!cfg?.token, staleTime: 10_000 },
})
```
- Many reads -> one `useReadContracts` (multicall, one RPC):
```js
const { data } = useReadContracts({
  contracts: [
    { address: cfg.presale, abi: presaleAbi, functionName: 'raised', chainId: TARGET.id },
    { address: cfg.presale, abi: presaleAbi, functionName: 'hardCap', chainId: TARGET.id },
    { address: cfg.presale, abi: presaleAbi, functionName: 'claimable', args: [address], chainId: TARGET.id },
  ],
  query: { enabled: !!address && !!cfg?.presale },
})
const [raised, hardCap, claimable] = data?.map(r => r.status === 'success' ? r.result : undefined) ?? []
```
- `allowFailure` defaults to true: each item is `{ status, result | error }`. Check `status`.
- ABI must contain every function you call. A missing entry (e.g. reading `allowance` with an ABI lacking it) fails silently as `error` and `data` stays undefined. Use viem's `erc20Abi` for tokens and the compiled artifact ABI (`artifacts/.../X.json`.abi) for your contracts; never hand-type ABIs that drift from the contract (function names like `balanceOf` vs `balances` must match exactly).
- Use `as const` on ABIs in TS for type inference.
- Function names/outputs must match the deployed contract; wrong `outputs` decode garbage or throw.

## 5. Writing: simulate -> write -> wait
```js
import { useSimulateContract, useWriteContract, useWaitForTransactionReceipt } from 'wagmi'

const amt = safeParse(input, 18) // bigint | null
const sim = useSimulateContract({
  address: cfg.staking, abi: stakingAbi, functionName: 'stake', args: [amt ?? 0n],
  chainId: TARGET.id, account: address,
  query: { enabled: !!address && !!amt && amt > 0n && !wrongChain },
})
const { writeContract, data: hash, isPending, error: writeErr, reset } = useWriteContract()
const receipt = useWaitForTransactionReceipt({ hash, chainId: TARGET.id })

const onStake = () => sim.data && writeContract(sim.data.request)
// button disabled = !sim.data || isPending || receipt.isLoading
// sim.error -> show decoded revert reason before the user signs
```
Imperative variant (inside one async handler, good for multi-step flows):
```js
import { simulateContract, writeContract, waitForTransactionReceipt } from 'wagmi/actions'
const { request } = await simulateContract(config, { address, abi, functionName: 'claim', chainId: TARGET.id, account })
const hash = await writeContract(config, request)
const rc = await waitForTransactionReceipt(config, { hash, chainId: TARGET.id }) // throws Error(reason) if reverted
```
- `writeContract` (non-async) never throws into a surrounding try/catch; errors land in `error` of the hook. Use `writeContractAsync` inside try/catch if you need control flow.
- One `useWriteContract` per independent action (or track which action is pending). Sharing one instance for approve/stake/claim mixes up their statuses.
- `waitForTransactionReceipt` in wagmi throws when `receipt.status === 'reverted'` (it replays the call to extract a reason), so `receipt.isError` covers reverts.
- Payable: pass `value: parseUnits(x, 18)` (bigint).

## 6. Transaction UX states
Show each state distinctly:
1. idle -> button enabled only if simulation succeeded.
2. `isPending` (wallet popup open) -> "Confirm in wallet".
3. hash present, `receipt.isLoading` -> "Confirming..." + explorer link `${TARGET.blockExplorers.default.url}/tx/${hash}`.
4. `receipt.isSuccess` -> success toast + refetch reads + game juice.
5. error -> human message (section 8), keep form values, offer retry.
- Persist pending hashes (sessionStorage) so a reload can resume waiting.
- Don't re-enable the button between "write sent" and "receipt" (double-submit).

## 7. BigInt and units
```js
import { parseUnits, formatUnits } from 'viem'
export function safeParse(s, dec) {
  if (!s || !/^\d*\.?\d*$/.test(s.trim()) || s.trim() === '.') return null
  try { return parseUnits(s.trim(), dec) } catch { return null }   // throws InvalidDecimalNumberError on bad input
}
export const fmt = (v, dec = 18, max = 4) =>
  v == null ? '—' : Number(formatUnits(v, dec)).toLocaleString(undefined, { maximumFractionDigits: max }) // display only
```
- `parseUnits` ROUNDS extra decimals (`parseUnits('1.1234567', 6)` -> 1123457n). Clamp input decimals to token decimals in the input handler.
- Read `decimals()` per token; don't assume 18 (USDC = 6).
- Slippage math in bigint: `const minOut = quoted * (10_000n - BigInt(slippageBps)) / 10_000n`. NOT `BigInt(Number(quoted) * 98 / 100)` (loses precision and throws RangeError on non-integers).
- Percent of bigint: `Number(raised * 10_000n / hardCap) / 100`.
- Max button: use exact `balance` bigint; for native currency leave gas headroom.
- `0n` is falsy: `if (bal)` is wrong for zero balance checks; use `bal != null`.
- JSON can't serialize bigint: `JSON.stringify(x, (_, v) => typeof v === 'bigint' ? v.toString() : v)`.

## 8. Errors
```js
import { BaseError, UserRejectedRequestError, ContractFunctionRevertedError, InsufficientFundsError } from 'viem'
export function humanError(err) {
  if (!err) return null
  if (err instanceof BaseError) {
    if (err.walk(e => e instanceof UserRejectedRequestError)) return 'Request rejected in wallet.'
    if (err.walk(e => e instanceof InsufficientFundsError)) return 'Not enough native token for gas/value.'
    const rev = err.walk(e => e instanceof ContractFunctionRevertedError)
    if (rev) return rev.reason                       // require("msg") / revert("msg") string
      ?? (rev.data ? `${rev.data.errorName}(${(rev.data.args ?? []).join(', ')})` : 'Transaction reverted.') // custom error
    return err.shortMessage
  }
  return err.message ?? String(err)
}
```
- Custom errors decode only if they are in the ABI passed to the call: include the contract's errors (and OZ errors like `ERC20InsufficientAllowance`) in the ABI - artifact ABIs include them.
- Map known reasons to friendly text (`'STK: locked'` -> "Still locked until <date>").
- Wrong network: wagmi throws `ChainMismatchError`/`ConnectorChainMismatchError` when `chainId` is passed and wallet is elsewhere -> show switch button.
- Log full error to console; show short message to user.

## 9. Allowance + approve flow
```js
const { data: allowance, queryKey: allowKey } = useReadContract({
  address: token, abi: erc20Abi, functionName: 'allowance', args: [address, spender],
  chainId: TARGET.id, query: { enabled: !!address && !!spender } })
const needsApprove = amt != null && (allowance ?? 0n) < amt
// UI: one button that says "Approve" then "Stake"; after approve receipt -> invalidate allowKey
```
- Approve exact `amt` by default; offer "unlimited" as explicit opt-in (`maxUint256` from viem).
- After approve receipt, refetch allowance BEFORE enabling the action (avoid racing a stale value).
- Tokens like USDT require approve to 0 before changing a non-zero allowance.
- For taxed/fee-on-transfer tokens, quote outputs after tax and use the router's `...SupportingFeeOnTransferTokens` functions.
- Approve the contract that actually calls `transferFrom` (router, staking), not the token or pair.

## 10. Freshness, caching, RPC load
- After success: `queryClient.invalidateQueries({ queryKey: balKey })` (query keys come from the hook result), or call each hook's `refetch()`.
```js
const qc = useQueryClient()
useEffect(() => { if (receipt.isSuccess) [balKey, allowKey, earnedKey].forEach(k => qc.invalidateQueries({ queryKey: k })) }, [receipt.isSuccess])
```
- Ticking values (earned rewards): refetch on new block cheaply, or interpolate locally from `rewardRate` and refetch every 15-30 s:
```js
const { data: block } = useBlockNumber({ watch: true, chainId: TARGET.id })
useEffect(() => { refetchEarned() }, [block])   // only for a few critical reads
```
- Don't put `refetchInterval: 1000` on many reads; prefer `staleTime` 10-30 s + invalidation on tx success + `refetchOnWindowFocus` (default true).
- Debounce quote reads on input (300 ms) so typing doesn't fire a call per keystroke.
- Static values (hardCap, decimals, endTime): `staleTime: Infinity`.
- Public RPCs rate-limit: use `fallback([...])` transports and batching (multicall).

## 11. Events
```js
useWatchContractEvent({ address: cfg.staking, abi: stakingAbi, eventName: 'Staked', chainId: TARGET.id,
  onLogs(logs) { /* update feed, invalidate totals */ } })
```
- History: `getContractEvents` / `useContractEvents` with `fromBlock` (bounded range; many RPCs cap ~2-10k blocks per call -> paginate).
- Events are hints for UI; re-read state from contract for truth.
- Polling-based watching costs RPC calls; enable only on pages that need it.

## 12. Security in the frontend
- Never trust frontend-computed limits (max buy, cooldowns); contract enforces.
- Verify `chainId` + contract addresses come from your config, not URL params or user input.
- Show the exact action, amount and spender before signing; avoid `eth_sign`/blind signatures; use EIP-712 typed data (`useSignTypedData`) with domain `chainId` and `verifyingContract`.
- Sanitize anything rendered from chain (token names, NFT metadata) - React escapes by default; never `dangerouslySetInnerHTML` with it.
- No private keys in frontend, localStorage or vite env. Server-side signing lives in a backend.
- Pin dependencies (lockfile) - wallet libs are supply-chain targets.

## 13. Game integration tips
- Keep game state offchain; call wallet only for meaningful actions (claim, mint, stake).
- Optimistic UI: show "pending" item state, finalize on receipt, roll back on error.
- Wrap tx steps in game feedback (see game-design.md section 12).

## Checklist
- [ ] Hooks used match wagmi 3 exports (`useConnection`, not v1-only APIs).
- [ ] Chains imported from `viem/chains` (multicall3 present) or defined with `defineChain`.
- [ ] Every read/write passes `chainId: TARGET.id`; wrong-network banner + switch button; writes disabled on wrong chain.
- [ ] Every `useReadContract` with `args` has `query.enabled` guarding undefined values.
- [ ] ABIs come from compiled artifacts / `erc20Abi`; every called function and custom error is present.
- [ ] Writes go simulate -> write -> wait; buttons disabled until simulation succeeds.
- [ ] Separate write hook (or tracked action) per action; no double submit.
- [ ] All amounts bigint; `parseUnits` input validated; slippage computed in bigint.
- [ ] Allowance read -> approve exact -> refetch -> action.
- [ ] Taxed tokens use SupportingFeeOnTransfer router functions; swaps include `amountOutMin` + deadline.
- [ ] Errors mapped to human text (rejected, insufficient funds, revert name, wrong chain).
- [ ] Reads invalidated after receipt success.
- [ ] No 1 s polling across many reads; multicall used for groups.
- [ ] Pending tx state shows explorer link; reverted receipt handled.
- [ ] No secrets in frontend; addresses only from config tied to chainId.
- [ ] `vite build` passes and lint (oxlint) clean.
