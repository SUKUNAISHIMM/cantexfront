"""
Cantex Auto-Swap Worker (Python).

Uses the official cantex_sdk to repeatedly swap full account balances between
USDCx and cBTC on Cantex Mainnet. Stops auto-swap on each account once 50
swaps have been performed (25 in each direction).

Reads accounts from the same Postgres DB used by the api-server (drizzle
schema: accounts, swap_history).
"""

from future import annotations

import asyncio
import logging
import os
import sys
import traceback
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row

from cantex_sdk import (
CantexAPIError,
CantexSDK,
CantexTimeoutError,
IntentTradingKeySigner,
OperatorKeySigner,
)

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
sys.exit("DATABASE_URL is required")

BASE_URL = os.environ.get("CANTEX_BASE_URL", "https://api.cantex.io")
POLL_INTERVAL_SEC = int(os.environ.get("AUTO_SWAP_POLL_SEC", "3"))
SWAPS_PER_DIRECTION = int(os.environ.get("AUTO_SWAP_PER_DIRECTION", "13"))
SWAP_TIMEOUT_SEC = float(os.environ.get("AUTO_SWAP_TIMEOUT", "90"))
MIN_SWEEP_AMOUNT = Decimal(os.environ.get("AUTO_SWAP_MIN", "0"))

CBTC_SYMBOL = "cBTC"
USDCX_SYMBOL = "USDCx"
CC_SYMBOL = "CC"

Cantex Mainnet returns these symbols in upper-case (e.g. "CBTC", "USDCX"),

while older docs / pool definitions sometimes use mixed case. Match

case-insensitively so we don't miss the right token.

CBTC_ALIASES = {"cbtc"}
USDCX_ALIASES = {"usdcx", "usdc", "usdcxmain"}
CC_ALIASES = {"cc", "amulet", "cantoncoin", "canton coin"}
DIR_CBTC_TO_USDCX = "cbtc_to_usdcx"
DIR_USDCX_TO_CBTC = "usdcx_to_cbtc"

logging.basicConfig(
level=logging.INFO,
format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
log = logging.getLogger("auto_swap")
logging.getLogger("cantex_sdk").setLevel(logging.WARNING)

── Database helpers ───────────────────────────────────────────────────

def db_connect() -> psycopg.Connection:
return psycopg.connect(DATABASE_URL, autocommit=True)

def fetch_active_accounts() -> list[dict[str, Any]]:
with db_connect() as conn, conn.cursor(row_factory=dict_row) as cur:
cur.execute(
"""
SELECT id, name, operator_key, trading_key, swap_count_reset_at
FROM accounts
WHERE is_active = TRUE
AND auto_swap_enabled = TRUE
"""
)
return list(cur.fetchall())

def fetch_global_gas_threshold() -> "Decimal | None":
with db_connect() as conn, conn.cursor() as cur:
cur.execute(
"SELECT gas_threshold FROM global_settings WHERE id = 1"
)
row = cur.fetchone()
if row and row[0] is not None:
return Decimal(str(row[0]))
return None

def count_swaps(account_id: int, since: "Any | None" = None) -> dict[str, int]:
counts = {DIR_CBTC_TO_USDCX: 0, DIR_USDCX_TO_CBTC: 0}
with db_connect() as conn, conn.cursor() as cur:
if since is not None:
cur.execute(
"""
SELECT direction, COUNT()
FROM swap_history
WHERE account_id = %s
AND status = 'success'
AND executed_at > %s
GROUP BY direction
""",
(account_id, since),
)
else:
cur.execute(
"""
SELECT direction, COUNT()
FROM swap_history
WHERE account_id = %s
AND status = 'success'
GROUP BY direction
""",
(account_id,),
)
for direction, n in cur.fetchall():
if direction in counts:
counts[direction] = int(n)
return counts

def disable_auto_swap(account_id: int) -> None:
with db_connect() as conn, conn.cursor() as cur:
cur.execute(
"UPDATE accounts SET auto_swap_enabled = FALSE WHERE id = %s",
(account_id,),
)

def insert_swap(
account_id: int,
name: str,
direction: str,
in_amt: Decimal,
in_sym: str,
out_amt: Decimal,
out_sym: str,
price: Decimal | None,
status: str,
network_fee: Decimal | None = None,
) -> None:
with db_connect() as conn, conn.cursor() as cur:
cur.execute(
"""
INSERT INTO swap_history
(account_id, account_name, direction,
input_amount, input_symbol,
output_amount, output_symbol,
price, status, network_fee)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
""",
(
account_id,
name,
direction,
str(in_amt),
in_sym,
str(out_amt),
out_sym,
str(price) if price is not None else None,
status,
str(network_fee) if network_fee is not None else None,
),
)

── Pool / token helpers ───────────────────────────────────────────────

def _canonical_symbol(raw: str | None) -> str | None:
"""Map any case/spelling variant to our canonical CBTC/USDCX/CC symbols."""
if raw is None:
return None
key = str(raw).strip().lower()
if key in CBTC_ALIASES:
return CBTC_SYMBOL
if key in USDCX_ALIASES:
return USDCX_SYMBOL
if key in CC_ALIASES:
return CC_SYMBOL
return None

def build_token_map(account_info) -> dict[str, dict[str, Any]]:
"""Map canonical symbol -> {instrument, unlocked_amount (Decimal), raw_symbol}.

The Cantex API uses different casings for the same token across endpoints  
(e.g. ``CBTC`` vs ``cBTC``). We normalize to ``cBTC`` / ``USDCx`` / ``CC``  
so the rest of the worker can use stable keys.  
"""  
out: dict[str, dict[str, Any]] = {}  
for tok in account_info.tokens:  
    canonical = _canonical_symbol(tok.instrument_symbol)  
    if canonical is None:  
        continue  
    # If the same canonical symbol appears twice (rare), keep the entry  
    # with a non-zero unlocked balance so we have something to swap.  
    amt = Decimal(str(tok.unlocked_amount))  
    existing = out.get(canonical)  
    if existing is None or (existing["unlocked_amount"] == 0 and amt > 0):  
        out[canonical] = {  
            "instrument": tok.instrument,  
            "unlocked_amount": amt,  
            "raw_symbol": tok.instrument_symbol,  
        }  
return out

def choose_direction(counts: dict[str, int], token_map: dict[str, dict[str, Any]]):
"""Decide which direction to swap.

Strategy:  
  - If both directions are at the per-direction limit -> None (caller stops).  
  - Prefer the side that has an actual non-zero balance.  
  - If both have balance, prefer whichever direction has fewer swaps so far  
    (keeps it balanced toward 25/25).  
  - If only one side has balance, use that direction (still respecting limit).  
"""  
cbtc_left = SWAPS_PER_DIRECTION - counts[DIR_CBTC_TO_USDCX]  
usdcx_left = SWAPS_PER_DIRECTION - counts[DIR_USDCX_TO_CBTC]  

if cbtc_left <= 0 and usdcx_left <= 0:  
    return None  

cbtc_bal = token_map.get(CBTC_SYMBOL, {}).get("unlocked_amount", Decimal(0))  
usdcx_bal = token_map.get(USDCX_SYMBOL, {}).get("unlocked_amount", Decimal(0))  

cbtc_ready = cbtc_left > 0 and cbtc_bal > MIN_SWEEP_AMOUNT  
usdcx_ready = usdcx_left > 0 and usdcx_bal > MIN_SWEEP_AMOUNT  

if cbtc_ready and usdcx_ready:  
    # Prefer whichever direction is further from its quota  
    return DIR_CBTC_TO_USDCX if cbtc_left >= usdcx_left else DIR_USDCX_TO_CBTC  
if cbtc_ready:  
    return DIR_CBTC_TO_USDCX  
if usdcx_ready:  
    return DIR_USDCX_TO_CBTC  
return None

── Per-account processing ─────────────────────────────────────────────

IDs of accounts currently mid-swap. Polling fires every few seconds, but a

real swap takes ~6-12s to confirm — without this guard a second tick would

fire a duplicate swap on the same account before the first finishes.

_in_flight_accounts: set[Any] = set()

async def process_account(acc: dict[str, Any]) -> None:
account_id = acc["id"]
name = acc["name"]
alog = logging.getLogger(f"auto_swap.{name}")

# Drop this tick's attempt if a previous tick is still processing this  
# account. The next tick will pick it up again in a few seconds.  
if account_id in _in_flight_accounts:  
    alog.debug("Previous swap still in flight; skipping this tick")  
    return  
_in_flight_accounts.add(account_id)  
try:  
    await _process_account_inner(acc, alog)  
finally:  
    _in_flight_accounts.discard(account_id)

async def _process_account_inner(acc: dict[str, Any], alog: logging.Logger) -> None:
account_id = acc["id"]
name = acc["name"]

counts = count_swaps(account_id, since=acc.get("swap_count_reset_at"))  
total = counts[DIR_CBTC_TO_USDCX] + counts[DIR_USDCX_TO_CBTC]  
alog.debug(  
    "Swap counts so far -> cbtc->usdcx=%d, usdcx->cbtc=%d (total=%d)",  
    counts[DIR_CBTC_TO_USDCX],  
    counts[DIR_USDCX_TO_CBTC],  
    total,  
)  

if (  
    counts[DIR_CBTC_TO_USDCX] >= SWAPS_PER_DIRECTION  
    and counts[DIR_USDCX_TO_CBTC] >= SWAPS_PER_DIRECTION  
):  
    alog.info("Reached 26 swaps (13 each). Disabling auto-swap.")  
    disable_auto_swap(account_id)  
    return  

operator = OperatorKeySigner.from_hex(acc["operator_key"])  
intent = IntentTradingKeySigner.from_hex(acc["trading_key"])  

async with CantexSDK(  
    operator,  
    intent,  
    base_url=BASE_URL,  
    api_key_path=None,  # don't persist api key to disk  
) as sdk:  
    await sdk.authenticate()  

    info = await sdk.get_account_info()  

    token_map = build_token_map(info)  
    missing = [s for s in (CBTC_SYMBOL, USDCX_SYMBOL) if s not in token_map]  
    if missing:  
        alog.warning("Account is missing token registration: %s; skipping", missing)  
        return  

    direction = choose_direction(counts, token_map)  
    if direction is None:  
        alog.info("No swappable balance (or quota reached); skipping")  
        return  

    if direction == DIR_CBTC_TO_USDCX:  
        sell_sym, buy_sym = CBTC_SYMBOL, USDCX_SYMBOL  
    else:  
        sell_sym, buy_sym = USDCX_SYMBOL, CBTC_SYMBOL  

    sell_amount = token_map[sell_sym]["unlocked_amount"]  
    if sell_amount <= MIN_SWEEP_AMOUNT:  
        alog.info("No %s balance to sweep (%s)", sell_sym, sell_amount)  
        return  

    sell_inst = token_map[sell_sym]["instrument"]  
    buy_inst = token_map[buy_sym]["instrument"]  

    # ── Gas-fee threshold check (global) ───────────────────────────  
    # Uses the single shared threshold from `global_settings`. A null  
    # threshold means "no limit — always swap immediately".  
    gas_threshold = fetch_global_gas_threshold()  
    # Always fetch a quote so we have the live gas fee to record later.  
    # If a threshold is set, also check it before proceeding.  
    quote_network_fee: Decimal | None = None  
    quote_price: Decimal | None = None  
    try:  
        quote = await sdk.get_swap_quote(sell_amount, sell_inst, buy_inst)  
        quote_network_fee = Decimal(str(quote.fees.network_fee.amount))  
        quote_price = Decimal(str(quote.prices.trade)) if quote.prices.trade else None  
    except Exception as exc:  
        alog.warning(  
            "Could not fetch quote to check gas threshold (%s); skipping this tick",  
            (str(exc) or repr(exc))[:120],  
        )  
        return  

    if gas_threshold is not None:  
        alog.info(  
            "Gas check: network fee = %s CC, threshold = %s CC",  
            quote_network_fee, gas_threshold,  
        )  
        if quote_network_fee is not None and quote_network_fee > gas_threshold:  
            alog.info(  
                "Skipping swap: gas fee %s CC > threshold %s CC",  
                quote_network_fee, gas_threshold,  
            )  
            return  
        alog.info(  
            "Gas %s CC <= threshold %s CC -> firing swap",  
            quote_network_fee, gas_threshold,  
        )  
    else:  
        alog.info("Gas fee = %s CC (no threshold set — firing swap)", quote_network_fee)  

    # Cantex's /v1/intent/build/pool/swap endpoint takes only a sell/buy  
    # instrument pair — the backend handles routing across one or more  
    # pools internally (e.g. cBTC -> CC -> USDCx when no direct pool  
    # exists). So we issue a single SDK call here and Cantex confirms it  
    # as one logical swap; how many on-chain hops it uses is up to them.  
    alog.info(  
        "Swapping ALL %s %s -> %s (single intent; Cantex routes hops if needed)",  
        sell_amount, sell_sym, buy_sym,  
    )  

    try:  
        confirmed = await sdk.swap_and_confirm(  
            sell_amount=sell_amount,  
            sell_instrument=sell_inst,  
            buy_instrument=buy_inst,  
            timeout=SWAP_TIMEOUT_SEC,  
        )  
        final_output = Decimal(str(confirmed.output_amount))  
        # confirmed.price comes from the WebSocket ticker and is often 0  
        # for cbtc_to_usdcx (missing ticker data). Use quote.prices.trade  
        # (fetched just before the swap from the AMM pool) as the  
        # authoritative price — fall back to confirmed only if it's > 0.  
        confirmed_price = Decimal(str(confirmed.price)) if getattr(confirmed, "price", None) is not None else Decimal(0)  
        price = confirmed_price if confirmed_price > 0 else quote_price  
        alog.info(  
            "Confirmed: %s %s -> %s %s (price=%s, quote_price=%s)",  
            confirmed.input_amount, sell_sym, final_output, buy_sym, price, quote_price,  
        )  

        insert_swap(  
            account_id, name, direction,  
            Decimal(str(confirmed.input_amount)), sell_sym,  
            final_output, buy_sym,  
            price, "success",  
            network_fee=quote_network_fee,  
        )  

        # Re-count after success; if both quotas now satisfied, disable.  
        new_counts = count_swaps(account_id)  
        if (  
            new_counts[DIR_CBTC_TO_USDCX] >= SWAPS_PER_DIRECTION  
            and new_counts[DIR_USDCX_TO_CBTC] >= SWAPS_PER_DIRECTION  
        ):  
            alog.info("50 swaps complete (25 each). Disabling auto-swap.")  
            disable_auto_swap(account_id)  

    except CantexTimeoutError:  
        # Swap was submitted to Cantex but confirmation timed out.  
        # The transaction may have settled on-chain — we cannot treat  
        # it as a confirmed failure. Log only; do not write a failed row.  
        alog.warning(  
            "Swap timed out waiting for Cantex confirmation — "  
            "outcome unknown; not recording as failed"  
        )  
    except CantexAPIError as exc:  
        # Cantex explicitly rejected the swap (non-2xx API response).  
        # This is a confirmed failure from the exchange.  
        msg = (str(exc) or repr(exc))[:120]  
        alog.error("Cantex rejected swap: %s", msg)  
        insert_swap(  
            account_id, name, direction,  
            sell_amount, sell_sym, Decimal(0), buy_sym,  
            None, f"failed: {msg}",  
            network_fee=quote_network_fee,  
        )  
    except Exception as exc:  # pragma: no cover  
        # Local error (network blip, SDK bug, etc.) — Cantex never  
        # confirmed a failure, so do not write a failed history row.  
        alog.error(  
            "Unexpected error during swap (not recorded as failed): %s\n%s",  
            (str(exc) or repr(exc))[:120],  
            traceback.format_exc(),  
        )

── Main loop ──────────────────────────────────────────────────────────

async def tick(tick_no: int) -> None:
try:
accounts = fetch_active_accounts()
except Exception as exc:
log.error("Failed to load accounts: %s", exc)
return

# At a 3s poll interval, INFO-logging every tick is too noisy. Only  
# surface ticks that actually have something to do.  
if not accounts:  
    log.debug("Tick #%d -> 0 active auto-swap account(s)", tick_no)  
    return  
log.debug("Tick #%d -> %d active auto-swap account(s)", tick_no, len(accounts))  

# Run accounts concurrently; failures in one don't block others.  
results = await asyncio.gather(  
    *(process_account(a) for a in accounts),  
    return_exceptions=True,  
)  
for acc, res in zip(accounts, results):  
    if isinstance(res, Exception):  
        log.error(  
            "Account %s (%s) crashed: %s",  
            acc["id"],  
            acc["name"],  
            res,  
        )

async def main() -> None:
log.info(
"Starting Cantex auto-swap worker (base=%s, poll=%ds, per-direction=%d)",
BASE_URL,
POLL_INTERVAL_SEC,
SWAPS_PER_DIRECTION,
)
tick_no = 0
while True:
tick_no += 1
try:
await tick(tick_no)
except Exception as exc:  # pragma: no cover
log.error("Tick crashed: %s\n%s", exc, traceback.format_exc())
await asyncio.sleep(POLL_INTERVAL_SEC)

if name == "main":
try:
asyncio.run(main())
except KeyboardInterrupt:
log.info("Auto-swap worker stopped")