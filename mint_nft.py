#!/usr/bin/env python3
"""mint_nft.py — safe, manual NFT minting, MULTI-CHAIN, for Iman.

Chains: BSC, Ethereum, Base, Robinhood Chain (all EVM, one code path via
Foundry cast) and Solana (separate module — NOT EVM, no mint(uint256)).

Why this exists: the Boltbook loop only READS contracts. Nothing could SEND
a mint. This tool is the single, guarded place where a mint can happen —
and only after a human-readable preview has been shown and explicitly
confirmed by the user.

SAFETY RULES (from Iman; non-negotiable, enforced in code, not just docs):
  1. NOTHING is ever sent without a preview file + `--confirm`.
     `prepare` only reads the chain and writes a preview JSON; it sends
     nothing, signs nothing, spends no gas. `execute` refuses without
     `--confirm`. There is no auto-mint, no cron mode, no loop hook.
  2. `prepare` runs the Contract-Read Gate first: the contract must have
     code, a recognisable mint/free-claim function, and a PASSING read-only
     simulation. A reverting simulation refuses the prepare and prints the
     revert reason. Verification is tiered: Sourcify-verified passes
     clean; a standard-template contract whose mint function was decoded
     from its own bytecode (the same bar the Boltbook loop's gate uses)
     passes with a warning; a non-template Sourcify-unverified contract
     refuses unless `--allow-unverified` is passed explicitly; a contract
     with no discoverable mint function always refuses.
  3. Free-only by default. Value is 0 unless `--allow-paid` is passed AND
     an exact value (in the chain's native currency) is set (`--value`,
     or PRICE read from the contract). `execute` re-checks that the wallet
     balance covers value + gas before sending.
  4. Idempotency (EVM). `prepare` refuses when the wallet already used its
     free quota (per-wallet free-claim getter, or balanceOf >=
     FREE_PER_WALLET). A successful mint is recorded in
     ~/workspace/minted_portfolio_registry.json (same entry shape as
     minted_monitor_v2.py `add`, plus a `chain` field); a second prepare
     for the same project then refuses on the registry key as well.
  5. `execute` re-verifies everything FRESH before sending: simulation,
     quota, balance, and the baw preview (simulation + risk screen).
     It then waits for the receipt and verifies on 2 RPCs that
     totalSupply / balanceOf moved, and records the minted token IDs
     from the Transfer events in the receipt.
  6. No private keys, ever. Signing is ONLY via the Binance Agentic
     Wallet CLI (`npx --yes @binance/agentic-wallet`). If baw cannot sign
     on a chain, `prepare` may still run (read-only) but `execute`
     refuses with a clear message. There is no fallback to key handling.

Chain notes (all verified live from this machine, 2026-11-06):
  - BSC (56, BNB), Ethereum (1, ETH), Base (8453, ETH): public RPCs
    probed live (eth_chainId correct); Sourcify supports all three.
    baw `wallet chains` lists 56, 1, 8453 — signing supported.
  - Robinhood Chain (4663, ETH): Arbitrum Orbit L2, mainnet live since
    Jul 2026. Public RPC https://rpc.mainnet.chain.robinhood.com probed
    live (chainId 0x1237, block 107,353,467). Sourcify lists chain 4663.
    baw `wallet chains` lists 4663 — signing supported.
  - Solana (SOL): public RPC https://api.mainnet-beta.solana.com live
    (getHealth ok). Wallet JBF7RNVJ7XiqdoZrUmRN1312DBMQhSsJKTAfCgQgRfDP
    holds 0 SOL — no Solana mint can pay fees until funded. Solana has
    no generic mint(uint256): the module identifies the program that
    owns an account (Core Candy Machine, Candy Machine V3, Token,
    Metadata...) from a table of program IDs verified ON-CHAIN, reads
    the account state, and assesses the mint. Candy Machine instruction
    construction (guard evaluation + PDA derivation + borsh layout) is
    NOT yet verified in this tool, so Solana `execute` refuses with a
    clear reason rather than building a possibly-wrong transaction.
    No Solana behaviour here is faked: unknown program shapes refuse.
  - ETH mainnet gas is expensive: the preview prints the gas estimate
    in ETH prominently, and a paid mint still needs --allow-paid.

Usage:
  # EVM — prepare (read-only; writes a preview JSON, sends nothing):
  python3 mint_nft.py prepare --chain bsc --contract 0x... --name HATCHIEggy
  python3 mint_nft.py prepare --chain base --contract 0x... --name FreeMint
  python3 mint_nft.py prepare --chain eth --contract 0x... --name NAME --allow-paid --value 0.02
  python3 mint_nft.py prepare --batch lanes.json        # {"lanes": [{chain?, contract, name, ...}]}

  # Solana — prepare is a read-only assessment (no executable preview):
  python3 mint_nft.py prepare --chain solana --contract <candy-machine-address> --name NAME

  # Execute (needs the preview file AND --confirm; re-verifies fresh):
  python3 mint_nft.py execute --chain bsc --preview mint_previews/hatchieggy-....json --confirm

  # Verify a tx afterwards (read-only):
  python3 mint_nft.py verify --chain bsc --tx 0x... --contract 0x...
  python3 mint_nft.py verify --chain solana --tx <base58-signature>

Previews live in ~/workspace/boltbook/tools/mint_previews/ and are valid
for 30 minutes; `execute` refuses an older preview (re-run `prepare`).
Known limits:
  - `contract-call execute` can return PENDING_CONFIRMATION (approve in
    the Binance App). This tool has no result-polling command for that
    case: it saves the orderId/requestId and tells the user to confirm
    in-app, then `verify --tx <hash>` once the tx exists.
  - Gas is estimated with eth_estimateGas + eth_gasPrice at prepare and
    re-checked at execute; the baw backend estimates again at preview.
    The balance check uses a 1.5x gas margin.
  - The EVM gate reads via cast + the Sourcify v2 API + the local 4byte
    cache; contract_read.py (BSC-only, used by the Boltbook loop) is left
    untouched — this tool's gate reuses the same methods, chain-aware.
  - Token (ERC-20-style) free claims use the same flow when the contract
    exposes a zero-arg claim function; `held.amount` is then recorded
    from the balance delta instead of token IDs.
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

WIB = timezone(timedelta(hours=7))
TOOLS = os.path.dirname(os.path.abspath(__file__))
FOURBYTE_CACHE = os.path.join(TOOLS, "fourbyte_cache.json")
REGISTRY = os.path.expanduser("~/workspace/minted_portfolio_registry.json")
PORTFOLIO_MD = os.path.expanduser("~/workspace/minted_portfolio.md")
PREVIEW_DIR = os.path.join(TOOLS, "mint_previews")
PREVIEW_MAX_AGE_S = 1800  # 30 minutes
GAS_MARGIN = 1.5
TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDR_PADDED = "0x" + "0" * 64
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) mint_nft/1.0",
      "Content-Type": "application/json"}
BAW = ["npx", "--yes", "@binance/agentic-wallet"]

WALLET_EVM = "0xF02e76Ed10cB66CbD1E44b2Ddfe7e54802eFFCF8"
WALLET_SOL = "JBF7RNVJ7XiqdoZrUmRN1312DBMQhSsJKTAfCgQgRfDP"

# Chain table. RPCs are probed live, sequentially, never in parallel
# bursts (public endpoints rate-limit bursts). `sourcify` was checked
# against https://sourcify.dev/server/chains (all four EVM chains listed).
# `baw` was checked live via `baw wallet chains --json` (2026-11-06):
# 8453, 56, 1, 137, 4663, CT_501, 42161.
CHAINS = {
    "bsc": {
        "label": "BNB Smart Chain", "chain_id": 56, "currency": "BNB",
        "evm": True,
        "rpcs": ["https://bsc-dataseed1.bnbchain.org",
                 "https://bsc-dataseed2.defibit.io"],
        "explorer": "https://bscscan.com", "sourcify": True, "baw": "56",
        "wallet": WALLET_EVM,
    },
    "eth": {
        "label": "Ethereum", "chain_id": 1, "currency": "ETH",
        "evm": True,
        "rpcs": ["https://ethereum.publicnode.com",
                 "https://cloudflare-eth.com"],
        "explorer": "https://etherscan.io", "sourcify": True, "baw": "1",
        "wallet": WALLET_EVM,
    },
    "base": {
        "label": "Base", "chain_id": 8453, "currency": "ETH",
        "evm": True,
        "rpcs": ["https://mainnet.base.org",
                 "https://base.publicnode.com"],
        "explorer": "https://basescan.org", "sourcify": True, "baw": "8453",
        "wallet": WALLET_EVM,
    },
    "robinhood": {
        "label": "Robinhood Chain", "chain_id": 4663, "currency": "ETH",
        "evm": True,
        "rpcs": ["https://rpc.mainnet.chain.robinhood.com",
                 "https://robinhood-rpc.publicnode.com"],
        "explorer": "https://robinhoodchain.blockscout.com",
        "sourcify": True, "baw": "4663",
        "wallet": WALLET_EVM,
    },
    "solana": {
        "label": "Solana", "chain_id": None, "currency": "SOL",
        "evm": False,
        "rpcs": ["https://api.mainnet-beta.solana.com"],
        "explorer": "https://solscan.io", "sourcify": False, "baw": "CT_501",
        "wallet": WALLET_SOL,
    },
}

# Solana program IDs — every one verified ON-CHAIN via getAccountInfo
# (executable=true, owner BPF Loader) on 2026-11-06, from Metaplex docs
# (Core CM + Guard typedoc; CMv3 + Guard v3 Amman configs + on-chain
# check — note: a second CMv3-looking string circulating online,
# ...dBYaPJrtzc, has NO account on-chain and is NOT used here).
SOLANA_PROGRAMS = {
    "CMACYFENjoBMHzapRXyo1JZkVS6EtaDDzkjMrmQLvr4J":
        ("Core Candy Machine", "candy-machine-core"),
    "CMAGAKJ67e9hRZgfC5SFTbZH8MgEmtqazKXjmkaJjWTJ":
        ("Core Candy Guard", "candy-guard-core"),
    "CndyV3LdqHUfDLmE5naZjVN8rBZz4tqhdefbAnjHG3JR":
        ("Candy Machine V3 (deprecated, Token Metadata NFTs)", "candy-machine-v3"),
    "Guard1JwRhJkVH6XZhzoYxeBVQe872VH6QggF4BWmS9g":
        ("Candy Guard V3", "candy-guard-v3"),
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA":
        ("SPL Token program", "spl-token"),
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb":
        ("SPL Token-2022 program", "spl-token-2022"),
    "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s":
        ("Metaplex Token Metadata", "token-metadata"),
    "11111111111111111111111111111111":
        ("System program", "system"),
}

# (EVM template reads use `cast call` with full signatures, not raw
# selectors — no selector table is kept here; see onchain_scan.py.)
FREE_CLAIM_FNS = ["claimFree", "freeMint", "claim", "freeClaim", "claimFreeMint"]
# Only these mint arities are real free-mint shapes in this ecosystem.
# A bare mint() is NOT one: 4byte's first match for selector 0x1249c58b
# is "mint()", but that selector is a no-arg function whose eth_call
# "passes" because it does not revert — simulating it would build an
# execute that mints nothing. mint(string,uint256) is a metadata mint,
# not a free claim. mintTo(address,uint256) IS a real shape (Base
# FreeMint's implementation mints through it, mintPrice() == 0); a
# permissioned or paid mintTo still fails at simulation, so the sim
# remains the guard. Anything else is skipped, never guessed.
MINT_SIGNATURES = {"mint(uint256)", "mint(address,uint256)",
                   "mintTo(address,uint256)"}
# Bare mint() corroboration (2026-11-06, SEAT / Agent Arena): a bare
# mint() is normally a 4byte collision (see MINT_SIGNATURES) — but some
# contracts' REAL mint function is bare mint(). It is only treated as
# a mint when ALL of these hold: (a) "mint()" is in THIS contract's
# decoded selectors (bytecode-present, not a guess); (b) at least two
# mint-state getters are present in the decoded names — a collision
# contract whose 0x1249c58b is some unrelated no-arg function has none
# of these; (c) when the seat-style getters exist, the wallet's seat is
# still unused (seatOf == 0 and mintedBy == 0x0). The simulation (all
# reachable RPCs must pass) and the quota check still guard the path
# afterwards. Returns (ok, evidence); evidence carries "reason" on
# failure so a refusal can say exactly which condition was missing.
BARE_MINT_STATE_GETTERS = {"seatOf", "mintedBy", "totalMinted", "mintOpen",
                           "totalSupply", "freeClaimed", "claimedFree",
                           "freeMinted", "hasClaimed"}
BARE_MINT_MIN_STATE_GETTERS = 2


def bare_mint_corroborated(chain, contract, decoded_sigs, wallet):
    """Corroboration check for a bare mint(). Read-only."""
    decoded_names = {s.split("(")[0] for s in (decoded_sigs or {}).values()}
    if "mint()" not in (decoded_sigs or {}).values():
        return False, {"reason": "mint() not in decoded selectors"}
    present = sorted(BARE_MINT_STATE_GETTERS & decoded_names)
    if len(present) < BARE_MINT_MIN_STATE_GETTERS:
        return False, {"reason": "only %d mint-state getter(s) present: %s"
                       % (len(present), present),
                       "state_getters": present}
    evidence = {"state_getters": present}
    if "seatOf" in decoded_names:
        res = evm_uint(chain, contract, "seatOf(address)(uint256)",
                       [wallet], wallet)
        evidence["seatOf"] = res["value"]
        if res["value"] is None:
            return False, dict(evidence,
                               reason="seatOf(wallet) unreadable")
        if res["value"] != 0:
            return False, dict(evidence, reason=(
                f"seatOf(wallet)={res['value']} (seat already held)"))
    if "mintedBy" in decoded_names:
        res = evm_call_sig(chain, contract,
                           "mintedBy(address)(address)", [wallet], wallet)
        vals = [p.get("value") for p in res["per_rpc"]
                if p["ok"] and p.get("value")]
        evidence["mintedBy"] = vals[0] if vals else None
        if not vals:
            return False, dict(evidence,
                               reason="mintedBy(wallet) unreadable")
        # cast returns an address as a full 32-byte word (64 hex chars),
        # not a 20-byte address — compare by integer value, not string.
        if int(vals[0], 0) != 0:
            return False, dict(evidence, reason=(
                f"mintedBy(wallet)={vals[0]} (already minted)"))
    return True, evidence
QUOTA_GETTERS = [
    ("freeClaimedById", "freeClaimedById(address)(bool)"),
    ("freeClaimed", "freeClaimed(address)(bool)"),
    ("claimed", "claimed(address)(bool)"),
    ("hasClaimed", "hasClaimed(address)(bool)"),
    ("claimedFree", "claimedFree(address)(bool)"),
    ("freeMinted", "freeMinted(address)(bool)"),
    ("freeClaimedByIdUint", "freeClaimedById(address)(uint256)"),
]
KNOWN_SELECTORS = {
    "mint(uint256)": "0xa0712d68",
    "balanceOf(address)": "0x70a08231",
    "freeClaimedById(address)": "0x8027dd44",
    "claimFree()": "0xf366afc9",
}


def now_wib():
    return datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "", name.lower())
    return s or "project"


def run(cmd, timeout=120):
    """Run a command; return (rc, stdout, stderr) as text. Never raises."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout or "", r.stderr or ""
    except Exception as e:
        return -1, "", f"{type(e).__name__}: {e}"


def cast_path():
    for p in (os.path.expanduser("~/.foundry/bin/cast"), "cast"):
        if os.path.exists(p) or p == "cast":
            return p
    return "cast"


def cast_sig(signature):
    """4-byte selector for a function signature, via `cast sig` (local keccak)."""
    rc, out, _ = run([cast_path(), "sig", signature], timeout=30)
    sel = (out or "").strip()
    if rc == 0 and re.fullmatch(r"0x[0-9a-f]{8}", sel):
        return sel
    return KNOWN_SELECTORS.get(signature)


def encode_call(signature, args):
    """Calldata: selector + 32-byte big-endian uint/address args."""
    sel = cast_sig(signature)
    if not sel:
        raise ValueError(f"cannot compute selector for {signature}")
    data = sel[2:]
    for arg in args:
        if isinstance(arg, str) and arg.startswith("0x"):
            data += arg[2:].rjust(64, "0")
        else:
            data += f"{int(arg):064x}"
    return "0x" + data


def rpc_json(rpc, method, params, timeout=60):
    """One JSON-RPC call (sequential; never parallel bursts).
    A User-Agent header is REQUIRED: several public endpoints 403
    bare-urllib requests (found 2026-11-06). Returns `result` or raises."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    req = urllib.request.Request(rpc, data=body, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.load(r)
    except Exception as e:
        raise RuntimeError(f"RPC {rpc} unreachable: {e}")
    if "error" in payload:
        raise RuntimeError(f"RPC {rpc} error: {payload['error']}")
    return payload.get("result")


def fetch_json(url, timeout=30):
    """GET JSON (Sourcify etc.). Returns (status, parsed_or_None, error)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r), None
    except urllib.error.HTTPError as e:
        return e.code, None, f"HTTP {e.code}"
    except Exception as e:
        return None, None, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- baw (signing route)
def baw(*args, timeout=180):
    """Run a baw command with --json. Returns parsed `data`.
    Raises RuntimeError on CLI failure or success=false."""
    rc, out, err = run(BAW + list(args) + ["--json"], timeout=timeout)
    try:
        data = json.loads(out)
    except Exception:
        raise RuntimeError(f"baw {' '.join(args)}: unparsable output "
                           f"(rc={rc}): {(out or err or '')[:300]}")
    if not data.get("success"):
        e = data.get("error") or {}
        raise RuntimeError(f"baw {' '.join(args)} failed: "
                           f"{e.get('code')} {e.get('message') or e}")
    return data.get("data") or {}


_BAW_CHAINS = None


def baw_chains():
    """Live list of binanceChainId values baw can sign on (cached per run)."""
    global _BAW_CHAINS
    if _BAW_CHAINS is None:
        try:
            _BAW_CHAINS = [c.get("binanceChainId")
                           for c in baw("wallet", "chains")]
        except Exception as e:
            raise RuntimeError(f"cannot list baw chains: {e}")
    return _BAW_CHAINS


def wallet_status():
    """Live session check. Returns (connected_bool, detail)."""
    try:
        d = baw("wallet", "status")
        return d.get("status") == "CONNECTED", d
    except Exception as e:
        return False, {"error": str(e)}


def devmode_ok():
    """Returns (enabled_bool, detail)."""
    try:
        d = baw("wallet", "settings")
        dm = d.get("devMode") or {}
        return bool(dm.get("enabled")), dm
    except Exception as e:
        return False, {"error": str(e)}


def wallet_checks_or_raise():
    """Session + devMode pre-checks (read-only). Refuses clearly if the
    session is dead or Developer Mode is off/expired."""
    connected, st = wallet_status()
    if not connected:
        raise RuntimeError(
            "Binance wallet session is NOT connected (status: "
            f"{st}). Re-sign in via the Binance App pairing flow, then retry.")
    enabled, dm = devmode_ok()
    if not enabled:
        raise RuntimeError(
            "Developer Mode is OFF or expired "
            f"({dm}). Enable it in the Binance App: Agentic Wallet "
            "management page -> Settings -> Developer Mode, then retry.")


def baw_supports_chain(chain):
    """True when baw lists this chain as signable. Prepare does not need
    this (read-only); execute refuses when it is False."""
    return chain.get("baw") in baw_chains()


# ---------------------------------------------------------------- EVM gate
def sourcify_check(chain, contract):
    """Sourcify v2 match status for this chain. Returns
    {"supported": bool, "match": str|None, "verified": bool, "detail": str}.
    A 404 means 'not verified' when the chain IS supported; when the chain
    is not in Sourcify's list, supported=False."""
    if not chain.get("sourcify"):
        return {"supported": False, "match": None, "verified": False,
                "detail": "Sourcify does not list this chain"}
    url = (f"https://sourcify.dev/server/v2/contract/"
           f"{chain['chain_id']}/{contract}")
    status, data, err = fetch_json(url)
    if status == 200 and isinstance(data, dict):
        m = data.get("match")
        verified = m in ("match", "partial", "exact_match", "partial_match")
        return {"supported": True, "match": m, "verified": verified,
                "detail": f"match={m}"}
    if status == 404:
        return {"supported": True, "match": None, "verified": False,
                "detail": "not verified on Sourcify (404)"}
    return {"supported": True, "match": None, "verified": False,
            "detail": f"Sourcify check failed: {err or status}"}


def evm_code_bytes(chain, contract):
    """(bytes, agree_bool) from `cast code` on the chain's RPCs."""
    vals = []
    per_rpc = []
    for rpc in chain["rpcs"]:
        rc, out, err = run([cast_path(), "code", contract, "--rpc-url", rpc],
                           timeout=60)
        hexcode = (out or "").strip()
        if rc == 0 and hexcode.startswith("0x"):
            n = (len(hexcode) - 2) // 2
            vals.append(n)
            per_rpc.append({"rpc": rpc, "ok": True, "bytes": n})
        else:
            per_rpc.append({"rpc": rpc, "ok": False,
                            "error": (err or out or "cast code failed")[:200]})
    if not vals:
        return None, False, per_rpc
    return vals[0], len(set(vals)) == 1, per_rpc


def fourbyte_lookup(selector):
    """Live `cast 4byte` lookup (4byte.directory, free, no key).
    Returns the first plausible signature, or None."""
    rc, out, _ = run([cast_path(), "4byte", selector], timeout=30)
    if rc != 0:
        return None
    for line in (out or "").splitlines():
        s = line.strip()
        if re.fullmatch(r"[a-z][a-zA-Z0-9_]*\([^)]*\)", s):
            return s
    return None


def evm_decoded_selectors(chain, contract):
    """selector -> FULL function signature, ONLY for selectors present in
    THIS contract's bytecode. `cast selectors` prints selector + input
    types + mutability (no names); the full signatures come from the local
    4byte cache built by contract_read.py. A selector absent from the cache
    stays unknown and is never simulated. Cache signatures are probable
    (4byte.directory), not verified source. The full signature matters:
    a token's `mint(address,uint256)` is NOT the freemint template's
    `mint(uint256)` — calling the wrong arity reverts and would be
    misread as 'mint is broken'. Returns (decoded_map, impl_address):
    for an EIP-1167 minimal proxy (45-byte clone) the selectors are read
    from the IMPLEMENTATION contract, because the proxy bytecode itself
    has no selectors — it only delegatecalls."""
    cache = {}
    try:
        cache = json.load(open(FOURBYTE_CACHE))
    except Exception:
        pass
    code = None
    for rpc in chain["rpcs"]:
        rc, out, _ = run([cast_path(), "code", contract, "--rpc-url", rpc],
                         timeout=60)
        c = (out or "").strip()
        if rc == 0 and c and c != "0x":
            code = c
            break
    if not code:
        return {}, None
    impl = None
    hexcode = code[2:] if code.startswith("0x") else code
    # EIP-1167 minimal proxy: 363d3d373d3d3d363d73 <20-byte impl>
    # 5af43d82803e903d91602b57fd5bf3 (45 bytes total). The implementation
    # address is bytes 10..30 of the proxy bytecode.
    if (len(hexcode) == 90 and hexcode.startswith("363d3d373d3d3d363d73")
            and hexcode.endswith("5af43d82803e903d91602b57fd5bf3")):
        impl = "0x" + hexcode[20:60]
        for rpc in chain["rpcs"]:
            rc, out, _ = run([cast_path(), "code", impl, "--rpc-url", rpc],
                             timeout=60)
            c = (out or "").strip()
            if rc == 0 and c and c != "0x":
                code = c
                break
        else:
            return {}, impl  # proxy whose implementation has no code
    rc, out, _ = run([cast_path(), "selectors", code], timeout=60)
    if rc != 0:
        return {}, impl
    # `cast selectors` prints TAB-separated: selector, INPUT types (no
    # parens), mutability. An empty middle column means a no-arg function.
    # Only selectors whose arity could be a mint / quota / free-claim
    # function get a live 4byte name lookup — resolving every selector
    # costs a HTTP call each and a template contract has dozens.
    INTERESTING = {"", "uint256", "address", "address,uint256"}
    sels = []
    for line in (out or "").splitlines():
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) >= 2 and parts[0].startswith("0x"):
            # middle column is space-padded ("uint256" + spaces) and is
            # all-spaces for no-arg functions — strip it.
            params = parts[1].strip() if len(parts) >= 3 else ""
            if params in INTERESTING:
                sels.append(parts[0])
    result = {}
    dirty = False
    live_lookups = 0
    # Resolve every interesting selector from the LOCAL cache (cheap);
    # only cache MISSES cost a live 4byte HTTP call, capped at 20.
    # Capping the whole list at 20 would miss a mint selector that sits
    # past the first 20 (NFT5050 has 68 interesting selectors).
    for sel in sels:
        sig = cache.get(sel)
        if not (isinstance(sig, str) and "(" in sig):
            if live_lookups >= 20:
                continue
            sig = fourbyte_lookup(sel)  # live resolution for cache gaps
            live_lookups += 1
            if sig:
                cache[sel] = sig
                dirty = True
        if sig:
            result[sel] = sig
    if dirty:
        try:  # best-effort write-back to the shared cache
            json.dump(cache, open(FOURBYTE_CACHE, "w"), indent=1, sort_keys=True)
        except Exception:
            pass
    return result, impl


def evm_call_raw(chain, contract, data_hex, wallet, value_wei=0):
    """Read-only eth_call of raw calldata on the chain's RPCs (sequential).
    Returns {"per_rpc": [{rpc, ok, value|detail}]} where a successful
    call's value is the first whitespace token of cast's output."""
    per_rpc = []
    for rpc in chain["rpcs"]:
        rc, out, err = run(
            [cast_path(), "call", contract, data_hex, "--from", wallet,
             "--value", str(value_wei), "--rpc-url", rpc], timeout=60)
        detail = ((out or "").strip() or (err or "").strip())
        per_rpc.append({"rpc": rpc, "ok": rc == 0,
                        "value": detail.split()[0] if rc == 0 and detail else None,
                        "detail": detail[:800]})
    return {"per_rpc": per_rpc}


def evm_call_sig(chain, contract, signature, args, wallet, value_wei=0):
    """Read-only eth_call by signature. Returns per-RPC results."""
    sel = cast_sig(signature)
    if not sel:
        return {"per_rpc": [{"rpc": r, "ok": False,
                              "error": f"no selector for {signature}"}
                             for r in chain["rpcs"]]}
    data = sel[2:]
    for arg in args:
        if isinstance(arg, str) and arg.startswith("0x"):
            data += arg[2:].rjust(64, "0")
        else:
            data += f"{int(arg):064x}"
    return evm_call_raw(chain, contract, "0x" + data, wallet, value_wei)


def evm_uint(chain, contract, signature, args, wallet):
    """uint/bool view call. Returns {"per_rpc": [...], "value": int|None}
    (first agreeing reachable value, or None)."""
    res = evm_call_sig(chain, contract, signature, args, wallet)
    vals = []
    for p in res["per_rpc"]:
        if p["ok"] and p.get("value"):
            try:
                vals.append(int(p["value"], 0))
            except ValueError:
                pass
    return {"per_rpc": res["per_rpc"], "value": vals[0] if vals else None}


def evm_template_read(chain, contract):
    """Standard-template fields via cast on the chain's RPCs.
    Uses full signatures (a bare 4-byte selector is NOT accepted by
    `cast call` as calldata). Returns {"status", "fields", "price",
    "free_left"}."""
    sigs = {
        "totalSupply": "totalSupply()(uint256)",
        "freeClaimed": "freeClaimed()(uint256)",
        "FREE_SUPPLY": "FREE_SUPPLY()(uint256)",
        "mintOpen": "mintOpen()(uint256)",
        "FREE_PER_WALLET": "FREE_PER_WALLET()(uint256)",
        "MAX_PER_WALLET": "MAX_PER_WALLET()(uint256)",
        "PRICE": "PRICE()(uint256)",
        "MAX_SUPPLY": "MAX_SUPPLY()(uint256)",
    }
    fields = {}
    for name, sig in sigs.items():
        vals = []
        for rpc in chain["rpcs"]:
            rc, out, _ = run([cast_path(), "call", contract, sig,
                              "--rpc-url", rpc], timeout=60)
            if rc == 0:
                try:
                    vals.append(int((out or "").strip().split()[0], 0))
                except Exception:
                    vals.append("revert/empty")
            else:
                vals.append("revert/empty")
        fields[name] = vals
    ints = lambda vs: [v for v in vs if isinstance(v, int)]
    price = None
    pv = ints(fields.get("PRICE", []))
    if pv:
        price = pv[0] / 10**18
    free_left = None
    fs, fc = ints(fields.get("FREE_SUPPLY", [])), ints(fields.get("freeClaimed", []))
    if fs and fc:
        free_left = fs[0] - fc[0]
    ok = any(isinstance(v, int) for vs in fields.values() for v in vs)
    return {"status": "ok" if ok else "failed", "fields": fields,
            "price": price, "free_left": free_left}


def sim_passes(sim):
    """A simulation passes when every REACHABLE rpc passes and at least one
    rpc was reachable. A network error is an error, never a pass."""
    reachable = [p for p in sim["per_rpc"]
                 if p["ok"] or "unreachable" not in (p.get("detail") or "").lower()]
    if not reachable:
        return False, "no RPC reachable for simulation"
    reverts = [p for p in sim["per_rpc"] if not p["ok"]]
    if reverts:
        reasons = "; ".join(
            f"{p['rpc'].split('//')[1].split('/')[0]}: "
            f"{(p.get('detail') or '')[:200]}" for p in reverts)
        return False, f"simulation reverts: {reasons}"
    return True, "passes on all reachable RPCs"


def evm_quota_check(chain, contract, wallet, decoded_names, template_ok):
    """Per-wallet free-quota checks. A getter is only trusted when its
    call SUCCEEDS; a revert/empty means the getter is absent, never
    'quota used'. Returns {"used": bool, "checks": [...], "balance_of": {...}}."""
    checks = []
    used = False
    names = set(decoded_names or [])
    for label, sig in QUOTA_GETTERS:
        fn = sig.split("(")[0]
        if fn not in names and not template_ok:
            continue
        res = evm_uint(chain, contract, sig, [wallet], wallet)
        if res["value"] is None:
            checks.append({"getter": label, "status": "absent/error"})
            continue
        is_used = bool(res["value"])
        if is_used:
            used = True
        checks.append({"getter": label,
                       "status": "used" if is_used else "free",
                       "value": res["value"], "per_rpc": res["per_rpc"]})
    bal = evm_uint(chain, contract, "balanceOf(address)(uint256)", [wallet], wallet)
    return {"used": used, "checks": checks,
            "balance_of": {"per_rpc": bal["per_rpc"], "value": bal["value"]}}


def evm_gas_estimate(chain, contract, calldata, value_wei, wallet):
    """eth_estimateGas + eth_gasPrice on the chain's RPCs (sequential).
    Returns {"gas", "gas_price_wei", "per_rpc"} using the MAX estimate and
    MAX gas price across reachable RPCs, or raises."""
    per_rpc = []
    for rpc in chain["rpcs"]:
        try:
            gas_hex = rpc_json(rpc, "eth_estimateGas", [{
                "from": wallet, "to": contract, "data": calldata,
                "value": hex(value_wei)}])
            price_hex = rpc_json(rpc, "eth_gasPrice", [])
            per_rpc.append({"rpc": rpc, "ok": True,
                            "gas": int(gas_hex, 16),
                            "gas_price_wei": int(price_hex, 16)})
        except Exception as e:
            per_rpc.append({"rpc": rpc, "ok": False, "error": str(e)[:200]})
    good = [p for p in per_rpc if p["ok"]]
    if not good:
        raise RuntimeError("gas estimation failed on all RPCs: "
                           + "; ".join(p["error"] for p in per_rpc))
    return {"gas": max(p["gas"] for p in good),
            "gas_price_wei": max(p["gas_price_wei"] for p in good),
            "per_rpc": per_rpc}


def evm_native_balance(chain, wallet):
    """Native-currency balance (wei) on the chain's RPCs via cast.
    Returns (wei, per_rpc). Disagreement across RPCs raises."""
    per_rpc = []
    for rpc in chain["rpcs"]:
        rc, out, err = run([cast_path(), "balance", wallet, "--rpc-url", rpc],
                           timeout=60)
        try:
            wei = int((out or "").strip())
            per_rpc.append({"rpc": rpc, "ok": True, "wei": wei})
        except Exception:
            per_rpc.append({"rpc": rpc, "ok": False,
                            "error": (err or out or "parse error")[:200]})
    good = [p["wei"] for p in per_rpc if p["ok"]]
    if not good:
        raise RuntimeError("could not read wallet balance on any RPC")
    if len(set(good)) != 1:
        raise RuntimeError(f"wallet balance disagrees across RPCs: {good}")
    return good[0], per_rpc


def args_for_sig(signature, count, wallet):
    """Args for a mint-family signature. Returns a list, or None for an
    unknown arity (never guessed)."""
    inner = signature[signature.index("(") + 1:signature.index(")")]
    params = [p for p in inner.split(",") if p]
    if params == ["uint256"]:
        return [count]
    if params == ["address", "uint256"]:
        return [wallet, count]
    if params == []:
        return []
    return None


def pick_function(chain, contract, count, value_wei, decoded_sigs,
                  template_ok, wallet, bare_mint=None):
    """Choose the mint function and simulate it. Order: the standard
    template's mint(uint256) first when template_ok (the known shape),
    then each decoded mint signature that is a real free-mint arity
    (MINT_SIGNATURES only), then a CORROBORATED bare mint() (see
    bare_mint_corroborated — an uncorroborated bare mint() is a 4byte
    collision, not a mint), then zero-arg free-claim functions at value 0
    (free mints only). Unknown mint arities are skipped, never guessed.
    Returns (fn_dict, sim) or (None, refusal_reason)."""
    decoded_names = {s.split("(")[0] for s in (decoded_sigs or {}).values()}
    candidates = []
    if template_ok:
        candidates.append({"name": "mint", "signature": "mint(uint256)",
                           "args": [count], "value_wei": value_wei})
    for sig in (decoded_sigs or {}).values():
        if sig not in MINT_SIGNATURES:
            continue
        args = args_for_sig(sig, count, wallet)
        if args is None:
            continue  # unknown mint arity — do not guess
        candidates.append({"name": sig.split("(")[0], "signature": sig,
                           "args": args, "value_wei": value_wei})
    if bare_mint and bare_mint[0]:
        candidates.append({"name": "mint", "signature": "mint()",
                           "args": [], "value_wei": value_wei,
                           "bare_mint_corroboration": bare_mint[1]})
    if value_wei == 0:
        for fn in FREE_CLAIM_FNS:
            if fn in decoded_names:
                candidates.append({"name": fn, "signature": f"{fn}()",
                                   "args": [], "value_wei": 0})
    if not candidates:
        return None, ("no mint/free-claim function found in this contract's "
                      "decoded selectors; refusing an unknown mint path")
    errors = []
    for cand in candidates:
        try:
            calldata = encode_call(cand["signature"], cand["args"])
        except ValueError as e:
            errors.append(f"{cand['signature']}: {e}")
            continue
        cand["calldata"] = calldata
        sim = evm_call_raw(chain, contract, calldata, wallet,
                           cand["value_wei"])
        ok, why = sim_passes(sim)
        cand["simulation"] = sim
        if ok:
            return cand, sim
        errors.append(f"{cand['signature']} value {cand['value_wei']}: {why}")
    return None, "every mint path reverts: " + " | ".join(errors)


# ---------------------------------------------------------------- EVM prepare
def do_prepare_evm(chain_name, contract, name, count=1, function=None,
                   allow_paid=False, value=None, allow_unverified=False,
                   out=None):
    """Read-only. Returns (preview_dict_or_None, refusal_reason_or_None)."""
    chain = CHAINS[chain_name]
    wallet = chain["wallet"]
    cur = chain["currency"]
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", contract):
        return None, "contract must be a full 0x address (40 hex chars)"
    wallet_checks_or_raise()
    print(f"[gate:{chain_name}] Sourcify + code ...", flush=True)
    src = sourcify_check(chain, contract)
    nbytes, agree, _code_rpc = evm_code_bytes(chain, contract)
    if not nbytes:
        return None, "contract has no code on any RPC (not a contract)"
    print(f"[gate:{chain_name}] template read + decoded selectors ...", flush=True)
    tpl = evm_template_read(chain, contract)
    fields = tpl.get("fields", {}) if tpl.get("status") == "ok" else {}
    # Template = the freemint template family, identified by its
    # distinguishing getters (mintOpen + MAX_SUPPLY + totalSupply).
    # totalSupply alone does NOT make a template — plain ERC-20s (USDC,
    # BUCKET) have totalSupply too, and their mint(address,uint256) is a
    # permissioned minter, not a free mint. And the free-accounting
    # getters vary by variant: HATCHIEggy's freeClaimed / FREE_SUPPLY /
    # FREE_PER_WALLET all revert, while Saturnode's all read — so they
    # cannot be required.
    def _has_int(fname):
        return any(isinstance(v, int) for v in fields.get(fname, []))
    template_ok = (_has_int("mintOpen") and _has_int("MAX_SUPPLY")
                   and _has_int("totalSupply"))
    decoded, impl = evm_decoded_selectors(chain, contract)
    decoded_names = {s.split("(")[0] for s in decoded.values()}
    warnings = []
    if impl:
        warnings.append(f"EIP-1167 minimal proxy — selectors read from "
                        f"implementation {impl}")
    # A bare "mint()" in the decoded names does NOT count on its own
    # (4byte collision, see MINT_SIGNATURES); only real mint arities
    # do — unless the bare mint is corroborated (SEAT-style no-arg
    # mint with mint-state getters + unused seat).
    bare_mint = bare_mint_corroborated(chain, contract, decoded, wallet)
    mint_discovered = (template_ok
                       or any(sig in MINT_SIGNATURES
                              for sig in decoded.values())
                       or any(fn in decoded_names for fn in FREE_CLAIM_FNS)
                       or bare_mint[0])
    if not mint_discovered:
        return None, ("no mint/free-claim function found in this contract's "
                      "decoded selectors and the standard-template read "
                      "failed; refusing an unknown contract")
    if not src["verified"]:
        if template_ok:
            warnings.append(
                f"Sourcify unverified ({src['detail']}); verified instead "
                "by own read: standard template + decoded mint selector + "
                "simulation (loop Contract-Read Gate bar)")
        elif not allow_unverified:
            return None, (
                "Sourcify status is NOT verified "
                f"({src['detail']}) and this is not a standard-template "
                "contract. Refusing an unverified contract. Re-run with "
                "--allow-unverified only if you have verified this "
                "contract another way (official site + explorer).")
        else:
            warnings.append(
                f"Sourcify unverified ({src['detail']}); "
                "--allow-unverified was passed explicitly")
    # --- value (native currency) ---
    if allow_paid:
        if value is None:
            if isinstance(tpl.get("price"), (int, float)):
                value = tpl["price"] * count
            else:
                return None, ("--allow-paid needs an exact --value "
                              "(contract PRICE is unreadable)")
        value_wei = int(round(value * 10**18))
        if value_wei <= 0:
            return None, "--value must be > 0 for a paid mint"
    else:
        value_wei = 0
        value = 0.0
    # --- balance early: a paid mint the wallet cannot afford refuses
    # here with a clear message (instead of a confusing node OutOfFunds
    # at simulation time). The same reading is reused for the preview.
    balance_wei, _bal_rpc = evm_native_balance(chain, wallet)
    if value_wei > 0 and balance_wei < value_wei:
        return None, (f"wallet {cur} balance {balance_wei / 10**18:.6f} is "
                      f"less than the mint value {value_wei / 10**18:.6f} "
                      f"{cur}; top up first")
    # --- function + simulation ---
    if function:
        fn_name = function.split("(")[0]
        if fn_name not in decoded_names and fn_name != "mint":
            return None, (f"--function {function} is not in this contract's "
                          "decoded selectors")
        if function == "mint()" and not bare_mint[0]:
            return None, ("bare mint() is only accepted when corroborated "
                          f"(mint-state getters + unused seat): "
                          f"{bare_mint[1].get('reason')}")
        fn_args = args_for_sig(function, count, wallet)
        if fn_args is None:
            return None, (f"--function {function} has an arity this tool "
                          "does not support (mint(uint256), "
                          "mint(address,uint256) or zero-arg only)")
        cand = {"name": fn_name, "signature": function,
                "args": fn_args, "value_wei": value_wei}
        if function == "mint()":
            cand["bare_mint_corroboration"] = bare_mint[1]
        cand["calldata"] = encode_call(cand["signature"], cand["args"])
        sim = evm_call_raw(chain, contract, cand["calldata"], wallet, value_wei)
        ok, why = sim_passes(sim)
        cand["simulation"] = sim
        if not ok:
            return None, f"simulation of {function} reverts: {why}"
        chosen = cand
    else:
        print(f"[gate:{chain_name}] simulating mint paths ...", flush=True)
        chosen, reason = pick_function(chain, contract, count, value_wei,
                                       decoded, template_ok, wallet,
                                       bare_mint=bare_mint)
        if chosen is None:
            return None, reason
    sim = chosen["simulation"]
    # --- idempotency / quota (free mints only) ---
    quota = evm_quota_check(chain, contract, wallet, decoded_names, template_ok)
    if value_wei == 0 and quota["used"]:
        used_by = [c["getter"] for c in quota["checks"]
                   if c.get("status") == "used"]
        return None, ("free quota already used by this wallet "
                      f"(getter(s): {', '.join(used_by)}); refusing a "
                      "double mint")
    free_per_wallet = None
    if template_ok:
        vals = [v for v in fields.get("FREE_PER_WALLET", [])
                if isinstance(v, int)]
        free_per_wallet = vals[0] if vals else None
    bal_val = quota["balance_of"].get("value")
    if (value_wei == 0 and isinstance(bal_val, int)
            and isinstance(free_per_wallet, int) and free_per_wallet > 0
            and bal_val >= free_per_wallet):
        return None, (f"wallet already holds {bal_val} of this NFT and "
                      f"FREE_PER_WALLET is {free_per_wallet}; free quota "
                      "is exhausted")
    if isinstance(bal_val, int) and bal_val > 0:
        warnings.append(f"wallet already holds {bal_val} of this NFT")
    # --- gas + balance (read-only; insufficient balance WARNS here,
    # execute hard-refuses) ---
    try:
        gas = evm_gas_estimate(chain, contract, chosen["calldata"], value_wei, wallet)
        gas_cost_wei = int(gas["gas"] * gas["gas_price_wei"] * GAS_MARGIN)
    except RuntimeError as e:
        return None, f"gas estimation failed (mint may revert on-chain): {e}"
    total_wei = value_wei + gas_cost_wei
    covers = balance_wei >= total_wei
    if not covers:
        warnings.append(
            f"wallet {cur} {balance_wei / 10**18:.6f} does NOT cover "
            f"value + gas ({total_wei / 10**18:.6f} {cur} at 1.5x gas "
            "margin); top up before executing, or execute will refuse")
    if chain_name == "eth":
        warnings.append(
            "ETH mainnet gas is expensive: this estimate is "
            f"{gas_cost_wei / 10**18:.6f} ETH for gas alone at 1.5x margin. "
            "Double-check the gas line before confirming anything.")
    preview = {
        "version": 2, "tool": "mint_nft.py prepare",
        "created_at_wib": now_wib(),
        "created_at_epoch": int(time.time()),
        "valid_until_epoch": int(time.time()) + PREVIEW_MAX_AGE_S,
        "chain": chain_name, "chain_id": chain["chain_id"],
        "currency": cur,
        "contract": contract, "name": name, "wallet": wallet,
        "function": {"name": chosen["name"],
                     "signature": chosen["signature"],
                     "args": chosen["args"], "calldata": chosen["calldata"]},
        "count": count if chosen["name"] == "mint" else 1,
        "value_wei": value_wei, "value": value_wei / 10**18,
        "paid": value_wei > 0,
        "gas": {"estimate": gas["gas"],
                "gas_price_wei": gas["gas_price_wei"],
                "gas_cost_wei": gas_cost_wei,
                "gas_cost": gas_cost_wei / 10**18,
                "margin": GAS_MARGIN, "per_rpc": gas["per_rpc"]},
        "balance": {"wei": balance_wei,
                    "amount": balance_wei / 10**18,
                    "covers_value_plus_gas": covers,
                    "total_needed_wei": total_wei,
                    "total_needed": total_wei / 10**18},
        "gate": {"code_bytes": nbytes, "code_agree": agree,
                 "sourcify_match": src["match"], "verified": src["verified"],
                 "sourcify_detail": src["detail"],
                 "template_ok": template_ok,
                 "proxy_impl": impl,
                 "template_fields": {k: v for k, v in fields.items()},
                 "simulation": sim, "quota": quota},
        "warnings": warnings,
        "status": "ready",
    }
    if chosen.get("bare_mint_corroboration"):
        preview["gate"]["bare_mint_corroboration"] = \
            chosen["bare_mint_corroboration"]
    os.makedirs(PREVIEW_DIR, exist_ok=True)
    if not out:
        out = os.path.join(
            PREVIEW_DIR,
            f"{chain_name}-{slugify(name)}-"
            f"{datetime.now(WIB).strftime('%Y%m%d-%H%M%S')}.json")
    with open(out, "w") as f:
        json.dump(preview, f, indent=1)
    preview["_path"] = out
    return preview, None


def print_preview(p):
    fn = p["function"]
    cur = p["currency"]
    print(f"\n===== MINT PREVIEW [{p['chain']}] (read-only — nothing sent) =====")
    print(f"  project : {p['name']}  ({p['contract']})")
    print(f"  chain   : {p['chain']} (id {p['chain_id']})  currency: {cur}")
    print(f"  function: {fn['signature']} args={fn['args']}")
    print(f"  calldata: {fn['calldata']}")
    print(f"  count   : {p['count']}  |  value: {p['value']:.6f} {cur} "
          f"({'PAID' if p['paid'] else 'FREE'})")
    g = p["gas"]
    print(f"  gas est : {g['estimate']} @ {g['gas_price_wei']} wei "
          f"-> ~{g['gas_cost']:.6f} {cur} (1.5x margin)")
    b = p["balance"]
    print(f"  balance : {b['amount']:.6f} {cur}  |  total needed: "
          f"{b['total_needed']:.6f} {cur}  |  covers: {b['covers_value_plus_gas']}")
    gate = p["gate"]
    sim_ok = all(r["ok"] for r in gate["simulation"]["per_rpc"])
    print(f"  gate    : code {gate['code_bytes']} bytes, "
          f"Sourcify={gate['sourcify_match']}, "
          f"simulation={'PASS' if sim_ok else 'REVERT'}")
    for w in p["warnings"]:
        print(f"  WARNING : {w}")
    print(f"  preview : {p['_path']} (valid 30 min, until "
          f"{datetime.fromtimestamp(p['valid_until_epoch'], tz=WIB).strftime('%H:%M WIB')})")
    print("  To execute (after YOU confirm this preview):")
    print(f"    python3 {os.path.join(TOOLS, 'mint_nft.py')} execute "
          f"--chain {p['chain']} --preview {p['_path']} --confirm")
    print("=" * 62 + "\n")


# ---------------------------------------------------------------- Solana (separate module — NOT EVM)
def sol_rpc(method, params, timeout=60):
    """One Solana JSON-RPC call on the public mainnet endpoint.
    Returns `result` or raises RuntimeError."""
    rpc = CHAINS["solana"]["rpcs"][0]
    return rpc_json(rpc, method, params, timeout=timeout)


def sol_get_account(address):
    """getAccountInfo (base64). Returns None when the account does not
    exist; else {"owner","lamports","data_len","executable","discriminator"}.
    The discriminator is raw first-8-bytes hex — reported, never decoded
    into fields whose offsets are not verified here."""
    try:
        res = sol_rpc("getAccountInfo", [address, {"encoding": "base64"}])
    except RuntimeError as e:
        raise RuntimeError(f"Solana RPC error: {e}")
    if not res or not res.get("value"):
        return None
    v = res["value"]
    data_b64 = (v.get("data") or ["", ""])[0]
    raw = base64.b64decode(data_b64) if data_b64 else b""
    return {"owner": v.get("owner"), "lamports": v.get("lamports"),
            "data_len": len(raw), "executable": bool(v.get("executable")),
            "discriminator": raw[:8].hex() if len(raw) >= 8 else None}


def sol_balance_lamports(address):
    """getBalance returns {"context": ..., "value": lamports}."""
    res = sol_rpc("getBalance", [address])
    if isinstance(res, dict):
        return res.get("value")
    return res


def sol_identify(address):
    """Classify a Solana account. Two cases: the address IS a known
    program (its owner is the BPF loader), or the account is OWNED by a
    known program (e.g. a Candy Machine account owned by the CM program).
    Returns (kind, program_name, account_or_None)."""
    if address in SOLANA_PROGRAMS:
        prog_name, kind = SOLANA_PROGRAMS[address]
        return kind + ":program", prog_name + " (program itself)", \
            sol_get_account(address)
    acct = sol_get_account(address)
    if acct is None:
        return "not_found", None, None
    owner = acct["owner"]
    if owner in SOLANA_PROGRAMS:
        prog_name, kind = SOLANA_PROGRAMS[owner]
        return kind, prog_name, acct
    if acct["executable"]:
        return "unknown_program", f"executable program owned by {owner}", acct
    return "unknown_account", f"account owned by {owner}", acct


def do_prepare_solana(address, name):
    """Read-only Solana assessment. There is NO executable preview on
    Solana in this tool: Candy Machine instruction construction (guard
    evaluation + PDA derivation + borsh layout) is not verified here, so
    rather than build a possibly-wrong transaction, prepare reports what
    the account IS and execute refuses. Returns (assessment, refusal)."""
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", address or ""):
        return None, "not a valid Solana base58 address (32-44 chars)"
    try:
        health = sol_rpc("getHealth", [])
    except RuntimeError as e:
        return None, f"Solana RPC is unreachable: {e}"
    if health != "ok":
        return None, f"Solana RPC health is not ok: {health}"
    kind, prog_name, acct = sol_identify(address)
    try:
        wallet_lamports = sol_balance_lamports(WALLET_SOL)
    except RuntimeError as e:
        wallet_lamports = None
        wallet_err = str(e)
    else:
        wallet_err = None
    assessment = {
        "version": 2, "tool": "mint_nft.py prepare (solana assessment)",
        "created_at_wib": now_wib(), "chain": "solana", "currency": "SOL",
        "contract": address, "name": name, "wallet": WALLET_SOL,
        "account": acct, "program_kind": kind, "program_name": prog_name,
        "wallet_lamports": wallet_lamports,
        "wallet_sol": (wallet_lamports / 10**9
                       if wallet_lamports is not None else None),
        "wallet_error": wallet_err,
        "status": "assessment_only",
    }
    if wallet_lamports == 0:
        assessment["warnings"] = [
            "wallet holds 0 SOL — no Solana mint can pay fees until funded"]
    else:
        assessment["warnings"] = []
    os.makedirs(PREVIEW_DIR, exist_ok=True)
    path = os.path.join(
        PREVIEW_DIR,
        f"solana-{slugify(name)}-"
        f"{datetime.now(WIB).strftime('%Y%m%d-%H%M%S')}.json")
    with open(path, "w") as f:
        json.dump(assessment, f, indent=1)
    assessment["_path"] = path
    print(f"\n===== SOLANA ASSESSMENT (read-only — nothing sent) =====")
    print(f"  address : {address}")
    print(f"  program : {prog_name or '(none)'} [{kind}]")
    if acct:
        print(f"  account : owner={acct['owner']} lamports={acct['lamports']} "
              f"data_len={acct['data_len']} executable={acct['executable']} "
              f"discriminator={acct['discriminator']}")
    print(f"  wallet  : {WALLET_SOL} = "
          f"{assessment['wallet_sol']} SOL"
          + (f" (error: {wallet_err})" if wallet_err else ""))
    if kind in ("candy-machine-core", "candy-machine-v3"):
        print("  verdict : RECOGNISED Candy Machine account, but mint "
              "execution is NOT implemented in this tool: building the mint "
              "transaction needs guard evaluation + PDA derivation + the "
              "borsh instruction layout, none of which is verified here. "
              "Refusing to build a possibly-wrong transaction.")
    elif kind == "not_found":
        print("  verdict : account does NOT exist on Solana mainnet.")
    elif kind.endswith(":program"):
        print("  verdict : this is the program itself, not a Candy Machine "
              "account — pass a Candy Machine ACCOUNT address to assess a "
              "mint, not the program ID.")
    else:
        print(f"  verdict : not a Candy Machine account ({kind}); no mint "
              "path exists for this program shape.")
    for w in assessment["warnings"]:
        print(f"  WARNING : {w}")
    print(f"  assessment saved to {path}")
    print("=" * 62 + "\n")
    if kind in ("candy-machine-core", "candy-machine-v3"):
        return assessment, None  # assessed, but not executable (see execute)
    return None, (f"Solana account is {kind} ({prog_name}); no mint path "
                  "for this program shape")


# ---------------------------------------------------------------- execute (EVM)
def read_total_supply(chain, contract):
    res = evm_uint(chain, contract, "totalSupply()(uint256)", [],
                   chain["wallet"])
    return res["value"], res


def wait_receipt(chain, tx_hash, timeout_s=300, interval_s=10):
    """Poll eth_getTransactionReceipt on the chain's RPCs.
    Returns the receipt dict, or None on timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for rpc in chain["rpcs"]:
            try:
                rcpt = rpc_json(rpc, "eth_getTransactionReceipt", [tx_hash])
            except Exception:
                continue
            if rcpt:
                rcpt["_rpc"] = rpc
                return rcpt
        time.sleep(interval_s)
    return None


def minted_token_ids(receipt, wallet):
    """Token IDs from mint Transfer events (from 0x0 to wallet)."""
    ids = []
    wpad = "0x" + wallet[2:].rjust(64, "0").lower()
    for log in receipt.get("logs", []):
        topics = [t.lower() for t in log.get("topics", [])]
        if (len(topics) == 4 and topics[0] == TRANSFER_TOPIC0
                and topics[1] == ZERO_ADDR_PADDED and topics[2] == wpad):
            try:
                ids.append(int(topics[3], 16))
            except ValueError:
                pass
    return ids


def registry_append(chain_name, contract, name, kind, tx_hash, block, held):
    """Append one entry to minted_portfolio_registry.json (same shape as
    minted_monitor_v2.py `add`, plus a `chain` field). Returns
    (key, entry) or (None, reason)."""
    key = slugify(name)
    reg = json.load(open(REGISTRY)) if os.path.exists(REGISTRY) else \
        {"version": 2, "projects": {}}
    projects = reg.setdefault("projects", {})
    if key in projects:
        return None, f"registry key '{key}' already exists; not overwriting"
    entry = {"kind": kind, "name": name, "contract": contract,
             "chain": chain_name, "held": held, "official_x": None,
             "official_site": None, "element_slug": None,
             "mint_tx": tx_hash, "mint_block": block,
             "minted_at": time.strftime("%Y-%m-%dT%H:%M:%S+07:00",
                                        time.localtime()),
             "alerts": {"liq_usd": 1000} if kind == "token"
             else {"floor_move_pct": 20}}
    projects[key] = entry
    tmp = REGISTRY + ".tmp"
    with open(tmp, "w") as f:
        json.dump(reg, f, indent=1)
    os.replace(tmp, REGISTRY)
    with open(PORTFOLIO_MD, "a") as f:
        f.write(f"\n## {name} ({kind}, {chain_name}) — ditambah "
                f"{entry['minted_at'][:10]} (via mint_nft.py)\n"
                f"- Kontrak: {contract}\n- Held: {json.dumps(held)}\n"
                f"- Mint tx: {tx_hash}\n")
    return key, entry


def do_execute(chain_name, preview_path, confirm, out=None):
    """Send the mint. Requires --confirm. Re-verifies everything fresh.
    Solana always refuses (see do_prepare_solana)."""
    chain = CHAINS[chain_name]
    if not chain["evm"]:
        print("REFUSED: Solana execute is not implemented in this tool: "
              "Candy Machine instruction construction (guard evaluation + "
              "PDA derivation + borsh layout) is not verified here, so no "
              "transaction will be built rather than a wrong one.",
              file=sys.stderr)
        return 2
    if not confirm:
        print("REFUSED: execute needs --confirm. This is deliberate: "
              "read the preview, then re-run with --confirm.", file=sys.stderr)
        return 2
    try:
        p = json.load(open(preview_path))
    except Exception as e:
        print(f"REFUSED: cannot read preview {preview_path}: {e}",
              file=sys.stderr)
        return 2
    if p.get("tool") != "mint_nft.py prepare" or p.get("status") != "ready":
        print("REFUSED: not a ready mint_nft.py preview file "
              "(Solana assessments have status 'assessment_only' and can "
              "never execute).", file=sys.stderr)
        return 2
    if p.get("chain") != chain_name:
        print(f"REFUSED: preview is for chain '{p.get('chain')}', not "
              f"'{chain_name}'. Re-run prepare with --chain {chain_name}.",
              file=sys.stderr)
        return 2
    age = int(time.time()) - p["created_at_epoch"]
    if age > PREVIEW_MAX_AGE_S:
        print(f"REFUSED: preview is {age // 60} min old (max "
              f"{PREVIEW_MAX_AGE_S // 60} min). Re-run prepare for fresh "
              "numbers.", file=sys.stderr)
        return 2
    if not baw_supports_chain(chain):
        print(f"REFUSED: baw cannot sign on {chain_name} "
              f"(binanceChainId {chain['baw']} is not in `baw wallet chains`). "
              "Prepare (read-only) still works; execution does not.",
              file=sys.stderr)
        return 2
    contract, name = p["contract"], p["name"]
    wallet = chain["wallet"]
    fn, value_wei = p["function"], p["value_wei"]
    print(f"[execute:{chain_name}] {name} {contract} — fresh re-verification ...",
          flush=True)
    wallet_checks_or_raise()
    sim = evm_call_raw(chain, contract, fn["calldata"], wallet, value_wei)
    ok, why = sim_passes(sim)
    if not ok:
        print(f"ABORT: fresh simulation failed: {why}", file=sys.stderr)
        return 2
    decoded, _impl = evm_decoded_selectors(chain, contract)
    decoded_names = {s.split("(")[0] for s in decoded.values()}
    quota = evm_quota_check(chain, contract, wallet, decoded_names, True)
    if value_wei == 0 and quota["used"]:
        print("ABORT: free quota is now used (state moved since prepare).",
              file=sys.stderr)
        return 2
    try:
        gas = evm_gas_estimate(chain, contract, fn["calldata"], value_wei, wallet)
    except RuntimeError as e:
        print(f"ABORT: fresh gas estimation failed: {e}", file=sys.stderr)
        return 2
    balance_wei, _ = evm_native_balance(chain, wallet)
    total_wei = value_wei + int(gas["gas"] * gas["gas_price_wei"] * GAS_MARGIN)
    if balance_wei < total_wei:
        print(f"ABORT: wallet {chain['currency']} {balance_wei / 10**18:.6f} "
              f"does not cover value + gas "
              f"({total_wei / 10**18:.6f} {chain['currency']}). Top up first.",
              file=sys.stderr)
        return 2
    supply_before, _ = read_total_supply(chain, contract)
    bal_before = quota["balance_of"].get("value")
    print(f"[execute:{chain_name}] baw contract-call preview ...", flush=True)
    try:
        prev = baw("contract-call", "preview",
                   "--binanceChainId", chain["baw"],
                   "--from", wallet, "--to", contract,
                   "--value", str(value_wei),
                   "--inputData", fn["calldata"])
    except RuntimeError as e:
        print(f"ABORT: baw preview failed: {e}", file=sys.stderr)
        return 2
    request_id = prev.get("requestId")
    simres = (prev.get("simulationResult") or {})
    if not request_id:
        print(f"ABORT: baw preview returned no requestId: {prev}",
              file=sys.stderr)
        return 2
    if simres.get("simulationErrorDetail"):
        print(f"ABORT: baw simulation error: "
              f"{simres.get('simulationErrorDetail')}", file=sys.stderr)
        return 2
    risks = (prev.get("risks") or {}).get("riskDetails") or []
    for r in risks:
        print(f"  baw risk [{r.get('riskType')}]: {r.get('title')} — "
              f"{r.get('description')}")
    if prev.get("requireConfirmation"):
        print("  NOTE: baw says this needs approval in the Binance App "
              "(requireConfirmation=true).")
    print(f"[execute:{chain_name}] preview ok (requestId "
          f"{request_id[:12]}...). Sending ...", flush=True)
    try:
        ex = baw("contract-call", "execute", "--requestId", request_id)
    except RuntimeError as e:
        print(f"ABORT: baw execute failed: {e}", file=sys.stderr)
        return 2
    tx_hash = ex.get("txHash")
    status = ex.get("status")
    order_id = ex.get("orderId")
    receipt_data = {"tool": "mint_nft.py execute", "preview": preview_path,
                    "chain": chain_name, "chain_id": chain["chain_id"],
                    "currency": chain["currency"],
                    "contract": contract, "name": name, "wallet": wallet,
                    "function": fn, "value_wei": value_wei,
                    "baw_order_id": order_id, "baw_request_id": request_id,
                    "baw_status": status, "tx_hash": tx_hash,
                    "executed_at_wib": now_wib()}
    if status == "PENDING_CONFIRMATION" or not tx_hash:
        receipt_data["status"] = "pending_app_confirmation"
        receipt_data["note"] = ("Approve the order in the Binance App, then "
                                "run: mint_nft.py verify --chain "
                                f"{chain_name} --tx <hash> --contract "
                                + contract + " (once the tx exists on-chain).")
        print("PENDING: approve in the Binance App. No tx hash yet; "
              f"orderId={order_id}.", flush=True)
    else:
        print(f"[execute:{chain_name}] broadcasted: {tx_hash} — waiting for "
              "receipt ...", flush=True)
        rcpt = wait_receipt(chain, tx_hash)
        if not rcpt:
            receipt_data["status"] = "broadcasted_no_receipt_yet"
            receipt_data["note"] = ("Receipt not seen within 5 min. Check "
                                    "with: mint_nft.py verify --chain "
                                    f"{chain_name} --tx {tx_hash} --contract "
                                    f"{contract}")
            print("WARNING: broadcasted but no receipt within 5 min.",
                  file=sys.stderr)
        else:
            rcpt_status = rcpt.get("status")
            block = int(rcpt.get("blockNumber", "0x0"), 16)
            token_ids = minted_token_ids(rcpt, wallet)
            supply_after, _ = read_total_supply(chain, contract)
            bal_after_res = evm_uint(
                chain, contract, "balanceOf(address)(uint256)", [wallet], wallet)
            bal_after = bal_after_res["value"]
            moved = ((supply_before is not None and supply_after is not None
                      and supply_after > supply_before)
                     or (bal_before is not None and bal_after is not None
                         and bal_after > bal_before))
            receipt_data.update({
                "status": "confirmed" if rcpt_status == "0x1" else "reverted",
                "block": block, "receipt_rpc": rcpt.get("_rpc"),
                "token_ids": token_ids,
                "supply_before": supply_before, "supply_after": supply_after,
                "balance_before": bal_before, "balance_after": bal_after,
                "balance_moved": moved})
            print(f"[execute:{chain_name}] receipt: status={rcpt_status} "
                  f"block={block} token_ids={token_ids} supply "
                  f"{supply_before} -> {supply_after} balance "
                  f"{bal_before} -> {bal_after}", flush=True)
            if rcpt_status != "0x1":
                print("ON-CHAIN REVERT: tx mined but status != 1.",
                      file=sys.stderr)
            elif token_ids or (bal_after is not None
                               and bal_before is not None
                               and bal_after > bal_before):
                kind = "nft"
                held = {"token_ids": token_ids} if token_ids else {}
                if not token_ids and bal_after is not None \
                        and bal_before is not None:
                    kind = "token"
                    held = {"amount": bal_after - bal_before, "unit": name}
                key, res = registry_append(chain_name, contract, name, kind,
                                           tx_hash, block, held)
                receipt_data["registry"] = ({"key": key, "entry": res}
                                            if key else {"error": res})
                print(f"[execute:{chain_name}] registry: {key or res}",
                      flush=True)
            if not moved:
                print("WARNING: receipt is success but neither totalSupply "
                      "nor balanceOf moved on 2-RPC re-read — verify "
                      "manually before trusting this mint.", file=sys.stderr)
    if not out:
        out = os.path.join(
            PREVIEW_DIR,
            f"receipt-{chain_name}-{slugify(name)}-"
            f"{datetime.now(WIB).strftime('%Y%m%d-%H%M%S')}.json")
    os.makedirs(PREVIEW_DIR, exist_ok=True)
    with open(out, "w") as f:
        json.dump(receipt_data, f, indent=1)
    print(f"receipt written to {out}")
    return 0 if receipt_data.get("status") == "confirmed" else 1


# ---------------------------------------------------------------- verify
def do_verify(chain_name, tx_hash, contract):
    chain = CHAINS[chain_name]
    if not chain["evm"]:
        if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{80,90}", tx_hash or ""):
            print("ERROR: --tx must be a base58 Solana transaction signature.")
            return 2
        try:
            tx = sol_rpc("getTransaction",
                         [tx_hash, {"encoding": "json",
                                   "maxSupportedTransactionVersion": 0}])
        except RuntimeError as e:
            print(f"Solana RPC error: {e}")
            return 1
        if not tx:
            print(f"no transaction {tx_hash} found (not confirmed yet?).")
            return 1
        meta = tx.get("meta") or {}
        print(f"tx {tx_hash}\n  slot: {tx.get('slot')}"
              f"\n  err: {meta.get('err')} (null = success)"
              f"\n  fee: {meta.get('fee')} lamports")
        return 0 if meta.get("err") is None else 1
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", tx_hash or ""):
        print("ERROR: --tx must be a full 0x transaction hash (64 hex chars).")
        return 2
    rcpt = wait_receipt(chain, tx_hash, timeout_s=60, interval_s=5)
    if not rcpt:
        print(f"no receipt for {tx_hash} on any RPC (not mined yet?).")
        return 1
    block = int(rcpt.get("blockNumber", "0x0"), 16)
    print(f"tx {tx_hash}\n  status: {rcpt.get('status')} (0x1 = success)"
          f"\n  block: {block} (via {rcpt.get('_rpc')})"
          f"\n  from: {rcpt.get('from')}  to: {rcpt.get('to')}"
          f"\n  gas used: {int(rcpt.get('gasUsed', '0x0'), 16)}")
    ids = minted_token_ids(rcpt, chain["wallet"])
    print(f"  minted token IDs to wallet: {ids}")
    supply, _ = read_total_supply(chain, contract)
    bal = evm_uint(chain, contract, "balanceOf(address)(uint256)",
                   [chain["wallet"]], chain["wallet"])
    print(f"  totalSupply now: {supply} | wallet balanceOf: {bal['value']}")
    return 0 if rcpt.get("status") == "0x1" else 1


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(
        description="Safe manual NFT minting, multi-chain (preview -> confirm -> send).")
    ap.add_argument("--chain", dest="top_chain", default=None,
                    choices=list(CHAINS),
                    help="chain to work on (default: bsc; also accepted after the subcommand)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("prepare", help="Read-only gate + simulation; writes a preview JSON (EVM) or an assessment (Solana). Sends nothing.")
    pp.add_argument("--chain", default=None, choices=list(CHAINS),
                    help="chain to work on (default: bsc)")
    pp.add_argument("--contract", help="EVM: full 0x contract address. Solana: base58 account address (e.g. a Candy Machine).")
    pp.add_argument("--name", default="project", help="label for the preview/registry")
    pp.add_argument("--count", type=int, default=1, help="mint(uint256) amount (EVM, default 1)")
    pp.add_argument("--function", help="explicit EVM function signature, e.g. 'claimFree()' (validated against decoded selectors)")
    pp.add_argument("--allow-paid", action="store_true", help="allow value > 0 (needs --value or on-chain PRICE)")
    pp.add_argument("--value", type=float, default=None, help="exact value in the chain's native currency for a paid mint (BNB on BSC, ETH on ETH/Base/Robinhood)")
    pp.add_argument("--value-bnb", type=float, default=None, help="deprecated alias for --value")
    pp.add_argument("--allow-unverified", action="store_true", help="proceed despite Sourcify unverified (loud warning; you must have verified the contract another way)")
    pp.add_argument("--out", help="preview JSON path (default: mint_previews/<chain>-<slug>-<timestamp>.json)")
    pp.add_argument("--batch", help="JSON file {\"lanes\": [{chain?, contract, name, count?, allow_paid?, value?, function?}]} — each lane prepared independently")

    pe = sub.add_parser("execute", help="Send a prepared mint. Requires --confirm; re-verifies fresh first. Solana always refuses.")
    pe.add_argument("--chain", default=None, choices=list(CHAINS),
                    help="chain to work on (default: bsc)")
    pe.add_argument("--preview", required=True, help="preview JSON from prepare")
    pe.add_argument("--confirm", action="store_true", help="REQUIRED: confirms you read the preview and want this exact transaction sent")
    pe.add_argument("--out", help="receipt JSON path")

    pv = sub.add_parser("verify", help="Read-only receipt + movement check for a tx hash.")
    pv.add_argument("--chain", default=None, choices=list(CHAINS),
                    help="chain to work on (default: bsc)")
    pv.add_argument("--tx", required=True, help="EVM: full 0x tx hash. Solana: base58 tx signature.")
    pv.add_argument("--contract", help="EVM contract address (for balance checks). Not needed on Solana.")

    args = ap.parse_args()
    chain_name = args.chain or args.top_chain or "bsc"
    if args.cmd == "prepare":
        if args.batch:
            try:
                lanes = json.load(open(args.batch)).get("lanes", [])
            except Exception as e:
                print(f"ERROR: cannot read batch file: {e}", file=sys.stderr)
                return 2
            ok_n = fail_n = 0
            for lane in lanes:
                lane_chain = lane.get("chain", chain_name)
                print(f"\n--- lane [{lane_chain}] {lane.get('name')} "
                      f"{lane.get('contract')} ---")
                try:
                    if not CHAINS[lane_chain]["evm"]:
                        _a, reason = do_prepare_solana(
                            lane["contract"], lane.get("name", "project"))
                        if reason:
                            print(f"REFUSED: {reason}")
                            fail_n += 1
                        else:
                            ok_n += 1
                        continue
                    p, reason = do_prepare_evm(
                        lane_chain, lane["contract"],
                        lane.get("name", "project"),
                        count=lane.get("count", 1),
                        function=lane.get("function"),
                        allow_paid=lane.get("allow_paid", False),
                        value=lane.get("value", lane.get("value_bnb")),
                        allow_unverified=lane.get("allow_unverified", False))
                except RuntimeError as e:
                    p, reason = None, str(e)
                if p:
                    print_preview(p)
                    ok_n += 1
                else:
                    print(f"REFUSED: {reason}")
                    fail_n += 1
            print(f"\nbatch done: {ok_n} ready, {fail_n} refused")
            return 0 if fail_n == 0 else 1
        if not args.contract:
            print("ERROR: prepare needs --contract or --batch.", file=sys.stderr)
            return 2
        if not CHAINS[chain_name]["evm"]:
            try:
                _a, reason = do_prepare_solana(args.contract, args.name)
            except RuntimeError as e:
                print(f"ERROR: {e}", file=sys.stderr)
                return 2
            if reason:
                print(f"REFUSED: {reason}", file=sys.stderr)
                return 1
            return 0
        try:
            p, reason = do_prepare_evm(
                chain_name, args.contract, args.name, count=args.count,
                function=args.function, allow_paid=args.allow_paid,
                value=(args.value if args.value is not None
                       else args.value_bnb),
                allow_unverified=args.allow_unverified, out=args.out)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        if not p:
            print(f"REFUSED: {reason}", file=sys.stderr)
            return 1
        print_preview(p)
        return 0
    if args.cmd == "execute":
        try:
            return do_execute(chain_name, args.preview, args.confirm,
                              out=args.out)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
    if args.cmd == "verify":
        if CHAINS[chain_name]["evm"] and not args.contract:
            print("ERROR: verify on an EVM chain needs --contract.",
                  file=sys.stderr)
            return 2
        try:
            return do_verify(chain_name, args.tx, args.contract)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
