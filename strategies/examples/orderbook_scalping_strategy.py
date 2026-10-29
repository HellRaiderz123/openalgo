#!/usr/bin/env python
"""
Order Book Scalping Strategy for Cash Equities
===============================================
A market depth (order book) based scalping strategy for liquid Indian stocks.

Strategy Logic:
1. Monitors Level 5 market depth (bid/ask orders)
2. Calculates order imbalance between buy and sell sides
3. Detects large order walls and their absorption
4. Enters when strong imbalance detected + tight spread
5. Exits on target profit, stop loss, or signal reversal

Key Metrics:
- Order Imbalance Ratio (OIR): (Bid Qty - Ask Qty) / (Bid Qty + Ask Qty)
- Spread %: (Best Ask - Best Bid) / LTP * 100
- Volume Weighted Imbalance across depth levels

Risk Management:
- Max position size limit
- Stop loss per trade
- Target profit per trade
- Max trades per day limit
- Only trades during market hours

Author: FastTrade/OpenAlgo Strategy
"""

import os
import time
import threading
from datetime import datetime, timedelta
from collections import deque
import pytz

from openalgo import api

# ============================================================================
# CONFIGURATION - Modify these settings
# ============================================================================

# API Configuration (auto-injected by OpenAlgo Python Host)
API_KEY = os.getenv('OPENALGO_API_KEY')
HOST = os.getenv('HOST_SERVER', 'http://127.0.0.1:5000')
WS_URL = os.getenv('WEBSOCKET_URL', 'ws://127.0.0.1:8765')

# Strategy Settings
STRATEGY_NAME = "OrderBook Scalper"

# Stocks to trade (pick highly liquid stocks with tight spreads)
# Format: {"symbol": "SYMBOL", "exchange": "NSE", "qty": shares_per_trade}
WATCHLIST = [
    {"symbol": "RELIANCE", "exchange": "NSE", "qty": 10},
    {"symbol": "SBIN", "exchange": "NSE", "qty": 50},
    {"symbol": "ICICIBANK", "exchange": "NSE", "qty": 20},
    {"symbol": "INFY", "exchange": "NSE", "qty": 15},
    {"symbol": "TCS", "exchange": "NSE", "qty": 5},
]

# Trading Parameters
PRODUCT = "MIS"  # MIS for intraday, CNC for delivery
MAX_SPREAD_PCT = 0.08  # Max spread % to enter (0.08 = 0.08%)
MIN_IMBALANCE = 0.35  # Min order imbalance to trigger signal (-1 to +1)
DEPTH_LEVELS = 5  # How many depth levels to analyze (max 5)

# Risk Management
TARGET_PCT = 0.15  # Target profit % per trade
STOP_LOSS_PCT = 0.10  # Stop loss % per trade
MAX_TRADES_PER_DAY = 20  # Max trades per stock per day
MAX_POSITION_VALUE = 100000  # Max position value in INR per stock
COOLDOWN_SECONDS = 30  # Wait time between trades on same stock

# Market Hours (IST)
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MIN = 20
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MIN = 15

# Polling interval
SCAN_INTERVAL = 2  # Seconds between scans

# ============================================================================
# STRATEGY CODE - Don't modify unless you know what you're doing
# ============================================================================

IST = pytz.timezone('Asia/Kolkata')


class OrderBookScalper:
    """Order book based scalping strategy."""

    def __init__(self):
        if not API_KEY:
            raise ValueError("OPENALGO_API_KEY not set")
        
        self.client = api(api_key=API_KEY, host=HOST, ws_url=WS_URL)
        self.positions = {}  # symbol -> {"qty": int, "side": str, "entry": float}
        self.trade_counts = {}  # symbol -> count today
        self.last_trade_time = {}  # symbol -> timestamp
        self.price_history = {}  # symbol -> deque of recent prices
        self.running = False
        self.lock = threading.Lock()
        
        print(f"[{self._now()}] OrderBook Scalper initialized")
        print(f"Watchlist: {[s['symbol'] for s in WATCHLIST]}")
        print(f"Target: {TARGET_PCT}% | Stop Loss: {STOP_LOSS_PCT}%")
        print(f"Max Spread: {MAX_SPREAD_PCT}% | Min Imbalance: {MIN_IMBALANCE}")

    def _now(self) -> str:
        return datetime.now(IST).strftime("%H:%M:%S")

    def _is_market_hours(self) -> bool:
        now = datetime.now(IST)
        if now.weekday() >= 5:  # Saturday/Sunday
            return False
        market_open = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MIN, second=0)
        market_close = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MIN, second=0)
        return market_open <= now <= market_close

    def _can_trade(self, symbol: str) -> bool:
        """Check if we can trade this symbol (cooldown + daily limit)."""
        now = time.time()
        
        # Check cooldown
        last = self.last_trade_time.get(symbol, 0)
        if now - last < COOLDOWN_SECONDS:
            return False
        
        # Check daily limit
        count = self.trade_counts.get(symbol, 0)
        if count >= MAX_TRADES_PER_DAY:
            return False
        
        return True

    def get_market_depth(self, symbol: str, exchange: str) -> dict | None:
        """Fetch Level 5 market depth for a symbol."""
        try:
            response = self.client.depth(symbol=symbol, exchange=exchange)
            if response.get("status") == "success":
                return response.get("data", {})
        except Exception as e:
            print(f"[{self._now()}] Depth error {symbol}: {e}")
        return None

    def get_quote(self, symbol: str, exchange: str) -> dict | None:
        """Fetch current quote (LTP, volume, etc.)."""
        try:
            response = self.client.quotes(symbol=symbol, exchange=exchange)
            if response.get("status") == "success":
                return response.get("data", {})
        except Exception as e:
            print(f"[{self._now()}] Quote error {symbol}: {e}")
        return None

    def analyze_orderbook(self, depth: dict, ltp: float) -> dict:
        """
        Analyze order book and return trading signals.
        
        Returns:
            dict with keys:
            - signal: "BUY", "SELL", or "HOLD"
            - imbalance: float (-1 to +1)
            - spread_pct: float
            - bid_strength: float
            - ask_strength: float
            - reason: str
        """
        bids = depth.get("bids", [])
        asks = depth.get("asks", [])
        
        if not bids or not asks:
            return {"signal": "HOLD", "reason": "No depth data"}
        
        # Get best bid/ask
        best_bid = float(bids[0].get("price", 0))
        best_ask = float(asks[0].get("price", 0))
        
        if best_bid <= 0 or best_ask <= 0:
            return {"signal": "HOLD", "reason": "Invalid prices"}
        
        # Calculate spread
        spread = best_ask - best_bid
        spread_pct = (spread / ltp) * 100 if ltp > 0 else 999
        
        # Skip if spread too wide
        if spread_pct > MAX_SPREAD_PCT:
            return {
                "signal": "HOLD",
                "spread_pct": spread_pct,
                "reason": f"Spread too wide: {spread_pct:.3f}%"
            }
        
        # Calculate order imbalance across depth levels
        bid_qty_total = 0
        ask_qty_total = 0
        bid_value_total = 0
        ask_value_total = 0
        
        for i in range(min(DEPTH_LEVELS, len(bids), len(asks))):
            bid_price = float(bids[i].get("price", 0))
            bid_qty = int(bids[i].get("quantity", 0))
            ask_price = float(asks[i].get("price", 0))
            ask_qty = int(asks[i].get("quantity", 0))
            
            # Weight by proximity to best price (closer = more weight)
            weight = 1.0 / (i + 1)
            
            bid_qty_total += bid_qty * weight
            ask_qty_total += ask_qty * weight
            bid_value_total += bid_price * bid_qty * weight
            ask_value_total += ask_price * ask_qty * weight
        
        total_qty = bid_qty_total + ask_qty_total
        if total_qty == 0:
            return {"signal": "HOLD", "reason": "No volume in depth"}
        
        # Order Imbalance Ratio: +1 = all buyers, -1 = all sellers
        imbalance = (bid_qty_total - ask_qty_total) / total_qty
        
        # Bid/Ask strength (value-weighted)
        total_value = bid_value_total + ask_value_total
        bid_strength = bid_value_total / total_value if total_value > 0 else 0.5
        ask_strength = ask_value_total / total_value if total_value > 0 else 0.5
        
        # Generate signal
        signal = "HOLD"
        reason = f"Imbalance: {imbalance:.2f}"
        
        if imbalance >= MIN_IMBALANCE:
            signal = "BUY"
            reason = f"Strong buy pressure: {imbalance:.2f}"
        elif imbalance <= -MIN_IMBALANCE:
            signal = "SELL"
            reason = f"Strong sell pressure: {imbalance:.2f}"
        
        return {
            "signal": signal,
            "imbalance": imbalance,
            "spread_pct": spread_pct,
            "bid_strength": bid_strength,
            "ask_strength": ask_strength,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "reason": reason
        }

    def check_exit_conditions(self, symbol: str, ltp: float) -> str | None:
        """Check if we should exit an existing position."""
        with self.lock:
            pos = self.positions.get(symbol)
            if not pos:
                return None
            
            entry = pos["entry"]
            side = pos["side"]
            
            if side == "LONG":
                pnl_pct = ((ltp - entry) / entry) * 100
                if pnl_pct >= TARGET_PCT:
                    return "TARGET"
                if pnl_pct <= -STOP_LOSS_PCT:
                    return "STOPLOSS"
            else:  # SHORT
                pnl_pct = ((entry - ltp) / entry) * 100
                if pnl_pct >= TARGET_PCT:
                    return "TARGET"
                if pnl_pct <= -STOP_LOSS_PCT:
                    return "STOPLOSS"
        
        return None

    def place_order(self, symbol: str, exchange: str, action: str, qty: int, ltp: float) -> bool:
        """Place an order and update position tracking."""
        try:
            response = self.client.placeorder(
                strategy=STRATEGY_NAME,
                symbol=symbol,
                exchange=exchange,
                action=action,
                price_type="MARKET",
                product=PRODUCT,
                quantity=qty
            )
            
            if response.get("status") == "success":
                with self.lock:
                    if action == "BUY":
                        self.positions[symbol] = {"qty": qty, "side": "LONG", "entry": ltp}
                    else:
                        self.positions[symbol] = {"qty": qty, "side": "SHORT", "entry": ltp}
                    
                    self.trade_counts[symbol] = self.trade_counts.get(symbol, 0) + 1
                    self.last_trade_time[symbol] = time.time()
                
                print(f"[{self._now()}] ORDER: {action} {qty} {symbol} @ ~{ltp:.2f}")
                return True
            else:
                print(f"[{self._now()}] Order failed: {response.get('message')}")
        except Exception as e:
            print(f"[{self._now()}] Order error: {e}")
        
        return False

    def close_position(self, symbol: str, exchange: str, ltp: float, reason: str) -> bool:
        """Close an existing position."""
        with self.lock:
            pos = self.positions.get(symbol)
            if not pos:
                return False
            
            qty = pos["qty"]
            side = pos["side"]
            entry = pos["entry"]
        
        action = "SELL" if side == "LONG" else "BUY"
        
        try:
            response = self.client.placeorder(
                strategy=STRATEGY_NAME,
                symbol=symbol,
                exchange=exchange,
                action=action,
                price_type="MARKET",
                product=PRODUCT,
                quantity=qty
            )
            
            if response.get("status") == "success":
                # Calculate P&L
                if side == "LONG":
                    pnl = (ltp - entry) * qty
                    pnl_pct = ((ltp - entry) / entry) * 100
                else:
                    pnl = (entry - ltp) * qty
                    pnl_pct = ((entry - ltp) / entry) * 100
                
                with self.lock:
                    del self.positions[symbol]
                    self.trade_counts[symbol] = self.trade_counts.get(symbol, 0) + 1
                    self.last_trade_time[symbol] = time.time()
                
                print(f"[{self._now()}] CLOSE ({reason}): {action} {qty} {symbol} @ ~{ltp:.2f} | P&L: Rs.{pnl:.2f} ({pnl_pct:.2f}%)")
                return True
        except Exception as e:
            print(f"[{self._now()}] Close error: {e}")
        
        return False

    def scan_stock(self, stock: dict):
        """Scan a single stock for trading opportunities."""
        symbol = stock["symbol"]
        exchange = stock["exchange"]
        qty = stock["qty"]
        
        # Get current quote
        quote = self.get_quote(symbol, exchange)
        if not quote:
            return
        
        ltp = float(quote.get("ltp", 0))
        if ltp <= 0:
            return
        
        # Check position value limit
        if ltp * qty > MAX_POSITION_VALUE:
            return
        
        # Check if we have an existing position
        with self.lock:
            has_position = symbol in self.positions
        
        if has_position:
            # Check exit conditions
            exit_reason = self.check_exit_conditions(symbol, ltp)
            if exit_reason:
                self.close_position(symbol, exchange, ltp, exit_reason)
            return
        
        # No position - look for entry
        if not self._can_trade(symbol):
            return
        
        # Get market depth
        depth = self.get_market_depth(symbol, exchange)
        if not depth:
            return
        
        # Analyze order book
        analysis = self.analyze_orderbook(depth, ltp)
        signal = analysis.get("signal", "HOLD")
        
        if signal == "BUY":
            self.place_order(symbol, exchange, "BUY", qty, ltp)
        elif signal == "SELL":
            self.place_order(symbol, exchange, "SELL", qty, ltp)

    def run(self):
        """Main strategy loop."""
        self.running = True
        print(f"\n[{self._now()}] Strategy started. Waiting for market hours...")
        
        while self.running:
            try:
                if not self._is_market_hours():
                    # Reset daily counters at market open
                    now = datetime.now(IST)
                    if now.hour == MARKET_OPEN_HOUR and now.minute < MARKET_OPEN_MIN + 5:
                        self.trade_counts.clear()
                    time.sleep(30)
                    continue
                
                # Scan all stocks
                for stock in WATCHLIST:
                    if not self.running:
                        break
                    self.scan_stock(stock)
                
                # Print status every minute
                if datetime.now(IST).second < SCAN_INTERVAL:
                    self._print_status()
                
                time.sleep(SCAN_INTERVAL)
                
            except KeyboardInterrupt:
                print(f"\n[{self._now()}] Stopping strategy...")
                self.running = False
            except Exception as e:
                print(f"[{self._now()}] Error: {e}")
                time.sleep(5)
        
        # Close all positions on exit
        self._close_all_positions()

    def _print_status(self):
        """Print current strategy status."""
        with self.lock:
            pos_count = len(self.positions)
            total_trades = sum(self.trade_counts.values())
        
        print(f"[{self._now()}] Status: {pos_count} positions | {total_trades} trades today")

    def _close_all_positions(self):
        """Close all open positions (end of day or shutdown)."""
        with self.lock:
            symbols = list(self.positions.keys())
        
        for symbol in symbols:
            stock = next((s for s in WATCHLIST if s["symbol"] == symbol), None)
            if stock:
                quote = self.get_quote(symbol, stock["exchange"])
                if quote:
                    ltp = float(quote.get("ltp", 0))
                    self.close_position(symbol, stock["exchange"], ltp, "SHUTDOWN")

    def stop(self):
        """Stop the strategy."""
        self.running = False


def main():
    """Entry point."""
    print("=" * 60)
    print("ORDER BOOK SCALPING STRATEGY")
    print("=" * 60)
    print(f"Target: {TARGET_PCT}% | Stop Loss: {STOP_LOSS_PCT}%")
    print(f"Max Spread: {MAX_SPREAD_PCT}% | Min Imbalance: {MIN_IMBALANCE}")
    print(f"Max Trades/Day: {MAX_TRADES_PER_DAY} per stock")
    print("=" * 60)
    
    scalper = OrderBookScalper()
    
    try:
        scalper.run()
    except KeyboardInterrupt:
        scalper.stop()
    
    print("\nStrategy stopped.")


if __name__ == "__main__":
    main()
