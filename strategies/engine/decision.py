"""
decision.py
-----------
Strategy decision engine — pure Python, zero OpenAlgo dependency.
Takes a validated MarketSnapshot, returns (strategy, reason).

Strategies:
    BULL_PUT_SPREAD   — sell OTM put spread, bullish bias
    BEAR_CALL_SPREAD  — sell OTM call spread, bearish bias
    IRON_CONDOR       — sell both sides, neutral + high IV
    SHORT_STRADDLE    — sell ATM CE+PE, neutral + very high IV
    NO_TRADE          — conditions not met

Decision logic uses only 3 inputs:
    1. VIX         — IV regime
    2. PCR         — market bias
    3. Premium     — worth trading?

Deliberately simple. No RSI, EMA, MACD — those are for directional trades.
Short options need IV + positioning data, not price momentum.
"""

from dataclasses import dataclass
from typing import Tuple
from .normalizer import MarketSnapshot


@dataclass
class TradeDecision:
    strategy: str       # BULL_PUT_SPREAD | BEAR_CALL_SPREAD | IRON_CONDOR | SHORT_STRADDLE | NO_TRADE
    bias: str           # BULLISH | BEARISH | NEUTRAL | UNCERTAIN
    iv_regime: str      # LOW | NORMAL | HIGH | EXTREME
    reason: str


def _iv_regime(vix: float) -> str:
    if vix < 11:
        return "LOW"
    if vix <= 16:
        return "NORMAL"
    if vix <= 22:
        return "HIGH"
    return "EXTREME"


def _bias(pcr: float) -> str:
    """
    PCR interpretation (contrarian):
    High PCR = lots of put buying = fear = market likely to hold or bounce = BULLISH
    Low PCR  = lots of call buying = greed = market likely to stall or fall = BEARISH
    """
    if pcr >= 1.3:
        return "BULLISH"
    if pcr <= 0.75:
        return "BEARISH"
    return "NEUTRAL"


def decide(snap: MarketSnapshot) -> TradeDecision:
    """
    Core decision logic.

    Rules (in priority order):

    1. Premium too low → NO_TRADE always
       No point selling if there's nothing to collect.

    2. EXTREME VIX (>22) → NO_TRADE
       Gap risk too high. Market is in panic. Short options get destroyed.

    3. LOW VIX (<11) → NO_TRADE
       Premium is crushed. Risk/reward doesn't work for sellers.

    4. HIGH VIX (16-22) + NEUTRAL bias → SHORT_STRADDLE
       Best environment for straddle sellers. High premium, range-bound.

    5. HIGH VIX (16-22) + BULLISH bias → BULL_PUT_SPREAD
       High IV means fat put premiums. Sell puts with hedge.

    6. HIGH VIX (16-22) + BEARISH bias → BEAR_CALL_SPREAD
       High IV means fat call premiums. Sell calls with hedge.

    7. NORMAL VIX (11-16) + NEUTRAL → IRON_CONDOR
       Defined risk, collect from both sides in range.

    8. NORMAL VIX (11-16) + BULLISH → BULL_PUT_SPREAD
       Sell OTM puts, keep upside.

    9. NORMAL VIX (11-16) + BEARISH → BEAR_CALL_SPREAD
       Sell OTM calls, keep downside.
    """
    iv  = _iv_regime(snap.vix)
    bias = _bias(snap.pcr)
    prem = snap.straddle_premium

    # ── Rule 1: minimum premium gate ────────────────────────────────────────
    MIN_PREMIUM = 120.0
    if prem < MIN_PREMIUM:
        return TradeDecision(
            strategy="NO_TRADE",
            bias=bias,
            iv_regime=iv,
            reason=f"Premium Rs.{prem} below minimum Rs.{MIN_PREMIUM} — not worth the risk",
        )

    # ── Rule 2: extreme VIX ──────────────────────────────────────────────────
    if iv == "EXTREME":
        return TradeDecision(
            strategy="NO_TRADE",
            bias=bias,
            iv_regime=iv,
            reason=f"VIX={snap.vix} extreme — gap risk too high for short options",
        )

    # ── Rule 3: low VIX ──────────────────────────────────────────────────────
    if iv == "LOW":
        return TradeDecision(
            strategy="NO_TRADE",
            bias=bias,
            iv_regime=iv,
            reason=f"VIX={snap.vix} too low — premium crushed, risk/reward unfavorable",
        )

    # ── Rule 4-6: HIGH VIX ───────────────────────────────────────────────────
    if iv == "HIGH":
        if bias == "NEUTRAL":
            return TradeDecision(
                strategy="SHORT_STRADDLE",
                bias=bias,
                iv_regime=iv,
                reason=f"HIGH IV + neutral PCR={snap.pcr} — ideal straddle environment",
            )
        if bias == "BULLISH":
            return TradeDecision(
                strategy="BULL_PUT_SPREAD",
                bias=bias,
                iv_regime=iv,
                reason=f"HIGH IV + bullish PCR={snap.pcr} — fat put premiums to sell",
            )
        if bias == "BEARISH":
            return TradeDecision(
                strategy="BEAR_CALL_SPREAD",
                bias=bias,
                iv_regime=iv,
                reason=f"HIGH IV + bearish PCR={snap.pcr} — fat call premiums to sell",
            )

    # ── Rule 7-9: NORMAL VIX ─────────────────────────────────────────────────
    if iv == "NORMAL":
        if bias == "NEUTRAL":
            return TradeDecision(
                strategy="IRON_CONDOR",
                bias=bias,
                iv_regime=iv,
                reason=f"NORMAL IV + neutral PCR={snap.pcr} — defined risk condor",
            )
        if bias == "BULLISH":
            return TradeDecision(
                strategy="BULL_PUT_SPREAD",
                bias=bias,
                iv_regime=iv,
                reason=f"NORMAL IV + bullish PCR={snap.pcr} — sell OTM puts",
            )
        if bias == "BEARISH":
            return TradeDecision(
                strategy="BEAR_CALL_SPREAD",
                bias=bias,
                iv_regime=iv,
                reason=f"NORMAL IV + bearish PCR={snap.pcr} — sell OTM calls",
            )

    # ── fallback ─────────────────────────────────────────────────────────────
    return TradeDecision(
        strategy="NO_TRADE",
        bias=bias,
        iv_regime=iv,
        reason=f"No matching rule — VIX={snap.vix} PCR={snap.pcr} Premium={prem}",
    )
