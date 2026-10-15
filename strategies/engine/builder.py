"""
builder.py
----------
Strategy builder + risk gate.
Takes a TradeDecision + MarketSnapshot, returns a TradeSpec or None.

Responsibilities:
    1. Select strikes for each strategy
    2. Compute max loss and max profit
    3. Gate on risk/reward — reject if not worth it
    4. Return exact symbols and quantities to execute

Strike selection rules:
    SHORT_STRADDLE    — ATM CE + ATM PE
    BULL_PUT_SPREAD   — sell ATM-1 PE, buy ATM-3 PE (collect credit, cap loss)
    BEAR_CALL_SPREAD  — sell ATM+1 CE, buy ATM+3 CE (collect credit, cap loss)
    IRON_CONDOR       — sell ATM±2, buy ATM±4 (both sides)

NIFTY strike interval = 50 points
"""

from dataclasses import dataclass, field
from typing import Optional, List, Tuple
from .normalizer import MarketSnapshot
from .decision import TradeDecision

STRIKE_INTERVAL = 50  # NIFTY strikes are 50 points apart


@dataclass
class Leg:
    symbol: str         # resolved later by executor from option chain
    strike: int
    option_type: str    # CE or PE
    action: str         # BUY or SELL
    qty: int


@dataclass
class TradeSpec:
    strategy: str
    legs: List[Leg]
    max_loss_inr: float
    max_profit_inr: float
    risk_reward: float
    entry_reason: str
    reject_reason: str = ""

    @property
    def is_valid(self) -> bool:
        return self.reject_reason == ""


def _round_to_strike(price: float) -> int:
    return int(round(price / STRIKE_INTERVAL) * STRIKE_INTERVAL)


def _build_short_straddle(snap: MarketSnapshot, qty: int, max_loss_inr: float) -> TradeSpec:
    atm = snap.atm_strike
    premium = snap.straddle_premium
    max_profit = round(premium * qty, 0)
    # straddle has unlimited loss — we use stop loss as max loss
    rr = round(max_profit / max_loss_inr, 2) if max_loss_inr > 0 else 0

    legs = [
        Leg(symbol="", strike=atm, option_type="CE", action="SELL", qty=qty),
        Leg(symbol="", strike=atm, option_type="PE", action="SELL", qty=qty),
    ]
    spec = TradeSpec(
        strategy="SHORT_STRADDLE",
        legs=legs,
        max_loss_inr=max_loss_inr,
        max_profit_inr=max_profit,
        risk_reward=rr,
        entry_reason=f"ATM={atm} Premium=Rs.{premium} Qty={qty}",
    )
    if rr < 0.5:
        spec.reject_reason = f"Risk/reward {rr} too low for straddle (min 0.5)"
    return spec


def _build_bull_put_spread(snap: MarketSnapshot, qty: int) -> TradeSpec:
    atm = snap.atm_strike
    sell_strike = atm - STRIKE_INTERVAL          # 1 strike below ATM
    buy_strike  = atm - (3 * STRIKE_INTERVAL)    # 3 strikes below ATM (hedge)
    spread_width = sell_strike - buy_strike       # 100 points

    # Approximate: sell strike PE premium ~ 40-60% of ATM PE
    sell_premium = round(snap.atm_pe_ltp * 0.50, 2)
    buy_premium  = round(snap.atm_pe_ltp * 0.20, 2)
    net_credit   = round(sell_premium - buy_premium, 2)

    max_profit = round(net_credit * qty, 0)
    max_loss   = round((spread_width - net_credit) * qty, 0)
    rr = round(max_profit / max_loss, 2) if max_loss > 0 else 0

    legs = [
        Leg(symbol="", strike=sell_strike, option_type="PE", action="SELL", qty=qty),
        Leg(symbol="", strike=buy_strike,  option_type="PE", action="BUY",  qty=qty),
    ]
    spec = TradeSpec(
        strategy="BULL_PUT_SPREAD",
        legs=legs,
        max_loss_inr=max_loss,
        max_profit_inr=max_profit,
        risk_reward=rr,
        entry_reason=f"Sell PE {sell_strike} Buy PE {buy_strike} Credit~Rs.{net_credit} Qty={qty}",
    )
    if net_credit < 20:
        spec.reject_reason = f"Net credit Rs.{net_credit} too low — spread not worth it"
    elif rr < 0.25:
        spec.reject_reason = f"Risk/reward {rr} too low for bull put (min 0.25)"
    return spec


def _build_bear_call_spread(snap: MarketSnapshot, qty: int) -> TradeSpec:
    atm = snap.atm_strike
    sell_strike = atm + STRIKE_INTERVAL          # 1 strike above ATM
    buy_strike  = atm + (3 * STRIKE_INTERVAL)    # 3 strikes above ATM (hedge)
    spread_width = buy_strike - sell_strike       # 100 points

    sell_premium = round(snap.atm_ce_ltp * 0.50, 2)
    buy_premium  = round(snap.atm_ce_ltp * 0.20, 2)
    net_credit   = round(sell_premium - buy_premium, 2)

    max_profit = round(net_credit * qty, 0)
    max_loss   = round((spread_width - net_credit) * qty, 0)
    rr = round(max_profit / max_loss, 2) if max_loss > 0 else 0

    legs = [
        Leg(symbol="", strike=sell_strike, option_type="CE", action="SELL", qty=qty),
        Leg(symbol="", strike=buy_strike,  option_type="CE", action="BUY",  qty=qty),
    ]
    spec = TradeSpec(
        strategy="BEAR_CALL_SPREAD",
        legs=legs,
        max_loss_inr=max_loss,
        max_profit_inr=max_profit,
        risk_reward=rr,
        entry_reason=f"Sell CE {sell_strike} Buy CE {buy_strike} Credit~Rs.{net_credit} Qty={qty}",
    )
    if net_credit < 20:
        spec.reject_reason = f"Net credit Rs.{net_credit} too low — spread not worth it"
    elif rr < 0.25:
        spec.reject_reason = f"Risk/reward {rr} too low for bear call (min 0.25)"
    return spec


def _build_iron_condor(snap: MarketSnapshot, qty: int) -> TradeSpec:
    atm = snap.atm_strike
    sell_ce = atm + (2 * STRIKE_INTERVAL)
    buy_ce  = atm + (4 * STRIKE_INTERVAL)
    sell_pe = atm - (2 * STRIKE_INTERVAL)
    buy_pe  = atm - (4 * STRIKE_INTERVAL)
    spread_width = 2 * STRIKE_INTERVAL  # 100 points each side

    ce_credit = round(snap.atm_ce_ltp * 0.30, 2)
    pe_credit = round(snap.atm_pe_ltp * 0.30, 2)
    net_credit = round(ce_credit + pe_credit, 2)

    max_profit = round(net_credit * qty, 0)
    max_loss   = round((spread_width - net_credit) * qty, 0)
    rr = round(max_profit / max_loss, 2) if max_loss > 0 else 0

    legs = [
        Leg(symbol="", strike=sell_ce, option_type="CE", action="SELL", qty=qty),
        Leg(symbol="", strike=buy_ce,  option_type="CE", action="BUY",  qty=qty),
        Leg(symbol="", strike=sell_pe, option_type="PE", action="SELL", qty=qty),
        Leg(symbol="", strike=buy_pe,  option_type="PE", action="BUY",  qty=qty),
    ]
    spec = TradeSpec(
        strategy="IRON_CONDOR",
        legs=legs,
        max_loss_inr=max_loss,
        max_profit_inr=max_profit,
        risk_reward=rr,
        entry_reason=f"CE {sell_ce}/{buy_ce} PE {sell_pe}/{buy_pe} Credit~Rs.{net_credit} Qty={qty}",
    )
    if net_credit < 30:
        spec.reject_reason = f"Net credit Rs.{net_credit} too low for condor (min Rs.30)"
    elif rr < 0.20:
        spec.reject_reason = f"Risk/reward {rr} too low for condor (min 0.20)"
    return spec


def build(
    decision: TradeDecision,
    snap: MarketSnapshot,
    qty: int,
    stop_loss_inr: float,
) -> TradeSpec:
    """
    Build a TradeSpec from a decision.
    Returns a TradeSpec with reject_reason set if risk gate fails.
    """
    s = decision.strategy

    if s == "SHORT_STRADDLE":
        return _build_short_straddle(snap, qty, stop_loss_inr)
    if s == "BULL_PUT_SPREAD":
        return _build_bull_put_spread(snap, qty)
    if s == "BEAR_CALL_SPREAD":
        return _build_bear_call_spread(snap, qty)
    if s == "IRON_CONDOR":
        return _build_iron_condor(snap, qty)

    return TradeSpec(
        strategy="NO_TRADE",
        legs=[],
        max_loss_inr=0,
        max_profit_inr=0,
        risk_reward=0,
        entry_reason="",
        reject_reason=decision.reason,
    )
