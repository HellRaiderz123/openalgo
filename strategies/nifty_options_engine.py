#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
nifty_options_engine.py
-----------------------
Main runner — wires all engine layers together.
This is the file OpenAlgo Python host executes.

Architecture:
    MarketDataAdapter → Normalizer → DecisionEngine → Builder → StateManager → Executor

Config via OpenAlgo UI key icon (environment variables):
    LOTS                 = 1        number of lots
    STOP_LOSS_INR        = 3000     stop loss per trade in Rs
    TARGET_INR           = 2000     profit target per trade in Rs
    DAILY_LOSS_LIMIT_INR = 5000     max daily loss in Rs
    SQUARE_OFF_HOUR      = 15       square off hour IST
    SQUARE_OFF_MIN       = 10       square off minute IST
    PAPER_MODE           = true     true=paper, false=live
"""

import os
import sys
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Ensure repo root is on sys.path so strategies.engine.* is importable
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT  = None
for _candidate in [
    os.path.dirname(_SCRIPT_DIR),
    os.path.dirname(os.path.dirname(_SCRIPT_DIR)),
    _SCRIPT_DIR,
    r"C:\Users\TarunKumarParashar\OneDrive - Esyasoft Technologies Pvt. Ltd\Desktop\openalgo",
]:
    if os.path.isdir(os.path.join(_candidate, "strategies")):
        _REPO_ROOT = _candidate
        if _candidate not in sys.path:
            sys.path.insert(0, _candidate)
        break
if _REPO_ROOT is None:
    raise RuntimeError("Cannot locate repo root — strategies/ folder not found")

from openalgo import api
from strategies.engine.adapter    import MarketDataAdapter
from strategies.engine.normalizer import normalize
from strategies.engine.decision   import decide
from strategies.engine.builder    import build
from strategies.engine.state      import PositionState
from strategies.engine.executor   import Executor

# ── config ────────────────────────────────────────────────────────────────────
LOTS                 = int(os.getenv("LOTS",                 "1"))
STOP_LOSS_INR        = int(os.getenv("STOP_LOSS_INR",        "3000"))
TARGET_INR           = int(os.getenv("TARGET_INR",           "2000"))
DAILY_LOSS_LIMIT_INR = int(os.getenv("DAILY_LOSS_LIMIT_INR", "5000"))
SQUARE_OFF_HOUR      = int(os.getenv("SQUARE_OFF_HOUR",      "15"))
SQUARE_OFF_MIN       = int(os.getenv("SQUARE_OFF_MIN",       "10"))
PAPER_MODE           = os.getenv("PAPER_MODE", "true").lower() != "false"
ENTRY_START_H        = int(os.getenv("ENTRY_START_HOUR", "9"))
ENTRY_START_M        = int(os.getenv("ENTRY_START_MIN",  "35"))  # wait for OI to stabilise post-open
ENTRY_END_H          = int(os.getenv("ENTRY_END_HOUR",   "13"))
ENTRY_END_M          = int(os.getenv("ENTRY_END_MIN",    "0"))
PRODUCT              = "NRML"
STRATEGY_NAME        = "NIFTY Options Engine"
SLEEP_SECONDS        = 60   # polling interval when not in trade
SLEEP_IN_TRADE       = 15   # faster polling when in trade (for SL/target)
IST                  = timedelta(hours=5, minutes=30)

# ── OpenAlgo client ───────────────────────────────────────────────────────────
api_key = os.getenv("OPENALGO_API_KEY", "")
host    = os.getenv("HOST_SERVER",      "http://127.0.0.1:5000")
ws_url  = os.getenv("WEBSOCKET_URL",    "ws://127.0.0.1:8765")
client  = api(api_key=api_key, host=host, ws_url=ws_url)

# ── helpers ───────────────────────────────────────────────────────────────────
_DB_PATH = os.path.join(_REPO_ROOT, "db", "openalgo.db")


def ist_now():
    return datetime.now(timezone.utc) + IST


def hm(dt):
    return (dt.hour, dt.minute)


def log(msg):
    print(f"[{ist_now().strftime('%H:%M:%S')}] {msg}", flush=True)


def get_expiry_and_lotsize():
    """Fetch nearest expiry from DB, sorted by actual date (not alphabetically)."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        cur  = conn.cursor()
        cur.execute(
            "SELECT DISTINCT expiry, lotsize FROM symtoken "
            "WHERE name=? AND instrumenttype IN (?,?)",
            ("NIFTY", "CE", "PE"),
        )
        rows = [(r[0], r[1]) for r in cur.fetchall() if r[0]]
        conn.close()
        today = ist_now().date()
        # Parse all expiries and sort by actual date
        valid = []
        for raw, lotsize in rows:
            for fmt in ("%d-%b-%y", "%d-%b-%Y"):
                try:
                    exp_date = datetime.strptime(raw, fmt).date()
                    if exp_date >= today:
                        valid.append((exp_date, int(lotsize or 65)))
                    break
                except ValueError:
                    continue
        if valid:
            valid.sort(key=lambda x: x[0])  # Sort by date ascending
            nearest = valid[0]
            return nearest[0].strftime("%d%b%y").upper(), nearest[1]
        return None, 65
    except Exception as e:
        log(f"ERROR getting expiry: {e}")
        return None, 65


def _calc_net_premium(chain: dict, spec) -> Optional[float]:
    """
    Calculate NET spread premium (SELL legs - BUY legs).
    Returns None if any matched leg has a zero/missing LTP — caller must skip MTM update.
    """
    sell_strikes = {(leg.strike, leg.option_type) for leg in spec.legs if leg.action == "SELL"}
    buy_strikes  = {(leg.strike, leg.option_type) for leg in spec.legs if leg.action == "BUY"}
    found_sell   = {k: None for k in sell_strikes}
    found_buy    = {k: None for k in buy_strikes}
    for row in chain.get("chain", []):
        strike = int(float(row.get("strike") or 0))
        for side, otype in (("ce", "CE"), ("pe", "PE")):
            ltp = float((row.get(side) or {}).get("ltp") or 0)
            key = (strike, otype)
            if key in found_sell:
                found_sell[key] = ltp
            if key in found_buy:
                found_buy[key] = ltp
    # Reject if any leg is missing or zero
    for key, ltp in {**found_sell, **found_buy}.items():
        if ltp is None or ltp == 0.0:
            return None
    return sum(found_sell.values()) - sum(found_buy.values())


# ── entry window ──────────────────────────────────────────────────────────────
def in_entry_window():
    now = ist_now()
    # Skip Monday morning only (gap risk at open); afternoon is fine
    if now.weekday() == 0 and hm(now) < (11, 0):
        return False
    return (ENTRY_START_H, ENTRY_START_M) <= hm(now) <= (ENTRY_END_H, ENTRY_END_M)


# ── main loop ─────────────────────────────────────────────────────────────────
def run():
    adapter  = MarketDataAdapter(client)
    state    = PositionState()
    executor = Executor(client, STRATEGY_NAME, PRODUCT, paper=PAPER_MODE)

    expiry   = None
    qty      = LOTS * 65
    spec     = None

    mode_str = "PAPER" if PAPER_MODE else "LIVE"
    log(f"=== {STRATEGY_NAME} started [{mode_str}] ===")
    log(f"    Stop=Rs.{STOP_LOSS_INR}  Target=Rs.{TARGET_INR}  DailyLimit=Rs.{DAILY_LOSS_LIMIT_INR}")

    while True:
        try:
            now   = ist_now()
            today = now.date()

            # ── new day reset ─────────────────────────────────────────────────
            if state.session_date != today:
                state.reset_for_new_day(today)
                expiry = None
                spec   = None
                log("--- New session: state reset ---")

            # ── market hours ──────────────────────────────────────────────────
            if now.weekday() >= 5 or not ((9, 15) <= hm(now) <= (15, 30)):
                log("Market closed — waiting")
                time.sleep(60)
                continue

            # ── resolve expiry once per day ───────────────────────────────────
            if expiry is None:
                expiry, lot_size = get_expiry_and_lotsize()
                qty = LOTS * lot_size
                if expiry:
                    log(f"Expiry={expiry}  LotSize={lot_size}  Qty={qty}")
                else:
                    log("ERROR: Could not detect expiry")
                    time.sleep(SLEEP_SECONDS)
                    continue

            # ── daily loss limit ──────────────────────────────────────────────
            if not state.squared_off and state.daily_limit_hit(DAILY_LOSS_LIMIT_INR):
                log(f"Daily loss limit hit Rs.{state.daily_pnl} — squaring off")
                if state.in_trade:
                    executor.close_all(STRATEGY_NAME, log)
                state.squared_off = True

            if state.squared_off:
                time.sleep(SLEEP_SECONDS)
                continue

            # ── square off time ───────────────────────────────────────────────
            if state.in_trade and hm(now) >= (SQUARE_OFF_HOUR, SQUARE_OFF_MIN):
                log("Square-off time reached")
                executor.close_all(STRATEGY_NAME, log)
                state.exit()
                time.sleep(SLEEP_SECONDS)
                continue

            # ── entry ─────────────────────────────────────────────────────────
            if not state.in_trade and in_entry_window():

                # 1. fetch market data
                vix, vix_prev = adapter.get_vix_data()
                spot     = adapter.get_nifty_spot()
                chain, pcr = adapter.get_option_chain_with_pcr(expiry)
                log(f"PCR(full)={pcr}" if pcr else "PCR(full)=None — will use chain-computed fallback")

                # 2. normalize + validate
                snapshot, reason = normalize(vix, spot, chain, pcr, vix_prev)
                if snapshot is None:
                    log(f"SKIP: {reason}")
                    time.sleep(SLEEP_SECONDS)
                    continue

                log(f"Snapshot | VIX={snapshot.vix} ({snapshot.vix_change_pct:+.1f}%)  PCR={snapshot.pcr}"
                    f"  Premium=Rs.{snapshot.straddle_premium}  Spot={snapshot.spot}")

                # 3. decide strategy
                decision = decide(snapshot)
                log(f"Decision | {decision.strategy} [{decision.iv_regime}/{decision.bias}]"
                    f" — {decision.reason}")

                if decision.strategy == "NO_TRADE":
                    time.sleep(SLEEP_SECONDS)
                    continue

                # 4. build + risk gate
                spec = build(decision, snapshot, qty, STOP_LOSS_INR)
                if not spec.is_valid:
                    log(f"RISK GATE REJECTED: {spec.reject_reason}")
                    time.sleep(SLEEP_SECONDS)
                    continue

                log(f"Trade | {spec.strategy} | MaxLoss=Rs.{spec.max_loss_inr}"
                    f"  MaxProfit=Rs.{spec.max_profit_inr}  RR={spec.risk_reward}")

                # 5. execute + capture NET entry premium (SELL - BUY legs)
                ok = executor.execute(spec, expiry, log)
                if ok:
                    chain_at_entry = adapter.get_option_chain(expiry)
                    entry_prem = _calc_net_premium(chain_at_entry, spec) if chain_at_entry else None
                    if entry_prem is None:
                        # Fallback: compute net credit from snapshot chain rows directly
                        entry_prem = _calc_net_premium({"chain": snapshot._chain_rows or []}, spec)
                    if entry_prem is None or entry_prem == 0.0:
                        log("WARN: entry premium is zero/unavailable — skipping state.enter to avoid bad MTM baseline")
                        time.sleep(SLEEP_SECONDS)
                        continue
                    state.enter(spec, entry_prem)
                    log(f"ENTERED {spec.strategy} | NetPremium=Rs.{entry_prem}")

                time.sleep(SLEEP_SECONDS)
                continue

            # ── MTM monitoring ────────────────────────────────────────────────
            if state.in_trade and spec:
                vix, vix_prev = adapter.get_vix_data()
                spot     = adapter.get_nifty_spot()
                chain, pcr = adapter.get_option_chain_with_pcr(expiry)

                if not chain:
                    log("WARNING: Could not fetch option chain for MTM")
                    time.sleep(SLEEP_IN_TRADE)
                    continue

                # Calculate NET premium (SELL - BUY legs)
                current = _calc_net_premium(chain, spec)
                if current is None:
                    log("MTM SKIP: one or more leg LTPs are zero — stale quote, retrying")
                    time.sleep(SLEEP_IN_TRADE)
                    continue
                state.update_mtm(current, qty)
                log(f"MTM=Rs.{state.mtm_pnl}  Entry={state.entry_premium}  Now={current}")

                # ── PRIORITY 1: Check SL/Target BEFORE regime shift ───────────
                if state.should_stop(STOP_LOSS_INR):
                    log(f"STOP HIT Rs.{state.mtm_pnl} — exiting")
                    executor.close_all(STRATEGY_NAME, log)
                    state.exit()
                    time.sleep(SLEEP_SECONDS)  # Prevent instant re-entry
                    continue

                if state.should_target(TARGET_INR):
                    log(f"TARGET HIT Rs.{state.mtm_pnl} — booking profit")
                    executor.close_all(STRATEGY_NAME, log)
                    state.exit()
                    time.sleep(SLEEP_SECONDS)  # Prevent instant re-entry
                    continue

                # ── PRIORITY 2: Strategy change exit ──────────────────────────
                new_snap, _ = normalize(vix, spot, chain, pcr, vix_prev)
                if new_snap:
                    new_decision = decide(new_snap)
                    if new_decision.strategy != spec.strategy:
                        if new_decision.strategy == "NO_TRADE":
                            log(f"REGIME EXIT: {spec.strategy} → NO_TRADE | {new_decision.reason}")
                        else:
                            log(f"STRATEGY SHIFT: {spec.strategy} → {new_decision.strategy} | VIX={new_snap.vix} PCR={new_snap.pcr}")
                        executor.close_all(STRATEGY_NAME, log)
                        state.exit()
                        time.sleep(SLEEP_SECONDS)  # Prevent instant re-entry
                        continue

                time.sleep(SLEEP_IN_TRADE)  # Faster polling when in trade
                continue

        except Exception as e:
            log(f"ERROR: {e}")

        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    run()
