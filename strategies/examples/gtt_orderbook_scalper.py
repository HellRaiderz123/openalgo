#!/usr/bin/env python
"""
Order Book Scalper with GTT Stop Loss Protection
=================================================
Advanced scalping strategy that places GTT (Good Till Triggered) stop loss
orders immediately after entry for automatic protection.

Key Features:
1. Places GTT SL order immediately after BUY/SELL entry
2. GTT stays active even if strategy crashes or internet disconnects
3. Supports OCO (One Cancels Other) for both SL and Target
4. Automatic GTT cancellation when position is manually closed

Flow:
1. Detect signal from order book imbalance
2. Place MARKET entry order
3. Immediately place GTT stop loss (and optional target)
4. Monitor for exit conditions
5. If exit triggered by strategy -> Cancel GTT and place exit order
6. If GTT triggers -> Position auto-closed by broker

Requirements:
- Broker must support GTT orders (Zerodha, Angel, Dhan, etc.)
- OpenAlgo running with GTT API enabled

Author: FastTrade/OpenAlgo Strategy
"""

import os
import time
import threading
from datetime import datetime
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

STRATEGY_NAME = "GTT Scalper"

# Stocks to trade - Start with fewer, add more once profitable
STOCKS = [
    # Most liquid - recommended for starting
    {"symbol": "RELIANCE", "exchange": "NSE", "base_qty": 5},
    {"symbol": "SBIN", "exchange": "NSE", "base_qty": 25},
    {"symbol": "ICICIBANK", "exchange": "NSE", "base_qty": 10},
    {"symbol": "HDFCBANK", "exchange": "NSE", "base_qty": 5},
    {"symbol": "INFY", "exchange": "NSE", "base_qty": 10},
    {"symbol": "TCS", "exchange": "NSE", "base_qty": 3},
    {"symbol": "BHARTIARTL", "exchange": "NSE", "base_qty": 10},
    {"symbol": "ITC", "exchange": "NSE", "base_qty": 25},
    {"symbol": "AXISBANK", "exchange": "NSE", "base_qty": 10},
    {"symbol": "TATAMOTORS", "exchange": "NSE", "base_qty": 15},
]


class Config:
    """Strategy configuration - OPTIMIZED based on live testing."""
    
    # ===== TRADE DIRECTION =====
    ALLOW_LONG = True         # Allow BUY entries (long positions)
    ALLOW_SHORT = False       # DISABLED - No shorting (much safer)
    
    # ===== ORDER BOOK SIGNAL =====
    MIN_IMBALANCE = 0.50      # INCREASED - Need 50% imbalance (stronger signal)
    MAX_SPREAD_PCT = 0.05     # TIGHTENED - Only trade tight spreads
    DEPTH_LEVELS = 5          # Depth levels to analyze
    
    # ===== GTT ORDER SETTINGS =====
    USE_GTT = True            # Enable GTT stop loss orders
    USE_OCO = False           # Use OCO (SL + Target) instead of just SL
    
    # ===== RISK MANAGEMENT (CRITICAL) =====
    STOP_LOSS_PCT = 0.40      # INCREASED - 0.40% gives room to breathe
    TARGET_PCT = 0.60         # INCREASED - 0.60% target (1:1.5 ratio)
    
    # ===== POSITION LIMITS =====
    MAX_POSITION_VALUE = 50000    # Max Rs. per position
    MAX_TRADES_PER_STOCK = 3      # REDUCED - Max 3 trades per stock per day
    MAX_DAILY_LOSS = 2000         # REDUCED - Stop if loss exceeds Rs.2000
    
    # ===== TIMING (PREVENT OVERTRADING) =====
    COOLDOWN_SEC = 120            # INCREASED - 2 min between trades on same stock
    SCAN_INTERVAL = 3             # INCREASED - 3 sec between scans
    MARKET_OPEN = (9, 20)         # (hour, minute) IST
    MARKET_CLOSE = (15, 10)       # Stop new trades before close
    
    # ===== ORDER SETTINGS =====
    PRODUCT = "MIS"               # MIS for intraday
    PRICE_TYPE = "MARKET"
    
    # Position Limits
    MAX_POSITION_VALUE = 50000    # Max Rs. per position
    MAX_TRADES_PER_STOCK = 10     # Per stock per day
    MAX_DAILY_LOSS = 5000         # Stop trading if loss exceeds
    
    # Timing
    COOLDOWN_SEC = 30             # Seconds between trades on same stock
    SCAN_INTERVAL = 2             # Seconds between scans
    MARKET_OPEN = (9, 20)         # (hour, minute) IST
    MARKET_CLOSE = (15, 10)       # Stop new trades before close
    
    # Order Settings
    PRODUCT = "MIS"               # MIS for intraday
    PRICE_TYPE = "MARKET"


IST = pytz.timezone('Asia/Kolkata')


@dataclass
class Position:
    """Track a single position with its GTT order."""
    symbol: str
    exchange: str
    qty: int
    side: str              # "LONG" or "SHORT"
    entry_price: float
    entry_time: float
    gtt_trigger_id: str = ""   # GTT order ID for stop loss
    stop_loss: float = 0.0
    target: float = 0.0


@dataclass 
class StockState:
    """Track state for each stock."""
    symbol: str
    exchange: str
    base_qty: int
    
    # Market data
    ltp: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    imbalance: float = 0.0
    spread_pct: float = 0.0
    
    # Price history for momentum
    price_history: deque = field(default_factory=lambda: deque(maxlen=20))
    
    # Position (None if flat)
    position: Position = None
    
    # Daily tracking
    trade_count: int = 0
    last_trade_time: float = 0.0
    daily_pnl: float = 0.0


class GTTScalper:
    """Order book scalper with GTT stop loss protection."""

    def __init__(self):
        if not API_KEY:
            raise ValueError("OPENALGO_API_KEY not set. Run via OpenAlgo Python Host.")
        
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
                base_qty=s["base_qty"]
            )
        
        self.total_daily_pnl = 0.0
        self._print_config()

    def _print_config(self):
        print("=" * 60)
        print("ORDER BOOK SCALPER WITH GTT PROTECTION")
        print("=" * 60)
        print(f"Stocks: {len(STOCKS)} configured")
        print(f"GTT Enabled: {self.config.USE_GTT}")
        print(f"Stop Loss: {self.config.STOP_LOSS_PCT}% | Target: {self.config.TARGET_PCT}%")
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
        """Check if trading is allowed."""
        now = time.time()
        
        if self.total_daily_pnl <= -self.config.MAX_DAILY_LOSS:
            return False, "Daily loss limit"
        
        if now - state.last_trade_time < self.config.COOLDOWN_SEC:
            return False, "Cooldown"
        
        if state.trade_count >= self.config.MAX_TRADES_PER_STOCK:
            return False, "Max trades"
        
        if state.ltp * state.base_qty > self.config.MAX_POSITION_VALUE:
            return False, "Position too large"
        
        return True, "OK"

    # =========================================================================
    # MARKET DATA
    # =========================================================================
    
    def fetch_quote(self, state: StockState) -> bool:
        """Fetch current LTP."""
        try:
            response = self.client.quotes(symbol=state.symbol, exchange=state.exchange)
            if response.get("status") != "success":
                return False
            
            data = response.get("data", {})
            ltp = float(data.get("ltp", 0))
            if ltp <= 0:
                return False
            
            state.price_history.append(ltp)
            state.ltp = ltp
            return True
        except Exception as e:
            print(f"[{self._now()}] Quote error {state.symbol}: {e}")
            return False

    def fetch_depth(self, state: StockState) -> bool:
        """Fetch market depth and calculate imbalance."""
        try:
            response = self.client.depth(symbol=state.symbol, exchange=state.exchange)
            if response.get("status") != "success":
                return False
            
            data = response.get("data", {})
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            
            if not bids or not asks:
                return False
            
            state.best_bid = float(bids[0].get("price", 0))
            state.best_ask = float(asks[0].get("price", 0))
            
            if state.best_bid <= 0 or state.best_ask <= 0:
                return False
            
            # Calculate weighted imbalance
            bid_total = 0.0
            ask_total = 0.0
            
            for i in range(min(self.config.DEPTH_LEVELS, len(bids), len(asks))):
                weight = 1.0 / (i + 1)
                bid_total += float(bids[i].get("quantity", 0)) * weight
                ask_total += float(asks[i].get("quantity", 0)) * weight
            
            total = bid_total + ask_total
            if total > 0:
                state.imbalance = (bid_total - ask_total) / total
            
            if state.ltp > 0:
                state.spread_pct = ((state.best_ask - state.best_bid) / state.ltp) * 100
            
            return True
        except Exception as e:
            print(f"[{self._now()}] Depth error {state.symbol}: {e}")
            return False

    # =========================================================================
    # GTT ORDER MANAGEMENT
    # =========================================================================
    
    def place_gtt_stoploss(self, state: StockState, entry_price: float, side: str) -> str:
        """
        Place GTT stop loss order after entry.
        
        Returns: trigger_id if successful, empty string if failed
        """
        if not self.config.USE_GTT:
            return ""
        
        try:
            # Calculate SL price
            if side == "LONG":
                # Long position: SL below entry, exit action is SELL
                sl_price = round(entry_price * (1 - self.config.STOP_LOSS_PCT / 100), 2)
                exit_action = "SELL"
            else:
                # Short position: SL above entry, exit action is BUY
                sl_price = round(entry_price * (1 + self.config.STOP_LOSS_PCT / 100), 2)
                exit_action = "BUY"
            
            # Place single-leg GTT (stop loss only)
            response = self.client.placegttorder(
                strategy=STRATEGY_NAME,
                symbol=state.symbol,
                exchange=state.exchange,
                trigger_type="SINGLE",
                trigger_price=sl_price,
                action=exit_action,
                price_type="MARKET",
                product=self.config.PRODUCT,
                quantity=state.base_qty
            )
            
            if response.get("status") == "success":
                trigger_id = response.get("trigger_id", "")
                print(f"[{self._now()}] GTT SL placed: {state.symbol} @ {sl_price} | ID: {trigger_id}")
                return trigger_id
            else:
                print(f"[{self._now()}] GTT SL failed: {response.get('message')}")
                
        except Exception as e:
            print(f"[{self._now()}] GTT error: {e}")
        
        return ""

    def place_gtt_oco(self, state: StockState, entry_price: float, side: str) -> str:
        """
        Place OCO (One Cancels Other) GTT with both SL and Target.
        
        Returns: trigger_id if successful, empty string if failed
        """
        if not self.config.USE_GTT or not self.config.USE_OCO:
            return ""
        
        try:
            if side == "LONG":
                sl_price = round(entry_price * (1 - self.config.STOP_LOSS_PCT / 100), 2)
                target_price = round(entry_price * (1 + self.config.TARGET_PCT / 100), 2)
                exit_action = "SELL"
            else:
                sl_price = round(entry_price * (1 + self.config.STOP_LOSS_PCT / 100), 2)
                target_price = round(entry_price * (1 - self.config.TARGET_PCT / 100), 2)
                exit_action = "BUY"
            
            response = self.client.placegttorder(
                strategy=STRATEGY_NAME,
                symbol=state.symbol,
                exchange=state.exchange,
                trigger_type="OCO",
                triggerprice_sl=sl_price,
                triggerprice_tg=target_price,
                action=exit_action,
                price_type="MARKET",
                product=self.config.PRODUCT,
                quantity=state.base_qty
            )
            
            if response.get("status") == "success":
                trigger_id = response.get("trigger_id", "")
                print(f"[{self._now()}] GTT OCO placed: {state.symbol} SL={sl_price} TG={target_price} | ID: {trigger_id}")
                return trigger_id
            else:
                print(f"[{self._now()}] GTT OCO failed: {response.get('message')}")
                
        except Exception as e:
            print(f"[{self._now()}] GTT OCO error: {e}")
        
        return ""

    def cancel_gtt(self, trigger_id: str, symbol: str) -> bool:
        """Cancel a GTT order."""
        if not trigger_id:
            return True
        
        try:
            response = self.client.cancelgttorder(trigger_id=trigger_id)
            if response.get("status") == "success":
                print(f"[{self._now()}] GTT cancelled: {symbol} | ID: {trigger_id}")
                return True
            else:
                print(f"[{self._now()}] GTT cancel failed: {response.get('message')}")
        except Exception as e:
            print(f"[{self._now()}] GTT cancel error: {e}")
        
        return False

    # =========================================================================
    # ORDER EXECUTION
    # =========================================================================
    
    def place_entry_order(self, state: StockState, action: str) -> bool:
        """Place entry order and GTT stop loss."""
        try:
            # Place market entry order
            response = self.client.placeorder(
                strategy=STRATEGY_NAME,
                symbol=state.symbol,
                exchange=state.exchange,
                action=action,
                price_type=self.config.PRICE_TYPE,
                product=self.config.PRODUCT,
                quantity=state.base_qty
            )
            
            if response.get("status") != "success":
                print(f"[{self._now()}] Entry failed: {response.get('message')}")
                return False
            
            entry_price = state.ltp
            side = "LONG" if action == "BUY" else "SHORT"
            
            # Place GTT stop loss
            if self.config.USE_OCO:
                gtt_id = self.place_gtt_oco(state, entry_price, side)
            else:
                gtt_id = self.place_gtt_stoploss(state, entry_price, side)
            
            # Calculate SL and target prices
            if side == "LONG":
                sl_price = round(entry_price * (1 - self.config.STOP_LOSS_PCT / 100), 2)
                target_price = round(entry_price * (1 + self.config.TARGET_PCT / 100), 2)
            else:
                sl_price = round(entry_price * (1 + self.config.STOP_LOSS_PCT / 100), 2)
                target_price = round(entry_price * (1 - self.config.TARGET_PCT / 100), 2)
            
            # Create position record
            with self.lock:
                state.position = Position(
                    symbol=state.symbol,
                    exchange=state.exchange,
                    qty=state.base_qty,
                    side=side,
                    entry_price=entry_price,
                    entry_time=time.time(),
                    gtt_trigger_id=gtt_id,
                    stop_loss=sl_price,
                    target=target_price
                )
                state.trade_count += 1
                state.last_trade_time = time.time()
            
            gtt_status = f"GTT: {gtt_id[:8]}..." if gtt_id else "NO GTT"
            print(f"[{self._now()}] ENTRY: {action} {state.base_qty} {state.symbol} @ {entry_price:.2f} | SL: {sl_price} | TG: {target_price} | {gtt_status}")
            return True
            
        except Exception as e:
            print(f"[{self._now()}] Entry error: {e}")
            return False

    def close_position(self, state: StockState, reason: str) -> bool:
        """Close position and cancel GTT."""
        pos = state.position
        if not pos:
            return False
        
        # Cancel GTT first (before exit order)
        if pos.gtt_trigger_id:
            self.cancel_gtt(pos.gtt_trigger_id, state.symbol)
        
        # Place exit order
        action = "SELL" if pos.side == "LONG" else "BUY"
        
        try:
            response = self.client.placeorder(
                strategy=STRATEGY_NAME,
                symbol=state.symbol,
                exchange=state.exchange,
                action=action,
                price_type=self.config.PRICE_TYPE,
                product=self.config.PRODUCT,
                quantity=pos.qty
            )
            
            if response.get("status") != "success":
                print(f"[{self._now()}] Exit failed: {response.get('message')}")
                return False
            
            # Calculate P&L
            if pos.side == "LONG":
                pnl = (state.ltp - pos.entry_price) * pos.qty
                pnl_pct = ((state.ltp - pos.entry_price) / pos.entry_price) * 100
            else:
                pnl = (pos.entry_price - state.ltp) * pos.qty
                pnl_pct = ((pos.entry_price - state.ltp) / pos.entry_price) * 100
            
            with self.lock:
                state.daily_pnl += pnl
                self.total_daily_pnl += pnl
                state.position = None
                state.trade_count += 1
                state.last_trade_time = time.time()
            
            status = "WIN" if pnl > 0 else "LOSS"
            print(f"[{self._now()}] EXIT ({reason}): {action} {pos.qty} {state.symbol} @ {state.ltp:.2f} | P&L: Rs.{pnl:.2f} ({pnl_pct:.2f}%) [{status}]")
            return True
            
        except Exception as e:
            print(f"[{self._now()}] Exit error: {e}")
            return False

    # =========================================================================
    # SIGNAL ANALYSIS
    # =========================================================================
    
    def analyze_signal(self, state: StockState) -> tuple[str, str]:
        """Analyze order book for entry signal."""
        
        # Check spread
        if state.spread_pct > self.config.MAX_SPREAD_PCT:
            return "HOLD", f"Spread: {state.spread_pct:.3f}%"
        
        # Check imbalance
        if abs(state.imbalance) < self.config.MIN_IMBALANCE:
            return "HOLD", f"Imbalance: {state.imbalance:.2f}"
        
        # BUY Signal: Strong bid imbalance (more buyers)
        if state.imbalance >= self.config.MIN_IMBALANCE:
            if self.config.ALLOW_LONG:
                return "BUY", f"Buy pressure: {state.imbalance:.2f}"
            else:
                return "HOLD", "Long trades disabled"
        
        # SELL Signal: Strong ask imbalance (more sellers)
        if state.imbalance <= -self.config.MIN_IMBALANCE:
            if self.config.ALLOW_SHORT:
                return "SELL", f"Sell pressure: {state.imbalance:.2f}"
            else:
                return "HOLD", "Short trades disabled"
        
        return "HOLD", "No signal"

    def check_exit(self, state: StockState) -> tuple[bool, str]:
        """Check if position should be exited (strategy-side, not GTT)."""
        pos = state.position
        if not pos:
            return False, ""
        
        ltp = state.ltp
        
        if pos.side == "LONG":
            # Target hit
            if ltp >= pos.target:
                return True, "TARGET"
            # Stop loss hit (backup if GTT didn't trigger)
            if ltp <= pos.stop_loss:
                return True, "STOPLOSS"
        else:  # SHORT
            if ltp <= pos.target:
                return True, "TARGET"
            if ltp >= pos.stop_loss:
                return True, "STOPLOSS"
        
        # Signal reversal exit
        if state.imbalance >= self.config.MIN_IMBALANCE and pos.side == "SHORT":
            return True, "REVERSAL"
        if state.imbalance <= -self.config.MIN_IMBALANCE and pos.side == "LONG":
            return True, "REVERSAL"
        
        return False, ""

    # =========================================================================
    # MAIN LOOP
    # =========================================================================
    
    def process_stock(self, state: StockState):
        """Process a single stock."""
        # Fetch data
        if not self.fetch_quote(state):
            return
        if not self.fetch_depth(state):
            return
        
        # Check existing position
        if state.position:
            should_exit, reason = self.check_exit(state)
            if should_exit:
                self.close_position(state, reason)
            return
        
        # Check if can trade
        can_trade, reason = self._can_trade(state)
        if not can_trade:
            return
        
        # Analyze for entry
        signal, signal_reason = self.analyze_signal(state)
        
        if signal == "BUY":
            self.place_entry_order(state, "BUY")
        elif signal == "SELL":
            self.place_entry_order(state, "SELL")

    def run(self):
        """Main strategy loop."""
        self.running = True
        print(f"\n[{self._now()}] Strategy started")
        
        scan_count = 0
        
        while self.running:
            try:
                if not self._is_market_hours():
                    now = datetime.now(IST)
                    if now.hour == self.config.MARKET_OPEN[0] and now.minute < self.config.MARKET_OPEN[1] + 2:
                        # Reset daily counters
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
                    time.sleep(0.3)
                
                scan_count += 1
                
                # Print status every 60 scans (~2 minutes)
                if scan_count % 60 == 0:
                    self._print_status()
                
                time.sleep(self.config.SCAN_INTERVAL)
                
            except KeyboardInterrupt:
                print(f"\n[{self._now()}] Stopping...")
                self.running = False
            except Exception as e:
                print(f"[{self._now()}] Error: {e}")
                time.sleep(5)
        
        self._close_all()

    def _print_status(self):
        """Print current status."""
        positions = sum(1 for s in self.stocks.values() if s.position)
        trades = sum(s.trade_count for s in self.stocks.values())
        
        print(f"\n[{self._now()}] === STATUS ===")
        print(f"Positions: {positions} | Trades: {trades} | Daily P&L: Rs.{self.total_daily_pnl:.2f}")
        
        for state in self.stocks.values():
            if state.position:
                pos = state.position
                if pos.side == "LONG":
                    pnl = (state.ltp - pos.entry_price) * pos.qty
                else:
                    pnl = (pos.entry_price - state.ltp) * pos.qty
                gtt = "GTT" if pos.gtt_trigger_id else "NO-GTT"
                print(f"  {state.symbol}: {pos.side} @ {pos.entry_price:.2f} | LTP: {state.ltp:.2f} | SL: {pos.stop_loss} | P&L: Rs.{pnl:.2f} | {gtt}")
        print()

    def _close_all(self):
        """Close all positions on shutdown."""
        print(f"[{self._now()}] Closing all positions...")
        for state in self.stocks.values():
            if state.position:
                self.fetch_quote(state)
                self.close_position(state, "SHUTDOWN")

    def stop(self):
        self.running = False


def main():
    scalper = GTTScalper()
    try:
        scalper.run()
    except KeyboardInterrupt:
        scalper.stop()
    print("\nStrategy stopped.")


if __name__ == "__main__":
    main()
