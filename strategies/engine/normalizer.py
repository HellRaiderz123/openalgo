"""
normalizer.py
-------------
Signal normalizer — validates raw market data before it reaches the decision engine.
Returns a clean MarketSnapshot or None with a reason.

If this returns None, the strategy does NOT trade. No exceptions.
"""

from dataclasses import dataclass
from typing import Optional, Tuple
from datetime import datetime, timezone, timedelta

IST = timedelta(hours=5, minutes=30)


def _ist_now():
    return datetime.now(timezone.utc) + IST


@dataclass
class MarketSnapshot:
    """Clean, validated market data. All fields guaranteed non-None."""
    vix: float
    spot: float
    pcr: float
    atm_strike: int
    atm_ce_ltp: float
    atm_pe_ltp: float
    straddle_premium: float
    total_ce_oi: float
    total_pe_oi: float
    timestamp: datetime


def _compute_pcr_and_atm(chain: dict, spot: float) -> Tuple[float, int, float, float, float, float]:
    """Extract PCR, ATM strike, ATM LTPs from raw option chain."""
    rows = chain.get("chain", [])
    total_ce_oi = 0.0
    total_pe_oi = 0.0
    atm_ce_ltp  = 0.0
    atm_pe_ltp  = 0.0
    atm_strike  = 0

    # find ATM strike = closest to spot
    best_diff = float("inf")
    for row in rows:
        strike = float(row.get("strike") or 0)
        if strike <= 0:
            continue
        diff = abs(strike - spot)
        if diff < best_diff:
            best_diff = diff
            atm_strike = int(strike)

    for row in rows:
        strike = float(row.get("strike") or 0)
        ce = row.get("ce") or {}
        pe = row.get("pe") or {}
        total_ce_oi += float(ce.get("oi") or 0)
        total_pe_oi += float(pe.get("oi") or 0)
        if int(strike) == atm_strike:
            atm_ce_ltp = float(ce.get("ltp") or 0)
            atm_pe_ltp = float(pe.get("ltp") or 0)

    pcr = (total_pe_oi / total_ce_oi) if total_ce_oi > 0 else 1.0
    return round(pcr, 3), atm_strike, atm_ce_ltp, atm_pe_ltp, total_ce_oi, total_pe_oi


def normalize(
    vix: Optional[float],
    spot: Optional[float],
    chain: Optional[dict],
) -> Tuple[Optional[MarketSnapshot], str]:
    """
    Validate and normalize raw market data.

    Returns:
        (MarketSnapshot, "OK")          — all checks passed
        (None, "reason string")         — something failed, do not trade
    """
    now = _ist_now()

    # ── session check ────────────────────────────────────────────────────────
    if now.weekday() >= 5:
        return None, "Weekend — market closed"

    hm = (now.hour, now.minute)
    if not ((9, 15) <= hm <= (15, 30)):
        return None, "Outside market hours"

    # ── VIX ──────────────────────────────────────────────────────────────────
    if vix is None:
        return None, "VIX data unavailable"
    if not (5.0 <= vix <= 50.0):
        return None, f"VIX={vix} looks stale or invalid"

    # ── spot ─────────────────────────────────────────────────────────────────
    if spot is None:
        return None, "NIFTY spot unavailable"
    if not (10_000 <= spot <= 50_000):
        return None, f"NIFTY spot={spot} looks invalid"

    # ── option chain ─────────────────────────────────────────────────────────
    if chain is None:
        return None, "Option chain unavailable"

    rows = chain.get("chain", [])
    if len(rows) < 4:
        return None, f"Option chain too thin — only {len(rows)} rows"

    pcr, atm_strike, atm_ce_ltp, atm_pe_ltp, ce_oi, pe_oi = _compute_pcr_and_atm(chain, spot)

    if atm_strike == 0:
        return None, "Could not find ATM strike"

    if atm_ce_ltp <= 0 or atm_pe_ltp <= 0:
        return None, f"ATM LTPs invalid — CE={atm_ce_ltp} PE={atm_pe_ltp}"

    if ce_oi <= 0 or pe_oi <= 0:
        return None, "OI data missing — cannot compute PCR"

    straddle_premium = round(atm_ce_ltp + atm_pe_ltp, 2)

    snapshot = MarketSnapshot(
        vix=vix,
        spot=spot,
        pcr=pcr,
        atm_strike=atm_strike,
        atm_ce_ltp=atm_ce_ltp,
        atm_pe_ltp=atm_pe_ltp,
        straddle_premium=straddle_premium,
        total_ce_oi=ce_oi,
        total_pe_oi=pe_oi,
        timestamp=now,
    )
    return snapshot, "OK"
