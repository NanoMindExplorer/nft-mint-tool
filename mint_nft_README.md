# mint_nft.py — safe manual NFT minting, multi-chain

Tool: [mint_nft.py](sandbox:/workspace/boltbook/tools/mint_nft.py) (`~/workspace/boltbook/tools/mint_nft.py`)
Previews/assessments: `~/workspace/boltbook/tools/mint_previews/`
Registry (appended on a verified successful mint): `~/workspace/minted_portfolio_registry.json`

## The two-step flow (nothing is ever sent without it)

1. **`prepare` — read-only.** Runs the Contract-Read Gate, simulates the
   mint from Iman's wallet on the chain's RPCs, and writes a preview JSON.
   It sends nothing, signs nothing, spends no gas. The preview shows:
   chain, contract, function + args + calldata, count, value (in the
   chain's currency), gas estimate + cost, wallet balance and whether it
   covers value + gas, the gate evidence, and warnings.
2. **`execute --preview <file> --confirm` — the only way to send.**
   Re-verifies everything fresh (simulation, quota, balance, baw
   preview), then sends via the Binance Agentic Wallet CLI, waits for
   the receipt, verifies on 2 RPCs that supply / balance moved, and
   appends the mint to the registry. Without `--confirm` it refuses.
   Previews expire after 30 minutes.

`verify <txhash>` re-checks a receipt read-only (no send).

```bash
# 1. Prepare (read-only)
python3 ~/workspace/boltbook/tools/mint_nft.py prepare --chain bsc \
  --contract 0xf2a6d1a5da92d611cf8ec614548d4d6e83beb6d8 --name HATCHIEggy

# 2. Read the preview, then — only after confirming it yourself —
python3 ~/workspace/boltbook/tools/mint_nft.py execute --chain bsc \
  --preview ~/workspace/boltbook/tools/mint_previews/bsc-hatchieggy-*.json --confirm
```

`--chain` is accepted before or after the subcommand; default is `bsc`.
Batch prepare: `prepare --batch lanes.json` — each lane is
`{"chain", "contract", "name", optional "count", "allow_paid", "value",
"allow_unverified"}`; each lane still needs its own `execute --confirm`.

## Safety rules (from Iman — enforced in code, not just docs)

- **Preview + `--confirm`, always.** No `--confirm`, no send. No
  auto-mint, no cron mode, no loop hook. Manual only.
- **Contract-Read Gate first.** Refuses contracts with no code, no
  discoverable mint/free-claim function, or a reverting simulation
  (the revert reason is printed). Verification tiers: Sourcify-verified
  passes clean; a standard-template contract with a mint decoded from
  its own bytecode + passing sim passes with a warning; a non-template
  Sourcify-unverified contract refuses unless `--allow-unverified`;
  no discoverable mint function always refuses.
- **Free-only by default.** Value is 0 unless `--allow-paid` AND an
  exact `--value` in the chain's native currency (or on-chain PRICE).
  A paid mint is refused early if the wallet balance is below the value.
- **Idempotency.** Refuses when the wallet already used its free quota
  (per-wallet getter / balanceOf / simulation revert). A successful mint
  is recorded in the registry; a second prepare for the same project
  refuses on the registry key too.
- **No private keys, ever.** Signing is only via the Binance Agentic
  Wallet CLI (`baw`). If baw cannot sign on a chain, `prepare` may still
  run (read-only) but `execute` refuses. No key-handling fallback.
- **ETH gas warning.** Mainnet ETH gas is expensive; every ETH preview
  carries the gas-cost estimate prominently.

## Examples, one per chain

```bash
# BSC — free mint (HATCHIEggy): passes, mint(uint256) [1]
python3 mint_nft.py prepare --chain bsc \
  --contract 0xf2a6d1a5da92d611cf8ec614548d4d6e83beb6d8 --name HATCHIEggy

# ETH — paid mint (needs --allow-paid + exact value; refuses early here
# because the wallet holds 0 ETH — top up first)
python3 mint_nft.py prepare --chain eth \
  --contract 0xf43335169127B7017EbeDB86218096Ae465a678c --name NFT5050 \
  --allow-paid --value 0.02

# Base — free mint behind an EIP-1167 proxy (FreeMint): needs
# --allow-unverified (Sourcify-unverified, non-template); the mint is
# read from the implementation contract: mintTo(address,uint256)
python3 mint_nft.py prepare --chain base \
  --contract 0x116491255cb4cd4da562a0dd0a3396c602362496 --name FreeMint \
  --allow-unverified

# Robinhood Chain — free mint check (BUCKET is a token, not a mint:
# correctly refuses with "no mint/free-claim function")
python3 mint_nft.py prepare --chain robinhood \
  --contract 0xbc9E7b1c5C0081f4aE85e71eC95703d3dEC9ffaD --name BUCKET

# Solana — read-only assessment only (never an executable preview)
python3 mint_nft.py prepare --chain solana \
  --contract CMACYFENjoBMHzapRXyo1JZkVS6EtaDDzkjMrmQLvr4J --name CoreCMProgram
```

## Per-chain status (tested 2026-11-06, read-only; no transaction sent on any chain)

| Chain | prepare | simulate | execute |
|---|---|---|---|
| **BSC** (56, BNB) | ✅ works | ✅ works (2-RPC) | ✅ code path complete (preview → re-verify → baw send → receipt → 2-RPC movement check → registry). Not yet run live — needs Iman's explicit per-mint confirmation. |
| **ETH** (1, ETH) | ✅ works | ✅ works (2-RPC) | ✅ code path complete. Blocked in practice: wallet holds 0 ETH, so any paid mint refuses early ("top up first") and gas is expensive. |
| **Base** (8453, ETH) | ✅ works | ✅ works (2-RPC) | ✅ code path complete. Blocked in practice: wallet holds 0 ETH on Base (preview shows `covers: False`). |
| **Robinhood** (4663, ETH) | ✅ works (live RPC verified: chainId 0x1237, block 107,353,467) | ✅ works (2-RPC) | ✅ code path complete; baw lists 4663. No live mintable contract tested yet — BUCKET correctly refused (token, no mint). |
| **Solana** (SOL) | ✅ assessment only (account identification + state read) | ⚠️ read-only assessment, no `simulateTransaction` mint simulation | ❌ **refuses by design.** Candy Machine instruction construction (guard evaluation + PDA derivation + borsh layout) is not verified in this tool, so no possibly-wrong transaction is built. Wallet holds 0 SOL. |

## What the testing caught (bugs found and fixed)

- **4byte `mint()` collision (dangerous).** Selector `0x1249c58b`'s first
  4byte match is `mint()`, but it is a no-arg function whose eth_call
  "passes" because it does not revert. The tool briefly built a preview
  with that calldata — it would have minted nothing. Fixed: only
  `mint(uint256)`, `mint(address,uint256)` and `mintTo(address,uint256)`
  are accepted mint shapes; a bare `mint()` never is. The poisoned
  preview was deleted.
- **Bare `mint()` exception, corroborated only (2026-11-06, SEAT).**
  Some contracts' REAL mint function is bare `mint()` (SEAT / Agent
  Arena: one seat per Agent ID). The blanket ban blocked it, so the
  tool now allows bare `mint()` only when corroborated:
  `mint()` present in the contract's own decoded selectors, at least
  two mint-state getters present (`seatOf`/`mintedBy`/`totalMinted`/
  `mintOpen`/`totalSupply`/…), and the wallet's seat unused
  (`seatOf == 0`, `mintedBy == 0x0`). The simulation (all reachable
  RPCs must pass) and the quota check still guard the path, and the
  corroboration evidence is recorded in the preview's gate. A
  collision-style contract (bare `mint()` + no mint-state getters)
  is still refused — verified by unit test. Fixed one real bug while
  building it: `cast` returns an address as a 32-byte word, so the
  `mintedBy` zero-address comparison must use integer value, not
  string equality.
- **Proxy fallback false-pass.** Base FreeMint's `mint(uint256)` sim
  "passed" because the call hit the implementation's fallback and did
  nothing. Fixed: EIP-1167 proxies are resolved to their implementation
  contract and selectors are decoded from ITS bytecode; the real mint
  is `mintTo(address,uint256)` (verified: `mintPrice() == 0`,
  `mintTo(wallet,1)` passes, `mintRandomTo` reverts `InvalidMintingType`).
  The stale false-pass preview was deleted.
- **Template variants.** The freemint template has at least two
  variants: Saturnode exposes all getters (`FREE_SUPPLY`,
  `FREE_PER_WALLET`…), HATCHIEggy's `freeClaimed` / `FREE_SUPPLY` /
  `FREE_PER_WALLET` all revert, and KING's `totalSupply` / `MAX_SUPPLY` /
  `PRICE` all revert (only `mintOpen` reads). Template detection now
  requires `mintOpen + MAX_SUPPLY + totalSupply`; the decoded-selector
  path covers the rest. KING's `mint(uint256)` sim correctly reverts
  `per-wallet cap`.
- **`cast selectors` parsing.** The output is tab-separated with a
  space-padded, paren-less middle column; no-arg functions have an
  empty middle column that `split()` collapses. The parser now splits
  on tabs and strips.
- **Selector cap.** The 4byte lookup was capped at the first 20
  interesting selectors; NFT5050 (ETH) has 68, so its `mint(uint256)`
  was missed. Now all selectors resolve from the local cache; only
  live 4byte lookups are capped at 20.
- **Solana `getBalance` shape.** Returns `{"context":…, "value": …}` —
  the value is now extracted.

## Test evidence (2026-11-06, all read-only)

- BSC HATCHIEggy `0xf2a6d1a5…`: PASS — `mint(uint256)` args `[1]`,
  gas 137159 @ 50000000 wei → ~0.000010 BNB, balance 0.000414 BNB covers.
  Preview `mint_previews/bsc-hatchieggy-20261106-133620.json`.
- BSC Saturnode `0x6124b3ae…`: REFUSED — `mint(uint256)` value 0 reverts
  `WrongPayment` (0x788a686f) on both RPCs (free quota used).
- BSC KING `0x2a2463d2…`: REFUSED — `mint(uint256)` value 0 reverts
  `per-wallet cap` on both RPCs (free quota used).
- Base FreeMint `0x11649125…`: REFUSED without flag (Sourcify-unverified,
  non-template); PASSES with `--allow-unverified` —
  `mintTo(address,uint256)` args `[wallet, 1]`, sim PASS, proxy warning
  shown. Preview `mint_previews/base-freemint-20261106-133655.json`.
- ETH USDC `0xA0b86991…`: REFUSED — no mint/free-claim function (proxy
  bytecode exposes no mint selector).
- Robinhood BUCKET `0xbc9E7b1c…`: REFUSED — no mint/free-claim function.
- ETH NFT5050 `0xf4333516…` paid 0.02: REFUSED early — wallet ETH 0 <
  value 0.02; top up first.
- Solana Core CM program `CMACYFEN…`: assessment — program itself, not
  a CM account; refuse with reason. Docs example CM `Ct5CWicv…`:
  account does NOT exist on mainnet. SPL Token program: program itself.
  Wallet 0 SOL.
- Execute guards: without `--confirm` → REFUSED (exit 2, nothing sent);
  `--chain solana --confirm` → REFUSED (not implemented, exit 2).
- Batch: 3 lanes (HATCHIEggy + FreeMint + BUCKET) → 2 ready, 1 refused.

## Limits (honest)

- `execute` has never sent a real transaction yet on any chain — the
  send path is code-complete and guarded, but its first live run must
  be a free mint on Iman's explicit order, with the preview in front
  of him. The receipt/movement verification assumes the mint emits
  standard Transfer events.
- Solana cannot mint through this tool at all (assessment only) until
  Candy Machine instruction construction is built and verified against
  a real Candy Machine account.
- Robinhood Chain has no tested live mintable contract yet.
- `verify` and the baw send path depend on the baw session being live;
  sessions expire — `execute` live-checks `wallet status` first and
  aborts if the session is down.
