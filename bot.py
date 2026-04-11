#!/usr/bin/env python3
"""
BTC/GBP fee-aware momentum paper trader
Runs continuously on Render as a Background Worker.
Uses Kraken public REST API — no CLI or API key required.

Strategy:
  ENTRY  — 2 consecutive rising ticks AND cumulative rise > MOMENTUM_MIN
  EXIT   — take-profit at +TP_PCT, stop-loss at -SL_PCT,
           or momentum reversal (only if price > break-even)
"""

import os, time, logging, requests
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

# ── Config (override via env vars on Render) ──────────────────────────────────
PAIR           = os.getenv("PAIR",            "XBTGBP")
POLL_SEC       = int(os.getenv("POLL_SEC",    "15"))      # seconds between ticks
SESSION_SEC    = int(os.getenv("SESSION_SEC", "86400"))   # reset every 24 h
START_GBP      = float(os.getenv("START_GBP", "10000"))
FEE_RATE       = float(os.getenv("FEE_RATE",  "0.0026"))  # 0.26% per leg
TP_PCT         = float(os.getenv("TP_PCT",    "0.0070"))  # +0.70% take-profit
SL_PCT         = float(os.getenv("SL_PCT",    "0.0040"))  # -0.40% stop-loss
MOMENTUM_MIN   = float(os.getenv("MOMENTUM_MIN", "0.0003"))  # min 2-tick rise
TRADE_FRAC     = float(os.getenv("TRADE_FRAC", "0.90"))   # 90% of cash per buy
KRAKEN_API     = "https://api.kraken.com/0/public"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("momentum")

# ── Kraken REST helpers ───────────────────────────────────────────────────────
session = requests.Session()
session.headers.update({"User-Agent": "btc-momentum-bot/2.0"})

def get_ticker(pair: str) -> dict:
    r = session.get(f"{KRAKEN_API}/Ticker", params={"pair": pair}, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken API error: {data['error']}")
    return next(iter(data["result"].values()))

def mid_price(pair: str) -> float:
    t = get_ticker(pair)
    return (float(t["b"][0]) + float(t["a"][0])) / 2

# ── Paper trading state ───────────────────────────────────────────────────────
@dataclass
class PaperAccount:
    cash: float = START_GBP
    btc:  float = 0.0
    entry_price:  Optional[float] = None
    entry_cost:   Optional[float] = None
    break_even:   Optional[float] = None
    trades: list  = field(default_factory=list)
    session_start_cash: float = START_GBP

    @property
    def in_position(self) -> bool:
        return self.btc > 0.00001

    def buy_market(self, price: float) -> dict:
        spend  = round(self.cash * TRADE_FRAC, 8)
        fee    = round(spend * FEE_RATE, 8)
        net_spend = spend + fee
        volume = round((spend - fee) / price, 8)
        self.cash        -= net_spend
        self.btc         += volume
        self.entry_price  = price
        self.entry_cost   = spend
        self.break_even   = price * (1 + FEE_RATE * 2)
        trade = dict(side="BUY", time=_ts(), price=price, volume=volume,
                     fee=fee, cost=net_spend, net_pnl=None, reason="MOMENTUM")
        self.trades.append(trade)
        log.info("BUY   %.8f XBT @ £%.2f | cost=£%.2f fee=£%.4f | break-even=£%.2f",
                 volume, price, net_spend, fee, self.break_even)
        return trade

    def sell_market(self, price: float, reason: str) -> dict:
        volume   = self.btc
        proceeds = round(volume * price, 8)
        fee      = round(proceeds * FEE_RATE, 8)
        net_recv = proceeds - fee
        gain_pct = (price - self.entry_price) / self.entry_price * 100
        net_pnl  = net_recv - self.entry_cost
        self.cash        += net_recv
        self.btc          = 0.0
        self.entry_price  = self.entry_cost = self.break_even = None
        trade = dict(side="SELL", time=_ts(), price=price, volume=volume,
                     fee=fee, cost=net_recv, net_pnl=net_pnl, reason=reason)
        self.trades.append(trade)
        log.info("SELL  %.8f XBT @ £%.2f | recv=£%.2f fee=£%.4f | net_pnl=£%+.4f (%+.3f%%)",
                 volume, price, net_recv, fee, net_pnl, gain_pct)
        return trade

    def portfolio_value(self, current_price: float) -> float:
        return self.cash + self.btc * current_price

    def print_summary(self, current_price: float):
        value   = self.portfolio_value(current_price)
        abs_pnl = value - self.session_start_cash
        pct_pnl = abs_pnl / self.session_start_cash * 100
        sells   = [t for t in self.trades if t["side"] == "SELL"]
        total_net = sum(t["net_pnl"] for t in sells if t["net_pnl"])
        total_fees = sum(t["fee"] for t in self.trades)

        log.info("─" * 60)
        log.info("SESSION SUMMARY")
        log.info("  Starting balance : £%.2f",  self.session_start_cash)
        log.info("  Portfolio value  : £%.2f",  value)
        log.info("  Abs P&L          : £%+.4f (%+.4f%%)", abs_pnl, pct_pnl)
        log.info("  Completed trades : %d buys, %d sells",
                 len([t for t in self.trades if t["side"] == "BUY"]), len(sells))
        log.info("  Net closed P&L   : £%+.4f", total_net)
        log.info("  Total fees paid  : £%.4f",  total_fees)
        log.info("─" * 60)

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

# ── Momentum strategy ─────────────────────────────────────────────────────────
def run_session(account: PaperAccount):
    prices = []
    start  = time.time()
    tick   = 0

    log.info("═" * 60)
    log.info("NEW SESSION | pair=%s start=£%.2f tp=+%.2f%% sl=-%.2f%% poll=%ds",
             PAIR, account.cash, TP_PCT * 100, SL_PCT * 100, POLL_SEC)
    log.info("═" * 60)

    while time.time() - start < SESSION_SEC:
        tick += 1
        try:
            price = mid_price(PAIR)
        except Exception as exc:
            log.warning("Price fetch failed: %s — retrying next tick", exc)
            time.sleep(POLL_SEC)
            continue

        prices.append(price)
        move_pct = (price - prices[-2]) / prices[-2] * 100 if len(prices) >= 2 else 0.0

        # ── EXIT ──────────────────────────────────────────────────────────────
        if account.in_position:
            gain = (price - account.entry_price) / account.entry_price

            if gain >= TP_PCT:
                account.sell_market(price, "TAKE-PROFIT")
            elif gain <= -SL_PCT:
                account.sell_market(price, "STOP-LOSS")
            elif len(prices) >= 3:
                reversal = prices[-1] < prices[-2] < prices[-3]
                if reversal and price >= account.break_even:
                    account.sell_market(price, "REVERSAL")
                else:
                    log.info("tick=%-3d  £%.2f  %+.3f%%  → hold (gain=%+.3f%%)",
                             tick, price, move_pct, gain * 100)
            else:
                log.info("tick=%-3d  £%.2f  %+.3f%%  → hold (gain=%+.3f%%)",
                         tick, price, move_pct, gain * 100)

        # ── ENTRY ─────────────────────────────────────────────────────────────
        elif len(prices) >= 3:
            up1      = prices[-1] > prices[-2]
            up2      = prices[-2] > prices[-3]
            cum_rise = (prices[-1] - prices[-3]) / prices[-3]

            if up1 and up2 and cum_rise >= MOMENTUM_MIN:
                account.buy_market(price)
            else:
                reason = ("momentum not sustained" if not (up1 and up2)
                          else f"rise {cum_rise*100:.3f}% < {MOMENTUM_MIN*100:.3f}% min")
                log.info("tick=%-3d  £%.2f  %+.3f%%  → no entry (%s)",
                         tick, price, move_pct, reason)
        else:
            log.info("tick=%-3d  £%.2f  %+.3f%%  → collecting baseline...",
                     tick, price, move_pct)

        # Keep price history bounded
        if len(prices) > 100:
            prices = prices[-50:]

        time.sleep(POLL_SEC)

    # Close open position at session end
    if account.in_position:
        price = mid_price(PAIR)
        account.sell_market(price, "SESSION-END")

    account.print_summary(price if account.btc == 0 else mid_price(PAIR))

# ── Entrypoint ────────────────────────────────────────────────────────────────
def main():
    log.info("BTC Momentum Bot starting | pair=%s", PAIR)
    while True:
        account = PaperAccount()
        try:
            run_session(account)
        except KeyboardInterrupt:
            log.info("Interrupted — shutting down.")
            break
        except Exception as exc:
            log.error("Session crashed: %s — restarting in 60s", exc, exc_info=True)
            time.sleep(60)

        log.info("Session complete. Restarting in 30s...")
        time.sleep(30)

if __name__ == "__main__":
    main()
