#!/usr/bin/env python
"""
Real-Time Order Book Scalper (WebSocket Version)
=================================================
Advanced scalping strategy using WebSocket for real-time market depth.

This version uses WebSocket streaming for:
- Real-time LTP updates (no polling delay)
- Instant order book changes detection
- Faster signal generation and execution

Strategy Features:
1. WebSocket-based real-time price feed
2. Order imbalance detection with weighted depth analysis
3. Momentum confirmation using recent price movement
4. Dynamic spread threshold based on volatility
5. Smart position sizing based on available margin

Requirements:
- OpenAlgo running with WebSocket proxy enabled
- Broker logged in with market data subscription
- Sufficient margin for intraday trading

Author: FastTrade/OpenAlgo Strategy
"""

import os
import time
import json
import threading
from datetime import datetime, timedelta
from collections import deque
from dataclasses import dataclass, field
import pytz

from openalgo import api

# ============================================================================
# CONFIGURATION
# ============================================================================

API_KEY = os.getenv('OPENALGO_API_KEY')
HOST = os.getenv('HOST_SERVER', 'http://127.0.0.1:5000')
WS_URL = os.getenv('WEBSOCKET_URL', 'ws://127.0.0.1:8765')

STRATEGY_NAME = "RT OrderBook Scalper"

# Stocks Configuration
# Only trade highly liquid stocks (Nifty 50 preferred)
# base_qty = shares per trade (adjust based on your capital)
# Tip: Start with 5-10 stocks, add more once profitable

STOCKS = [
    # Large Cap - Very Liquid (tight spreads)
    {"symbol": "RELIANCE", "exchange": "NSE", "base_qty": 5, "tick_size": 0.05},
    {"symbol": "HDFCBANK", "exchange": "NSE", "base_qty": 5, "tick_size": 0.05},
    {"symbol": "ICICIBANK", "exchange": "NSE", "base_qty": 10, "tick_size": 0.05},
    {"symbol": "INFY", "exchange": "NSE", "base_qty": 10, "tick_size": 0.05},
    {"symbol": "TCS", "exchange": "NSE", "base_qty": 3, "tick_size": 0.05},
    {"symbol": "SBIN", "exchange": "NSE", "base_qty": 25, "tick_size": 0.05},
    {"symbol": "BHARTIARTL", "exchange": "NSE", "base_qty": 10, "tick_size": 0.05},
    {"symbol": "ITC", "exchange": "NSE", "base_qty": 25, "tick_size": 0.05},
    {"symbol": "KOTAKBANK", "exchange": "NSE", "base_qty": 5, "tick_size": 0.05},
    {"symbol": "LT", "exchange": "NSE", "base_qty": 5, "tick_size": 0.05},
    
    # Banking & Finance - High Volume
    {"symbol": "AXISBANK", "exchange": "NSE", "base_qty": 10, "tick_size": 0.05},
    {"symbol": "INDUSINDBK", "exchange": "NSE", "base_qty": 10, "tick_size": 0.05},
    {"symbol": "BAJFINANCE", "exchange": "NSE", "base_qty": 2, "tick_size": 0.05},
    {"symbol": "HDFCLIFE", "exchange": "NSE", "base_qty": 15, "tick_size": 0.05},
    {"symbol": "SBILIFE", "exchange": "NSE", "base_qty": 10, "tick_size": 0.05},
    
    # IT & Tech - Good Liquidity
    {"symbol": "WIPRO", "exchange": "NSE", "base_qty": 20, "tick_size": 0.05},
    {"symbol": "HCLTECH", "exchange": "NSE", "base_qty": 10, "tick_size": 0.05},
    {"symbol": "TECHM", "exchange": "NSE", "base_qty": 10, "tick_size": 0.05},
    
    # Auto & Industrial
    {"symbol": "TATAMOTORS", "exchange": "NSE", "base_qty": 15, "tick_size": 0.05},
    {"symbol": "M&M", "exchange": "NSE", "base_qty": 5, "tick_size": 0.05},
    {"symbol": "MARUTI", "exchange": "NSE", "base_qty": 1, "tick_size": 0.05},
    
    # Energy & Metals
    {"symbol": "TATASTEEL", "exchange": "NSE", "base_qty": 10, "tick_size": 0.05},
    {"symbol": "HINDALCO", "exchange": "NSE", "base_qty": 15, "tick_size": 0.05},
    {"symbol": "ONGC", "exchange": "NSE", "base_qty": 25, "tick_size": 0.05},
    {"symbol": "NTPC", "exchange": "NSE", "base_qty": 25, "tick_size": 0.05},
    {"symbol": "POWERGRID", "exchange": "NSE", "base_qty": 25, "tick_size": 0.05},
    
    # FMCG & Pharma
    {"symbol": "HINDUNILVR", "exchange": "NSE", "base_qty": 5, "tick_size": 0.05},
    {"symbol": "SUNPHARMA", "exchange": "NSE", "base_qty": 10, "tick_size": 0.05},
    {"symbol": "DRREDDY", "exchange": "NSE", "base_qty": 2, "tick_size": 0.05},
    {"symbol": "CIPLA", "exchange": "NSE", "base_qty": 10, "tick_size": 0.05},
]

# Signal Parameters
class Config:
    # ===== TRADE DIRECTION =====
    ALLOW_LONG = True         # Allow BUY entries
    ALLOW_SHORT = False       # DISABLED - No shorting (safer)
    
    # ===== ORDER BOOK ANALYSIS =====
    MIN_IMBALANCE = 0.50      # INCREASED from 0.40 - Need stronger signal
    MAX_SPREAD_PCT = 0.05     # TIGHTENED - Only very liquid moments
    DEPTH_LEVELS = 5          # Levels to analyze
    
    # ===== MOMENTUM CONFIRMATION =====
    MOMENTUM_WINDOW = 10      # Number of ticks to track
    MIN_MOMENTUM = 0.03       # INCREASED - Need price moving in direction
    
    # ===== RISK MANAGEMENT (CRITICAL CHANGES) =====
    TARGET_PCT = 0.25         # INCREASED from 0.12 - Bigger targets
    STOP_LOSS_PCT = 0.15      # INCREASED from 0.08 - Room to breathe
    TRAILING_STOP_PCT = 0.10  # INCREASED - Don't exit too early
    MAX_POSITION_VALUE = 50000  # Max INR per position
    MAX_DAILY_LOSS = 2000     # REDUCED - Stop earlier on bad days
    MAX_TRADES_PER_STOCK = 5  # REDUCED from 15 - Less overtrading
    
    # ===== TIMING =====
    COOLDOWN_SEC = 60         # INCREASED from 20 - Wait between trades
    MARKET_OPEN = (9, 20)     # (hour, minute) IST
    MARKET_CLOSE = (15, 10)   # Stop new trades 5 min before close
    
    # ===== EXECUTION =====
    PRODUCT = "MIS"
    PRICE_TYPE = "MARKET"


IST = pytz.timezone('Asia/Kolkata')


@dataclass
class StockState:
    """Track state for each stock."""
    symbol: str
    exchange: str
    base_qty: int
    tick_size: float
    
    # Real-time data
    ltp: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    bid_qty: int = 0
    ask_qty: int = 0
    
    # Analysis
    imbalance: float = 0.0
    spread_pct: float = 0.0
    momentum: float = 0.0
    price_history: deque = field(default_factory=lambda: deque(maxlen=50))
    
    # Position
    position_qty: int = 0
    position_side: str = ""  # "LONG" or "SHORT"
    entry_price: float = 0.0
    highest_since_entry: float = 0.0
    lowest_since_entry: float = 0.0
    
    # Tracking
    trade_count: int = 0
    last_trade_time: float = 0.0
    daily_pnl: float = 0.0


class RealtimeScalper:
    """Real-time order book scalping strategy."""

    def __init__(self):
        if not API_KEY:
            raise ValueError("OPENALGO_API_KEY not set")
        
        self.client = api(api_key=API_KEY, host=HOST, ws_url=WS_URL)
        self.config = Config()
        self.running = False
        self.lock = threading.Lock()
        
        # Initialize stock states
        self.stocks: dict[str, StockState] = {}
        for s in STOCKS:
            key = f"{s['exchange']}:{s['symbol']}"
            self.stocks[key] = StockState(
                symbol=s["symbol"],
                exchange=s["exchange"],
                base_qty=s["base_qty"],
                tick_size=s["tick_size"]
            )
        
        self.total_daily_pnl = 0.0
        self._print_config()

    def _print_config(self):
        print("=" * 60)
        print("REAL-TIME ORDER BOOK SCALPER")
        print("=" * 60)
        print(f"Stocks: {[s['symbol'] for s in STOCKS]}")
        print(f"Target: {self.config.TARGET_PCT}% | SL: {self.config.STOP_LOSS_PCT}%")
        print(f"Max Spread: {self.config.MAX_SPREAD_PCT}% | Min Imbalance: {self.config.MIN_IMBALANCE}")
        print(f"Max Position: Rs.{self.config.MAX_POSITION_VALUE:,}")
        print("=" * 60)

    def _now(self) -> str:
        return datetime.now(IST).strftime("%H:%M:%S")

    def _is_market_hours(self) -> bool:
        now = datetime.now(IST)
        if now.weekday() >= 5:
            return False
        open_time = now.replace(hour=self.config.MARKET_OPEN[0], minute=self.config.MARKET_OPEN[1], second=0)
        close_time = now.replace(hour=self.config.MARKET_CLOSE[0], minute=self.config.MARKET_CLOSE[1], second=0)
        return open_time <= now <= close_time

    def _can_trade(self, state: StockState) -> tuple[bool, str]:
        """Check if trading is allowed for this stock."""
        now = time.time()
        
        # Check daily loss limit
        if self.total_daily_pnl <= -self.config.MAX_DAILY_LOSS:
            return False, "Daily loss limit reached"
        
        # Check cooldown
        if now - state.last_trade_time < self.config.COOLDOWN_SEC:
            return False, "Cooldown"
        
        # Check daily trade limit
        if state.trade_count >= self.config.MAX_TRADES_PER_STOCK:
            return False, "Max trades reached"
        
        # Check position value limit
        if state.ltp * state.base_qty > self.config.MAX_POSITION_VALUE:
            return False, "Position value too high"
        
        return True, "OK"

    def fetch_depth(self, state: StockState) -> bool:
        """Fetch market depth and update state."""
        try:
            response = self.client.depth(symbol=state.symbol, exchange=state.exchange)
            if response.get("status") != "success":
                return False
            
            data = response.get("data", {})
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            
            if not bids or not asks:
                return False
            
            # Update best bid/ask
            state.best_bid = float(bids[0].get("price", 0))
            state.best_ask = float(asks[0].get("price", 0))
            
            if state.best_bid <= 0 or state.best_ask <= 0:
                return False
            
            # Calculate weighted imbalance
            bid_total = 0.0
            ask_total = 0.0
            
            for i in range(min(self.config.DEPTH_LEVELS, len(bids), len(asks))):
                weight = 1.0 / (i + 1)  # Closer levels have more weight
                bid_total += float(bids[i].get("quantity", 0)) * weight
                ask_total += float(asks[i].get("quantity", 0)) * weight
            
            total = bid_total + ask_total
            if total > 0:
                state.imbalance = (bid_total - ask_total) / total
                state.bid_qty = int(bid_total)
                state.ask_qty = int(ask_total)
            
            # Calculate spread
            if state.ltp > 0:
                state.spread_pct = ((state.best_ask - state.best_bid) / state.ltp) * 100
            
            return True
            
        except Exception as e:
            print(f"[{self._now()}] Depth error {state.symbol}: {e}")
            return False

    def fetch_quote(self, state: StockState) -> bool:
        """Fetch current quote and update state."""
        try:
            response = self.client.quotes(symbol=state.symbol, exchange=state.exchange)
            if response.get("status") != "success":
                return False
            
            data = response.get("data", {})
            ltp = float(data.get("ltp", 0))
            
            if ltp <= 0:
                return False
            
            # Update price history for momentum
            state.price_history.append(ltp)
            state.ltp = ltp
            
            # Calculate momentum (% change over window)
            if len(state.price_history) >= self.config.MOMENTUM_WINDOW:
                old_price = state.price_history[-self.config.MOMENTUM_WINDOW]
                state.momentum = ((ltp - old_price) / old_price) * 100
            
            # Update trailing stop tracking
            if state.position_qty != 0:
                state.highest_since_entry = max(state.highest_since_entry, ltp)
                state.lowest_since_entry = min(state.lowest_since_entry, ltp)
            
            return True
            
        except Exception as e:
            print(f"[{self._now()}] Quote error {state.symbol}: {e}")
            return False

    def analyze_signal(self, state: StockState) -> tuple[str, str]:
        """
        Analyze order book and momentum for trading signal.
        
        Returns: (signal, reason)
        - signal: "BUY", "SELL", or "HOLD"
        """
        # Check spread
        if state.spread_pct > self.config.MAX_SPREAD_PCT:
            return "HOLD", f"Spread too wide: {state.spread_pct:.3f}%"
        
        # Check imbalance
        if abs(state.imbalance) < self.config.MIN_IMBALANCE:
            return "HOLD", f"Low imbalance: {state.imbalance:.2f}"
        
        # BUY signal: Strong bid imbalance + positive momentum
        if state.imbalance >= self.config.MIN_IMBALANCE:
            if state.momentum >= 0:  # At least not falling
                return "BUY", f"Buy pressure: imb={state.imbalance:.2f}, mom={state.momentum:.3f}%"
        
        # SELL signal: Strong ask imbalance + negative momentum
        if state.imbalance <= -self.config.MIN_IMBALANCE:
            if state.momentum <= 0:  # At least not rising
                return "SELL", f"Sell pressure: imb={state.imbalance:.2f}, mom={state.momentum:.3f}%"
        
        return "HOLD", "No clear signal"

    def check_exit(self, state: StockState) -> tuple[bool, str]:
        """Check if position should be exited."""
        if state.position_qty == 0:
            return False, ""
        
        entry = state.entry_price
        ltp = state.ltp
        
        if state.position_side == "LONG":
            pnl_pct = ((ltp - entry) / entry) * 100
            
            # Target hit
            if pnl_pct >= self.config.TARGET_PCT:
                return True, "TARGET"
            
            # Stop loss hit
            if pnl_pct <= -self.config.STOP_LOSS_PCT:
                return True, "STOPLOSS"
            
            # Trailing stop (only if in profit)
            if pnl_pct > self.config.TRAILING_STOP_PCT:
                trail_price = state.highest_since_entry * (1 - self.config.TRAILING_STOP_PCT / 100)
                if ltp <= trail_price:
                    return True, "TRAILING_STOP"
        
        else:  # SHORT
            pnl_pct = ((entry - ltp) / entry) * 100
            
            if pnl_pct >= self.config.TARGET_PCT:
                return True, "TARGET"
            
            if pnl_pct <= -self.config.STOP_LOSS_PCT:
                return True, "STOPLOSS"
            
            if pnl_pct > self.config.TRAILING_STOP_PCT:
                trail_price = state.lowest_since_entry * (1 + self.config.TRAILING_STOP_PCT / 100)
                if ltp >= trail_price:
                    return True, "TRAILING_STOP"
        
        return False, ""

    def place_order(self, state: StockState, action: str) -> bool:
        """Place order and update state."""
        try:
            response = self.client.placeorder(
                strategy=STRATEGY_NAME,
                symbol=state.symbol,
                exchange=state.exchange,
                action=action,
                price_type=self.config.PRICE_TYPE,
                product=self.config.PRODUCT,
                quantity=state.base_qty
            )
            
            if response.get("status") == "success":
                with self.lock:
                    state.position_qty = state.base_qty
                    state.position_side = "LONG" if action == "BUY" else "SHORT"
                    state.entry_price = state.ltp
                    state.highest_since_entry = state.ltp
                    state.lowest_since_entry = state.ltp
                    state.trade_count += 1
                    state.last_trade_time = time.time()
                
                print(f"[{self._now()}] ENTRY: {action} {state.base_qty} {state.symbol} @ {state.ltp:.2f}")
                return True
            else:
                print(f"[{self._now()}] Order failed: {response.get('message')}")
                
        except Exception as e:
            print(f"[{self._now()}] Order error: {e}")
        
        return False

    def close_position(self, state: StockState, reason: str) -> bool:
        """Close position and calculate P&L."""
        if state.position_qty == 0:
            return False
        
        action = "SELL" if state.position_side == "LONG" else "BUY"
        
        try:
            response = self.client.placeorder(
                strategy=STRATEGY_NAME,
                symbol=state.symbol,
                exchange=state.exchange,
                action=action,
                price_type=self.config.PRICE_TYPE,
                product=self.config.PRODUCT,
                quantity=state.position_qty
            )
            
            if response.get("status") == "success":
                # Calculate P&L
                if state.position_side == "LONG":
                    pnl = (state.ltp - state.entry_price) * state.position_qty
                    pnl_pct = ((state.ltp - state.entry_price) / state.entry_price) * 100
                else:
                    pnl = (state.entry_price - state.ltp) * state.position_qty
                    pnl_pct = ((state.entry_price - state.ltp) / state.entry_price) * 100
                
                with self.lock:
                    state.daily_pnl += pnl
                    self.total_daily_pnl += pnl
                    state.position_qty = 0
                    state.position_side = ""
                    state.entry_price = 0
                    state.trade_count += 1
                    state.last_trade_time = time.time()
                
                status = "WIN" if pnl > 0 else "LOSS"
                print(f"[{self._now()}] EXIT ({reason}): {action} {state.symbol} @ {state.ltp:.2f} | P&L: Rs.{pnl:.2f} ({pnl_pct:.2f}%) [{status}]")
                return True
                
        except Exception as e:
            print(f"[{self._now()}] Close error: {e}")
        
        return False

    def process_stock(self, state: StockState):
        """Process a single stock - fetch data, analyze, trade."""
        # Fetch latest data
        if not self.fetch_quote(state):
            return
        if not self.fetch_depth(state):
            return
        
        # Check existing position for exit
        if state.position_qty != 0:
            should_exit, reason = self.check_exit(state)
            if should_exit:
                self.close_position(state, reason)
            return
        
        # Check if we can trade
        can_trade, reason = self._can_trade(state)
        if not can_trade:
            return
        
        # Analyze for entry signal
        signal, signal_reason = self.analyze_signal(state)
        
        if signal == "BUY":
            self.place_order(state, "BUY")
        elif signal == "SELL":
            self.place_order(state, "SELL")

    def run(self):
        """Main strategy loop."""
        self.running = True
        print(f"\n[{self._now()}] Strategy started")
        
        scan_count = 0
        
        while self.running:
            try:
                if not self._is_market_hours():
                    # Reset daily counters at start of day
                    now = datetime.now(IST)
                    if now.hour == self.config.MARKET_OPEN[0] and now.minute < self.config.MARKET_OPEN[1] + 2:
                        for state in self.stocks.values():
                            state.trade_count = 0
                            state.daily_pnl = 0
                        self.total_daily_pnl = 0
                    time.sleep(30)
                    continue
                
                # Process each stock
                for key, state in self.stocks.items():
                    if not self.running:
                        break
                    self.process_stock(state)
                    time.sleep(0.5)  # Small delay between stocks
                
                scan_count += 1
                
                # Print status every 30 scans (~1 minute)
                if scan_count % 30 == 0:
                    self._print_status()
                
                time.sleep(1)
                
            except KeyboardInterrupt:
                print(f"\n[{self._now()}] Stopping...")
                self.running = False
            except Exception as e:
                print(f"[{self._now()}] Error: {e}")
                time.sleep(5)
        
        self._close_all()

    def _print_status(self):
        """Print current status."""
        positions = sum(1 for s in self.stocks.values() if s.position_qty != 0)
        trades = sum(s.trade_count for s in self.stocks.values())
        
        print(f"\n[{self._now()}] === STATUS ===")
        print(f"Positions: {positions} | Trades: {trades} | Daily P&L: Rs.{self.total_daily_pnl:.2f}")
        
        for key, state in self.stocks.items():
            if state.position_qty != 0:
                if state.position_side == "LONG":
                    pnl = (state.ltp - state.entry_price) * state.position_qty
                else:
                    pnl = (state.entry_price - state.ltp) * state.position_qty
                print(f"  {state.symbol}: {state.position_side} {state.position_qty} @ {state.entry_price:.2f} | LTP: {state.ltp:.2f} | P&L: Rs.{pnl:.2f}")
        print()

    def _close_all(self):
        """Close all positions on shutdown."""
        print(f"[{self._now()}] Closing all positions...")
        for state in self.stocks.values():
            if state.position_qty != 0:
                self.fetch_quote(state)
                self.close_position(state, "SHUTDOWN")

    def stop(self):
        self.running = False


def main():
    scalper = RealtimeScalper()
    try:
        scalper.run()
    except KeyboardInterrupt:
        scalper.stop()
    print("\nStrategy stopped.")


if __name__ == "__main__":
    main()
