"""
decision.py
-----------
Strategy decision engine — pure Python, zero OpenAlgo dependency.
Takes a validated MarketSnapshot, returns (strategy, reason).

Strategies:
    BULL_PUT_SPREAD   — sell OTM put spread, bullish bias
    BEAR_CALL_SPREAD  — sell OTM call spread, bearish bias
    IRON_CONDOR       — sell both sides, neutral + normal IV
    SHORT_STRADDLE    — sell ATM CE+PE, neutral + VERY HIGH IV only (VIX >= 18)
    NO_TRADE          — conditions not met

VIX regimes:
    LOW     < 11    — premium crushed, don't sell
    NORMAL  11-17   — spreads and condors only (defined risk)
    HIGH    17-22   — spreads preferred; straddle only if VIX >= 18 AND neutral
    EXTREME > 22    — gap risk too high, no trade

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
    if vix < 17:
        return "NORMAL"   # 11-16.99 — spreads/condors only
    if vix <= 22:
        return "HIGH"     # 17-22 — spreads preferred; straddle only if VIX >= 18
    return "EXTREME"


def _bias(pcr: float) -> str:
    """
    PCR interpretation (contrarian):
    High PCR = lots of put buying/writing = fear = market likely to hold or bounce = BULLISH
    Low PCR  = lots of call buying/writing = greed = market likely to stall or fall = BEARISH

    Thresholds calibrated against Sensibull full-market PCR:
        >= 1.3  → BULLISH  (strong put OI dominance)
        <= 0.65 → BEARISH  (strong call OI dominance — tightened from 0.75)
        else    → NEUTRAL  (wider neutral band reduces false signals)
    """
    if pcr >= 1.3:
        return "BULLISH"
    if pcr <= 0.65:
        return "BEARISH"
    return "NEUTRAL"


def decide(snap: MarketSnapshot) -> TradeDecision:
    """
    Core decision logic.

    Rules (in priority order):

    1. Premium too low → NO_TRADE always

    2. EXTREME VIX (>22) → NO_TRADE
       Gap risk too high.

    3. LOW VIX (<11) → NO_TRADE
       Premium crushed.

    4. HIGH VIX (17-22) + NEUTRAL + VIX >= 18 → SHORT_STRADDLE
       Genuine elevated IV. Enough premium to justify unlimited-risk straddle.

    5. HIGH VIX (17-22) + NEUTRAL + VIX < 18 → IRON_CONDOR
       Borderline IV. Defined risk is safer.

    6. HIGH VIX + BULLISH → BULL_PUT_SPREAD

    7. HIGH VIX + BEARISH → BEAR_CALL_SPREAD

    8. NORMAL VIX (11-17) + NEUTRAL → IRON_CONDOR

    9. NORMAL VIX + BULLISH → BULL_PUT_SPREAD

    10. NORMAL VIX + BEARISH → BEAR_CALL_SPREAD
    """
    iv  = _iv_regime(snap.vix)
    bias = _bias(snap.pcr)
    prem = snap.straddle_premium

    # ── Rule 1: minimum premium gate ────────────────────────────────────────
    MIN_PREMIUM = 80.0
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

    # ── Rule 3b: VIX spiking ─────────────────────────────────────────────────
    # A rising VIX means the market expects more movement — bad for short options.
    # Block all premium-selling strategies when VIX is up >5% intraday.
    VIX_SPIKE_THRESHOLD = 5.0
    if snap.vix_change_pct >= VIX_SPIKE_THRESHOLD:
        return TradeDecision(
            strategy="NO_TRADE",
            bias=bias,
            iv_regime=iv,
            reason=f"VIX spiking +{snap.vix_change_pct}% today — short premium too risky",
        )

    # ── Rule 4-6: HIGH VIX (17-22) ───────────────────────────────────────────
    # SHORT_STRADDLE only when VIX >= 18 (genuine elevated IV) AND neutral bias.
    # VIX 17-17.99 is borderline — use defined-risk IRON_CONDOR instead.
    if iv == "HIGH":
        if bias == "NEUTRAL":
            if snap.vix >= 18.0:
                return TradeDecision(
                    strategy="SHORT_STRADDLE",
                    bias=bias,
                    iv_regime=iv,
                    reason=f"VIX={snap.vix} >= 18 + neutral PCR={snap.pcr} — genuine high IV straddle",
                )
            else:
                return TradeDecision(
                    strategy="IRON_CONDOR",
                    bias=bias,
                    iv_regime=iv,
                    reason=f"VIX={snap.vix} borderline (17-18) + neutral PCR={snap.pcr} — condor safer than straddle",
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
