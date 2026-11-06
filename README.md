# nft-mint-tool

Safe, manual, multi-chain NFT minting tool. Built 2026-11-06, proven live the
same day on BSC: Pulsekeep #1360 and SEAT #4148 minted free, 250,000 ARENA
claimed — all verified on 2 RPCs.

## What it does

- `prepare` — read-only contract-read gate + mint simulation, produces a
  preview JSON. Sends nothing.
- `execute --preview <file> --confirm` — re-verifies fresh, sends via the
  Binance Agentic Wallet, waits for the receipt, verifies ownership on
  2 RPCs, and registers the mint in the portfolio registry.
- `verify --tx ... --contract ...` — re-checks a past mint.

Chains: BSC, ETH, Base, Robinhood (EVM path), Solana (assessment-only —
execute refuses by design).

## Safety

- Free-mint default; paid mints need `--allow-paid`.
- Preview + explicit `--confirm` required; no cron, no auto-mint.
- Refuses when the free quota is spent, when simulation reverts, or when
  the mint function is not corroborated (bare `mint()` is accepted only
  with bytecode + mint-state-getter + seat-unused evidence).
- Never touches private keys or seed phrases; signing only via the
  Binance Agentic Wallet.

See `mint_nft_README.md` for the full manual.
