"""
adapter.py
----------
Market data adapter — all OpenAlgo client calls live here ONLY.
Nothing outside this file should call the OpenAlgo client directly.

PCR strategy:
    PCR is fetched from the full option chain (strike_count=None = all strikes
    for the expiry). This is the widest OI available through the broker and
    matches what the OpenAlgo OI Tracker page shows. It still covers one expiry
    only (Sensibull aggregates all expiries), so expect a small difference, but
    it is far more accurate than computing PCR from 20 ATM strikes.
"""

from typing import Optional


class MarketDataAdapter:
    """Fetches raw market data from OpenAlgo. Returns None on any failure."""

    def __init__(self, client):
        self._c = client

    def get_vix_data(self) -> tuple:
        """Returns (ltp, prev_close) for INDIAVIX in a single API call."""
        try:
            r    = self._c.quotes(symbol="INDIAVIX", exchange="NSE_INDEX")
            data = r.get("data") or r
            ltp  = data.get("ltp") or data.get("close")
            prev = data.get("prev_close") or data.get("close_price") or data.get("previous_close")
            return (float(ltp) if ltp else None, float(prev) if prev else None)
        except Exception:
            return (None, None)

    def get_nifty_spot(self) -> Optional[float]:
        try:
            r = self._c.quotes(symbol="NIFTY", exchange="NSE_INDEX")
            val = (r.get("data") or r).get("ltp")
            return float(val) if val else None
        except Exception:
            return None

    def get_option_chain_with_pcr(self, expiry: str, strike_count: int = 20) -> tuple:
        """
        Single broker call — returns (chain_dict, pcr).
        Avoids the double optionchain call that get_option_chain + get_pcr_full caused.
        """
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
                return None, None
            rows   = r.get("chain", [])
            ce_oi  = sum(float((row.get("ce") or {}).get("oi") or 0) for row in rows)
            pe_oi  = sum(float((row.get("pe") or {}).get("oi") or 0) for row in rows)
            pcr    = round(pe_oi / ce_oi, 3) if ce_oi > 0 and pe_oi > 0 else None
            return r, pcr
        except Exception:
            return None, None

    def get_option_chain(self, expiry: str, strike_count: int = 20) -> Optional[dict]:
        chain, _ = self.get_option_chain_with_pcr(expiry, strike_count)
        return chain
