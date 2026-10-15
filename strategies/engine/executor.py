"""
executor.py
-----------
Execution layer — paper mode logs only, live mode calls OpenAlgo.
The decision engine never touches this file directly.

Symbol resolution:
    Converts (strike, option_type, expiry) → actual trading symbol
    e.g. strike=22600, CE, expiry=03NOV26 → NIFTY03NOV2622600CE
"""

from typing import Optional
from .builder import TradeSpec, Leg


def _resolve_symbol(strike: int, option_type: str, expiry: str) -> str:
    """Build NSE option symbol from components."""
    return f"NIFTY{expiry}{strike}{option_type}"


def _resolve_legs(spec: TradeSpec, expiry: str) -> TradeSpec:
    """Fill in symbol field for all legs."""
    for leg in spec.legs:
        leg.symbol = _resolve_symbol(leg.strike, leg.option_type, expiry)
    return spec


class Executor:
    def __init__(self, client, strategy_name: str, product: str, paper: bool = True):
        self._c             = client
        self._strategy_name = strategy_name
        self._product       = product
        self._paper         = paper

    def execute(self, spec: TradeSpec, expiry: str, log_fn) -> bool:
        """
        Execute all legs of a TradeSpec.
        Returns True if all legs placed successfully.
        """
        spec = _resolve_legs(spec, expiry)

        mode = "PAPER" if self._paper else "LIVE"
        log_fn(f"[{mode}] Executing {spec.strategy} | {spec.entry_reason}")

        success = True
        for leg in spec.legs:
            ok = self._place(leg, log_fn)
            if not ok:
                success = False
        return success

    def close_all(self, strategy_name: str, log_fn):
        mode = "PAPER" if self._paper else "LIVE"
        log_fn(f"[{mode}] Closing all positions for {strategy_name}")
        if not self._paper:
            r = self._c.closeposition(strategy=strategy_name)
            log_fn(f"closeposition response: {r}")

    def _place(self, leg: Leg, log_fn) -> bool:
        log_fn(
            f"  {leg.action} {leg.symbol} qty={leg.qty} "
            f"[{leg.option_type} strike={leg.strike}]"
        )
        if self._paper:
            return True
        try:
            r = self._c.placeorder(
                strategy=self._strategy_name,
                symbol=leg.symbol,
                exchange="NFO",
                action=leg.action,
                price_type="MARKET",
                product=self._product,
                quantity=leg.qty,
            )
            log_fn(f"  response: {r}")
            return True
        except Exception as e:
            log_fn(f"  ERROR placing {leg.symbol}: {e}")
            return False
