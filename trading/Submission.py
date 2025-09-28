

from enum import Enum
from typing import Optional, Dict, Any
import time
from collections import deque

class Side(Enum):
    BUY = 0
    SELL = 1

class Ticker(Enum):
    TEAM_A = 0

def place_market_order(side: Side, ticker: Ticker, quantity: float) -> None:
    return

def place_limit_order(side: Side, ticker: Ticker, quantity: float, price: float, ioc: bool = False) -> int:
    return 0

def cancel_order(ticker: Ticker, order_id: int) -> bool:
    return False

class Strategy:
    def reset_state(self) -> None:
        self.position = 0.0
        self.avg_cost = 0.0
        self.realized_pnl = 0.0
        self.unrealized_pnl = 0.0
        self.capital = 10000.0

        self.orderbook = {'bids': {}, 'asks': {}}
        self.best_bid = None
        self.best_ask = None
        self.last_trade_price = None
        self.last_market_price = None

        self.my_orders: Dict[int, Dict[str, Any]] = {}

        # risk params
        self.MAX_CONTRACTS_PER_GAME = 50
        self.MAX_DOLLAR_EXPOSURE = 5000.0
        self.MIN_CAPITAL_PER_TRADE = 20.0

        # strategy params
        self.EDGE_THRESHOLD = 0.05   # lower threshold now, since we scale by edge
        self.IAT_COOLDOWN = 0.25     # throttle decisions
        self._last_action_ts = 0.0
        self.TICK = 0.5
        self.MIN_PRICE = 0.0
        self.MAX_PRICE = 100.0

        self.recent_scores = deque(maxlen=5)

    def __init__(self) -> None:
        self.reset_state()

    def _update_best_prices(self) -> None:
        bids = self.orderbook['bids']
        asks = self.orderbook['asks']
        self.best_bid = max(bids.keys()) if bids else None
        self.best_ask = min(asks.keys()) if asks else None
        if self.best_bid is not None and self.best_ask is not None:
            self.last_market_price = (self.best_bid + self.best_ask) / 2.0
        elif self.last_trade_price is not None:
            self.last_market_price = self.last_trade_price
        else:
            self.last_market_price = None

    def _safe_price(self, p: float) -> float:
        return min(max(p, self.MIN_PRICE), self.MAX_PRICE)

    def _record_fill(self, side: Side, price: float, quantity: float) -> None:
        qty = float(quantity)
        price = float(price)
        if side == Side.BUY:
            if self.position >= 0:
                prev_cost = self.avg_cost * self.position
                self.position += qty
                self.avg_cost = (prev_cost + price * qty) / self.position if self.position else 0.0
            else:  # covering shorts
                cover_qty = min(abs(self.position), qty)
                self.realized_pnl += (self.avg_cost - price) * cover_qty
                self.position += cover_qty
                qty -= cover_qty
                if qty > 0:
                    prev_cost = self.avg_cost * self.position
                    self.position += qty
                    self.avg_cost = (prev_cost + price * qty) / self.position if self.position else 0.0
        else:  # SELL
            if self.position > 0:
                close_qty = min(self.position, qty)
                self.realized_pnl += (price - self.avg_cost) * close_qty
                self.position -= close_qty
                qty -= close_qty
                if qty > 0:  # new short
                    self.avg_cost = price
                    self.position -= qty
            else:
                prev_cost = self.avg_cost * abs(self.position)
                self.position -= qty
                self.avg_cost = (prev_cost + price * qty) / abs(self.position) if self.position else 0.0

    def _can_trade(self, qty: float) -> bool:
        if self.last_market_price is None:
            return False
        notional = qty * self.last_market_price
        if abs(self.position) + qty > self.MAX_CONTRACTS_PER_GAME:
            return False
        if self.capital < self.MIN_CAPITAL_PER_TRADE:
            return False
        if notional > self.MAX_DOLLAR_EXPOSURE:
            return False
        return True

    def on_trade_update(self, ticker: Ticker, side: Side, quantity: float, price: float) -> None:
        self.last_trade_price = float(price)
        self._update_best_prices()

    def on_orderbook_update(self, ticker: Ticker, side: Side, quantity: float, price: float) -> None:
        price_f, qty_f = float(price), float(quantity)
        book_side = 'bids' if side == Side.BUY else 'asks'
        if qty_f <= 0:
            self.orderbook[book_side].pop(price_f, None)
        else:
            self.orderbook[book_side][price_f] = qty_f
        self._update_best_prices()

    def on_account_update(self, ticker: Ticker, side: Side, price: float, quantity: float, capital_remaining: float) -> None:
        self.capital = float(capital_remaining)
        self._record_fill(side, price, quantity)

    def on_game_event_update(self,
                             event_type: str,
                             home_away: str,
                             home_score: int,
                             away_score: int,
                             player_name: Optional[str],
                             substituted_player_name: Optional[str],
                             shot_type: Optional[str],
                             assist_player: Optional[str],
                             rebound_type: Optional[str],
                             coordinate_x: Optional[float],
                             coordinate_y: Optional[float],
                             time_seconds: Optional[float]) -> None:

        # reset state at end
        if event_type == "END_GAME":
            print(f"Game over. Final PnL: {self.realized_pnl + self.unrealized_pnl:.2f}")
            self.reset_state()
            return

        # update momentum tracker
        if event_type == "SCORE":
            self.recent_scores.append(home_away)

        # skip if no price
        if self.last_market_price is None:
            return

        # basic win-prob model
        score_diff = float(home_score - away_score)
        total_seconds = 2400.0
        t_remain = float(time_seconds) if time_seconds is not None else total_seconds
        time_factor = 1.0 - (t_remain / total_seconds)
        model_p = 0.5 + 0.012 * score_diff + 0.25 * time_factor

        # add momentum bias
        if len(self.recent_scores) >= 3:
            if all(s == "home" for s in self.recent_scores):
                model_p += 0.03
            elif all(s == "away" for s in self.recent_scores):
                model_p -= 0.03

        model_p = min(max(model_p, 0.01), 0.99)
        market_p = self.last_market_price / 100.0
        edge = model_p - market_p

        # dynamic sizing
        qty = max(1, int(abs(edge) * 20))  # scale with edge
        qty = min(qty, 5)  # cap per trade

        # end-game unwind: close position with <30s left
        if t_remain < 30 and self.position != 0:
            side = Side.SELL if self.position > 0 else Side.BUY
            try:
                place_market_order(side, Ticker.TEAM_A, abs(self.position))
                print(f"[unwind] Closing {self.position} contracts with {t_remain:.1f}s left")
            except Exception:
                pass
            return

        # throttle
        now = time.time()
        if now - self._last_action_ts < self.IAT_COOLDOWN:
            return

        # trade decision
        if edge > self.EDGE_THRESHOLD and self._can_trade(qty):
            post_price = self._safe_price(self.best_bid + self.TICK if self.best_bid else self.last_market_price)
            if post_price < 100.0:
                place_limit_order(Side.BUY, Ticker.TEAM_A, qty, post_price, ioc=True)
                print(f"[buy] qty={qty} price={post_price:.1f} model_p={model_p:.3f} market_p={market_p:.3f} edge={edge:.3f}")
                self._last_action_ts = now

        elif edge < -self.EDGE_THRESHOLD and self._can_trade(qty):
            post_price = self._safe_price(self.best_ask - self.TICK if self.best_ask else self.last_market_price)
            if post_price > 0.0:
                place_limit_order(Side.SELL, Ticker.TEAM_A, qty, post_price, ioc=True)
                print(f"[sell] qty={qty} price={post_price:.1f} model_p={model_p:.3f} market_p={market_p:.3f} edge={edge:.3f}")
                self._last_action_ts = now
