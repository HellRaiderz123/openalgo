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
ENTRY_START_M        = int(os.getenv("ENTRY_START_MIN",  "20"))
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


def _calc_net_premium(chain: dict, spec) -> float:
    """
    Calculate NET spread premium (SELL legs - BUY legs).
    This correctly tracks P&L for spreads/condors where hedge legs offset losses.
    """
    sell_strikes = {(leg.strike, leg.option_type) for leg in spec.legs if leg.action == "SELL"}
    buy_strikes  = {(leg.strike, leg.option_type) for leg in spec.legs if leg.action == "BUY"}
    sell_total = 0.0
    buy_total  = 0.0
    for row in chain.get("chain", []):
        strike = int(float(row.get("strike") or 0))
        for side, otype in (("ce", "CE"), ("pe", "PE")):
            ltp = float((row.get(side) or {}).get("ltp") or 0)
            if (strike, otype) in sell_strikes:
                sell_total += ltp
            if (strike, otype) in buy_strikes:
                buy_total += ltp
    return sell_total - buy_total  # Net premium (positive = credit)


# ── entry window ──────────────────────────────────────────────────────────────
def in_entry_window():
    now = ist_now()
    if now.weekday() == 0:  # Skip Monday — overnight gap risk
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
                executor.close_all(STRATEGY_NAME, log)
                state.exit()

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
                vix   = adapter.get_vix()
                spot  = adapter.get_nifty_spot()
                chain = adapter.get_option_chain(expiry)

                # Debug: Print raw chain data for ATM strike
                if chain:
                    rows = chain.get("chain", [])
                    if rows:
                        atm_approx = int(round((spot or 23000) / 50) * 50)
                        for row in rows:
                            strike = int(float(row.get("strike") or 0))
                            if abs(strike - atm_approx) <= 50:
                                ce = row.get("ce") or {}
                                pe = row.get("pe") or {}
                                log(f"DEBUG ATM Row | Strike={strike} CE={ce} PE={pe}")
                                break

                # 2. normalize + validate
                snapshot, reason = normalize(vix, spot, chain)
                if snapshot is None:
                    log(f"SKIP: {reason}")
                    time.sleep(SLEEP_SECONDS)
                    continue

                log(f"Snapshot | VIX={snapshot.vix}  PCR={snapshot.pcr}"
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
                    entry_prem = _calc_net_premium(chain_at_entry, spec) if chain_at_entry else snapshot.straddle_premium
                    state.enter(spec, entry_prem)
                    log(f"ENTERED {spec.strategy} | NetPremium=Rs.{entry_prem}")

                time.sleep(SLEEP_SECONDS)
                continue

            # ── MTM monitoring ────────────────────────────────────────────────
            if state.in_trade and spec:
                # Single API call for chain — reuse for MTM + regime check
                vix   = adapter.get_vix()
                spot  = adapter.get_nifty_spot()
                chain = adapter.get_option_chain(expiry)
                
                if not chain:
                    log("WARNING: Could not fetch option chain for MTM")
                    time.sleep(SLEEP_IN_TRADE)
                    continue

                # Calculate NET premium (SELL - BUY legs)
                current = _calc_net_premium(chain, spec)
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
                new_snap, _ = normalize(vix, spot, chain)
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
