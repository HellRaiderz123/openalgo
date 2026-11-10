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
    vix_change_pct: float = 0.0  # % change from prev close; 0.0 if unavailable
    _chain_rows: list = None  # raw chain rows — used by builder for real strike LTPs


def _compute_pcr_and_atm(chain: dict, spot: float) -> Tuple[float, int, float, float, float, float]:
    """
    Extract PCR, ATM strike, ATM LTPs from raw option chain.

    PCR is computed across ALL strikes in the chain (wider = more accurate).
    ATM LTPs are taken from the strike nearest to spot.
    """
    rows = chain.get("chain", [])
    total_ce_oi = 0.0
    total_pe_oi = 0.0
    atm_ce_ltp  = 0.0
    atm_pe_ltp  = 0.0

    # ATM = nearest 50-point strike to spot
    atm_strike = int(round(spot / 50) * 50)

    # First pass: accumulate full OI across all strikes for accurate PCR
    for row in rows:
        ce = row.get("ce") or {}
        pe = row.get("pe") or {}
        total_ce_oi += float(ce.get("oi") or 0)
        total_pe_oi += float(pe.get("oi") or 0)

    # Second pass: find best ATM LTP (closest strike to spot, not just exact match)
    best_dist = float("inf")
    for row in rows:
        strike = int(float(row.get("strike") or 0))
        dist = abs(strike - atm_strike)
        if dist < best_dist:
            ce = row.get("ce") or {}
            pe = row.get("pe") or {}
            ce_ltp = float(ce.get("ltp") or ce.get("last_price") or ce.get("close") or 0)
            pe_ltp = float(pe.get("ltp") or pe.get("last_price") or pe.get("close") or 0)
            if ce_ltp > 0 and pe_ltp > 0:
                atm_ce_ltp = ce_ltp
                atm_pe_ltp = pe_ltp
                best_dist  = dist

    pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi > 0 else 1.0
    return pcr, atm_strike, atm_ce_ltp, atm_pe_ltp, total_ce_oi, total_pe_oi


def normalize(
    vix: Optional[float],
    spot: Optional[float],
    chain: Optional[dict],
    external_pcr: Optional[float] = None,
    vix_prev_close: Optional[float] = None,
) -> Tuple[Optional[MarketSnapshot], str]:
    """
    Validate and normalize raw market data.

    external_pcr: Full-market PCR from broker's full option chain (all strikes, one expiry).
        When provided, this overrides the chain-computed PCR.
        Chain PCR only covers strike_count strikes of one expiry — always wrong.

    Returns:
        (MarketSnapshot, "OK")      — all checks passed
        (None, "reason string")     — something failed, do not trade
    """
    now = _ist_now()

    # ── session check ────────────────────────────────────────────────────────
    if now.weekday() >= 5:
        return None, "Weekend — market closed"

    hm = (now.hour, now.minute)
    if not ((9, 15) <= hm <= (15, 30)):
        return None, "Outside market hours"

    # ── market open warmup guard ─────────────────────────────────────────────
    # OI data is unreliable for the first 20 minutes after open.
    # PCR computed before 09:35 will be stale/partial and cause wrong decisions.
    if hm < (9, 35):
        return None, f"Market open warmup — OI data unreliable before 09:35 (now {now.hour:02d}:{now.minute:02d})"

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

    # Use full-market PCR when available and sane — chain PCR is always partial
    # Valid NIFTY PCR range: 0.1 to 5.0. Outside this = bad/missing OI data.
    PCR_MIN, PCR_MAX = 0.1, 5.0
    if external_pcr is not None and PCR_MIN <= external_pcr <= PCR_MAX:
        pcr = external_pcr
    elif ce_oi > 0 and pe_oi > 0 and PCR_MIN <= pcr <= PCR_MAX:
        pass  # use chain-computed pcr as-is
    else:
        return None, f"PCR={pcr} invalid (OI data bad or missing) — skipping to avoid wrong decision"

    straddle_premium = round(atm_ce_ltp + atm_pe_ltp, 2)

    vix_change_pct = 0.0
    if vix_prev_close and vix_prev_close > 0:
        vix_change_pct = round((vix - vix_prev_close) / vix_prev_close * 100, 2)

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
        vix_change_pct=vix_change_pct,
        _chain_rows=rows,
    )
    return snapshot, "OK"
