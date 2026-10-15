"""
adapter.py
----------
Market data adapter — all OpenAlgo client calls live here ONLY.
Nothing outside this file should call the OpenAlgo client directly.

If you switch broker or data source tomorrow, only this file changes.
"""

from typing import Optional


class MarketDataAdapter:
    """Fetches raw market data from OpenAlgo. Returns None on any failure."""

    def __init__(self, client):
        self._c = client

    def get_vix(self) -> Optional[float]:
        try:
            r = self._c.quotes(symbol="INDIA VIX", exchange="NSE_INDEX")
            val = (r.get("data") or r).get("ltp") or (r.get("data") or r).get("close")
            return float(val) if val else None
        except Exception:
            return None

    def get_nifty_spot(self) -> Optional[float]:
        try:
            r = self._c.quotes(symbol="NIFTY", exchange="NSE_INDEX")
            val = (r.get("data") or r).get("ltp")
            return float(val) if val else None
        except Exception:
            return None

    def get_option_chain(self, expiry: str, strike_count: int = 10) -> Optional[dict]:
        try:
            r = self._c.optionchain(
                underlying="NIFTY",
                exchange="NSE_INDEX",
                expiry_date=expiry,
                strike_count=strike_count,
                with_greeks=False,
                interest_rate=0,
            )
            if r.get("status") == "error":
                return None
            return r
        except Exception:
            return None
