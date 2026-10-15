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
PRODUCT              = "NRML"
STRATEGY_NAME        = "NIFTY Options Engine"
SLEEP_SECONDS        = 60
IST                  = timedelta(hours=5, minutes=30)

# ── OpenAlgo client ───────────────────────────────────────────────────────────
api_key = os.getenv("OPENALGO_API_KEY", "")
host    = os.getenv("HOST_SERVER",      "http://127.0.0.1:5000")
ws_url  = os.getenv("WEBSOCKET_URL",    "ws://127.0.0.1:8765")
client  = api(api_key=api_key, host=host, ws_url=ws_url)

# ── helpers ───────────────────────────────────────────────────────────────────
_HERE    = os.path.dirname(os.path.abspath(__file__))
_DB_PATH = os.path.join(_HERE, "..", "db", "openalgo.db")


def ist_now():
    return datetime.now(timezone.utc) + IST


def hm(dt):
    return (dt.hour, dt.minute)


def log(msg):
    print(f"[{ist_now().strftime('%H:%M:%S')}] {msg}", flush=True)


def get_expiry_and_lotsize():
    try:
        conn = sqlite3.connect(_DB_PATH)
        cur  = conn.cursor()
        cur.execute(
            "SELECT DISTINCT expiry, lotsize FROM symtoken "
            "WHERE name=? AND instrumenttype IN (?,?) ORDER BY expiry",
            ("NIFTY", "CE", "PE"),
        )
        rows = [(r[0], r[1]) for r in cur.fetchall() if r[0]]
        conn.close()
        today = ist_now().date()
        for raw, lotsize in rows:
            for fmt in ("%d-%b-%y", "%d-%b-%Y"):
                try:
                    exp_date = datetime.strptime(raw, fmt).date()
                    if exp_date >= today:
                        return exp_date.strftime("%d%b%y").upper(), int(lotsize or 65)
                except ValueError:
                    continue
        return None, 65
    except Exception as e:
        log(f"ERROR getting expiry: {e}")
        return None, 65


def get_current_premium(adapter, expiry, spec):
    """Re-fetch combined LTP of all SELL legs for MTM tracking."""
    chain = adapter.get_option_chain(expiry)
    if not chain:
        return None
    sell_strikes = {(leg.strike, leg.option_type) for leg in spec.legs if leg.action == "SELL"}
    total = 0.0
    for row in chain.get("chain", []):
        for side in ("ce", "pe"):
            leg_data = row.get(side) or {}
            strike = int(float(row.get("strike") or 0))
            otype  = side.upper()
            if (strike, otype) in sell_strikes:
                total += float(leg_data.get("ltp") or 0)
    return total


# ── entry window ──────────────────────────────────────────────────────────────
ENTRY_START = (9, 20)
ENTRY_END   = (9, 45)


def in_entry_window():
    now = ist_now()
    # Skip Monday — overnight gap risk
    if now.weekday() == 0:
        return False
    return ENTRY_START <= hm(now) <= ENTRY_END


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

                # 5. execute
                ok = executor.execute(spec, expiry, log)
                if ok:
                    state.enter(spec, snapshot.straddle_premium)
                    log(f"ENTERED {spec.strategy} at premium Rs.{snapshot.straddle_premium}")

                time.sleep(SLEEP_SECONDS)
                continue

            # ── MTM monitoring ────────────────────────────────────────────────
            if state.in_trade and spec:
                current = get_current_premium(adapter, expiry, spec)
                if current is None:
                    log("WARNING: Could not fetch current premium for MTM")
                    time.sleep(SLEEP_SECONDS)
                    continue

                state.update_mtm(current, qty)
                log(f"MTM=Rs.{state.mtm_pnl}  Entry={state.entry_premium}  Now={current}")

                if state.should_stop(STOP_LOSS_INR):
                    log(f"STOP HIT Rs.{state.mtm_pnl} — exiting")
                    executor.close_all(STRATEGY_NAME, log)
                    state.exit()
                    continue

                if state.should_target(TARGET_INR):
                    log(f"TARGET HIT Rs.{state.mtm_pnl} — booking profit")
                    executor.close_all(STRATEGY_NAME, log)
                    state.exit()
                    continue

        except Exception as e:
            log(f"ERROR: {e}")

        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    run()
