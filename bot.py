#!/usr/bin/env python3
"""
Multi-pair momentum paper trader — long + short + pyramiding
Pairs:   ETH/GBP, BTC/GBP, SOL/GBP (configurable)
Capital: £10,000 — 25% initial per position, 8% pyramid add on winners
Strategy:
  ENTRY    2 consecutive ticks in same direction + cumulative move > MOMENTUM_MIN
           → limit order at bid (long) or ask (short) — maker fee 0.14%
  PYRAMID  when unrealised gain > PYRAMID_TRIGGER (0.25%)
           → add one tranche at current bid/ask — maker fee 0.14%
  TP       limit order at entry × (1 ± TP_PCT) — closes full position
  SL       market order at entry × (1 ∓ SL_PCT) — closes full position
  REVERSAL market exit if 2 counter-ticks and above break-even
  GUARD    skip entry if ≥ MAX_SAME_DIR pairs already in same direction
           skip any order if available cash < RESERVE_MIN
"""

import os, time, logging, json, threading
from collections import deque
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

# ── Direction / order-type constants ─────────────────────────────────────────
LONG,  SHORT  = "LONG",  "SHORT"
BUY,   SELL   = "BUY",   "SELL"
LIMIT, MARKET = "LIMIT", "MARKET"

# ── Config ────────────────────────────────────────────────────────────────────
PAIRS           = os.getenv("PAIRS",    "ETHGBP,XBTGBP,SOLGBP").split(",")
POLL_SEC        = int(os.getenv("POLL_SEC",    "15"))
SESSION_SEC     = int(os.getenv("SESSION_SEC", "86400"))
TOTAL_CAPITAL   = float(os.getenv("TOTAL_CAPITAL", "10000"))
INITIAL_FRAC    = float(os.getenv("INITIAL_FRAC",  "0.25"))    # 25% per initial entry
PYRAMID_FRAC    = float(os.getenv("PYRAMID_FRAC",  "0.08"))    # 8% pyramid add
PYRAMID_TRIGGER = float(os.getenv("PYRAMID_TRIGGER","0.0040")) # add when gain > 0.40% (was 0.25%)
RESERVE_MIN     = float(os.getenv("RESERVE_MIN",   "500"))     # always keep £500 free
MAX_SAME_DIR    = int(os.getenv("MAX_SAME_DIR",    "1"))       # max pairs per direction (was 2)
FEE_MAKER       = float(os.getenv("FEE_MAKER",  "0.0014"))
FEE_TAKER       = float(os.getenv("FEE_TAKER",  "0.0024"))
MARGIN_OPEN     = float(os.getenv("MARGIN_OPEN","0.0002"))
MARGIN_4H       = float(os.getenv("MARGIN_4H",  "0.0002"))
TP_PCT          = float(os.getenv("TP_PCT",  "0.0080"))        # +0.80% take-profit (was 0.50%)
SL_PCT          = float(os.getenv("SL_PCT",  "0.0030"))
MOMENTUM_MIN    = float(os.getenv("MOMENTUM_MIN","0.0005"))    # 0.05% min move (was 0.02%)
SL_COOLDOWN     = int(os.getenv("SL_COOLDOWN",   "5"))         # ticks to wait after a stop-loss
LIMIT_EXPIRY    = int(os.getenv("LIMIT_EXPIRY",  "3"))
PORT            = int(os.getenv("PORT", "8080"))
KRAKEN_API      = "https://api.kraken.com/0/public"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("momentum")

# ── Kraken REST ───────────────────────────────────────────────────────────────
def fetch_prices(pair: str) -> tuple:
    """Return (bid, ask, mid) for a pair."""
    url = f"{KRAKEN_API}/Ticker?{urlencode({'pair': pair})}"
    req = Request(url, headers={"User-Agent": "momentum-bot/5.0"})
    with urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    if data.get("error"):
        raise RuntimeError(f"Kraken error: {data['error']}")
    t = next(iter(data["result"].values()))
    bid, ask = float(t["b"][0]), float(t["a"][0])
    return bid, ask, (bid + ask) / 2

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

# ── Domain model ──────────────────────────────────────────────────────────────
@dataclass
class Tranche:
    price:      float
    volume:     float
    cost:       float
    fee:        float
    is_pyramid: bool
    tp_target:  float
    sl_level:   float
    break_even: float
    entry_time: float = field(default_factory=time.time)

@dataclass
class Position:
    pair:      str
    direction: str
    tranches:  list  = field(default_factory=list)
    pending:   Optional[dict] = None

    @property
    def in_position(self) -> bool: return bool(self.tranches)
    @property
    def has_pending(self) -> bool: return self.pending is not None
    @property
    def pyramided(self) -> bool:   return len(self.tranches) > 1
    @property
    def total_volume(self) -> float: return sum(t.volume for t in self.tranches)
    @property
    def total_cost(self) -> float:   return sum(t.cost   for t in self.tranches)
    @property
    def tp_target(self) -> Optional[float]:
        return self.tranches[-1].tp_target if self.tranches else None
    @property
    def sl_level(self) -> Optional[float]:
        return self.tranches[0].sl_level if self.tranches else None
    @property
    def break_even(self) -> Optional[float]:
        return self.tranches[-1].break_even if self.tranches else None
    @property
    def avg_entry(self) -> float:
        tv = self.total_volume
        return sum(t.price * t.volume for t in self.tranches) / tv if tv else 0.0

    def rollover_fee(self) -> float:
        if not self.tranches: return 0.0
        hours   = (time.time() - self.tranches[0].entry_time) / 3600
        periods = int(hours / 4)
        return sum(t.cost * MARGIN_4H * periods for t in self.tranches)

    def current_gain(self, price: float) -> float:
        avg = self.avg_entry
        if not avg: return 0.0
        return ((price - avg) / avg if self.direction == LONG
                else (avg - price) / avg)

@dataclass
class Portfolio:
    cash:         float = TOTAL_CAPITAL
    positions:    dict  = field(default_factory=dict)  # pair → Position
    trades:       list  = field(default_factory=list)
    # Running totals — maintained incrementally, never recomputed
    session_pnl:  float = 0.0
    total_fees:   float = 0.0
    long_count:   int   = 0
    short_count:  int   = 0

    def direction_count(self, direction: str) -> int:
        return sum(1 for p in self.positions.values()
                   if p.in_position and p.direction == direction)

    def _can_spend(self, amount: float) -> bool:
        return self.cash - amount >= RESERVE_MIN

    def portfolio_value(self, prices: dict) -> float:
        """Total value: cash + unrealised P&L across all open positions."""
        value = self.cash
        for pair, pos in self.positions.items():
            if pos.in_position and pair in prices:
                price = prices[pair]
                if pos.direction == LONG:
                    value += pos.total_volume * price
                else:
                    value += sum((t.price - price) * t.volume for t in pos.tranches)
        return value

    # ── Entry ─────────────────────────────────────────────────────────────────
    def place_entry(self, pair: str, direction: str, limit_price: float):
        spend = round(self.cash * INITIAL_FRAC, 2)
        if not self._can_spend(spend):
            log.info("%-8s SKIP %s — low cash (need £%.0f reserve)", pair, direction, RESERVE_MIN)
            return
        if self.direction_count(direction) >= MAX_SAME_DIR:
            log.info("%-8s SKIP %s — already %d pairs in %s", pair, direction, MAX_SAME_DIR, direction)
            return
        pos = self.positions.setdefault(pair, Position(pair=pair, direction=direction))
        pos.direction = direction
        pos.pending   = {"type": "initial", "limit": limit_price, "spend": spend, "ticks": 0}
        log.info("%-8s LIMIT %s pending @ £%.2f (spend=£%.2f)", pair, direction, limit_price, spend)

    def place_pyramid(self, pair: str, limit_price: float):
        pos   = self.positions.get(pair)
        spend = round(self.cash * PYRAMID_FRAC, 2)
        if not pos or not pos.in_position or pos.pyramided or pos.has_pending:
            return
        if not self._can_spend(spend):
            log.info("%-8s SKIP pyramid — low cash", pair)
            return
        pos.pending = {"type": "pyramid", "limit": limit_price, "spend": spend, "ticks": 0}
        log.info("%-8s PYRAMID %s pending @ £%.2f (spend=£%.2f)", pair, pos.direction, limit_price, spend)

    # ── Fill ──────────────────────────────────────────────────────────────────
    def try_fill(self, pair: str, bid: float, ask: float) -> bool:
        pos = self.positions.get(pair)
        if not pos or not pos.has_pending:
            return False
        p = pos.pending
        p["ticks"] += 1

        # Fill condition: long fills when ask drops to limit; short when bid rises to limit
        filled = (ask <= p["limit"] if pos.direction == LONG else bid >= p["limit"])
        if not filled:
            if p["ticks"] >= LIMIT_EXPIRY:
                log.info("%-8s LIMIT %s expired", pair, pos.direction)
                pos.pending = None
            return False

        price     = p["limit"]
        spend     = p["spend"]
        trade_fee = round(spend * FEE_MAKER,   8)
        marg_fee  = round(spend * MARGIN_OPEN, 8)
        total_fee = trade_fee + marg_fee
        volume    = round((spend - trade_fee) / price, 8)

        # For longs, cash leaves immediately. For shorts, only fees leave cash.
        self.cash -= (spend + total_fee) if pos.direction == LONG else total_fee

        tp = round(price * (1 + TP_PCT) if pos.direction == LONG else price * (1 - TP_PCT), 4)
        sl = round(price * (1 - SL_PCT) if pos.direction == LONG else price * (1 + SL_PCT), 4)
        be = (price * (1 + FEE_MAKER + FEE_TAKER) if pos.direction == LONG
              else price * (1 - FEE_MAKER - FEE_TAKER))

        tranche = Tranche(price=price, volume=volume, cost=spend, fee=total_fee,
                          is_pyramid=p["type"] == "pyramid",
                          tp_target=tp, sl_level=sl, break_even=be)
        pos.tranches.append(tranche)
        pos.pending = None

        entry_side = BUY if pos.direction == LONG else SELL
        self.trades.append(dict(
            pair=pair, side=entry_side, direction=pos.direction, order=LIMIT,
            time=_ts(), price=price, volume=volume, fee=total_fee,
            net_pnl=None, reason=f"MOMENTUM{'↑' if pos.direction==LONG else '↓'}",
            is_pyramid=tranche.is_pyramid,
        ))
        self.total_fees += total_fee

        log.info("%-8s %s FILL  %s%.6f @ £%.2f | fee=£%.4f | TP=£%.2f SL=£%.2f%s",
                 pair, pos.direction, "▲ " if pos.direction == LONG else "▼ ",
                 volume, price, total_fee, tp, sl,
                 " [PYRAMID]" if tranche.is_pyramid else "")
        return True

    # ── Close ─────────────────────────────────────────────────────────────────
    def close_position(self, pair: str, price: float, reason: str, order_type: str):
        pos = self.positions.get(pair)
        if not pos or not pos.in_position:
            return
        fee_rate  = FEE_MAKER if order_type == LIMIT else FEE_TAKER
        roll_fee  = pos.rollover_fee()
        volume    = pos.total_volume
        trade_fee = round((volume * price) * fee_rate, 8)
        total_fee = trade_fee + roll_fee

        if pos.direction == LONG:
            proceeds = round(volume * price, 8)
            net_recv = proceeds - total_fee
            net_pnl  = net_recv - pos.total_cost
            self.cash += net_recv
        else:
            gross_pnl = round((pos.avg_entry - price) * volume, 8)
            net_pnl   = gross_pnl - total_fee
            self.cash += net_pnl

        gain_pct = pos.current_gain(price) * 100
        self.session_pnl += net_pnl
        self.total_fees  += total_fee
        if pos.direction == LONG:  self.long_count  += 1
        else:                      self.short_count += 1

        close_side = SELL if pos.direction == LONG else BUY
        self.trades.append(dict(
            pair=pair, side=close_side, direction=pos.direction, order=order_type,
            time=_ts(), price=price, volume=volume, fee=total_fee,
            net_pnl=net_pnl, reason=reason, is_pyramid=False,
        ))

        log.info("%-8s %s CLOSE %s%.5f @ £%.2f | pnl=£%+.4f (%+.3f%%) | %s",
                 pair, pos.direction, "▼ " if pos.direction == LONG else "▲ ",
                 volume, price, net_pnl, gain_pct, reason)
        del self.positions[pair]

# ── Shared state for status page ──────────────────────────────────────────────
state      = {"started_at": datetime.now(timezone.utc).isoformat(),
               "session_started_at": None, "tick": 0,
               "prices": {}, "positions": {}, "portfolio_value": TOTAL_CAPITAL,
               "cash": TOTAL_CAPITAL, "session_pnl": 0.0, "total_fees": 0.0,
               "long_count": 0, "short_count": 0,
               "trades": [], "status": "starting..."}
state_lock = threading.Lock()
trades_version = 0   # incremented only when trades list changes

# ── Strategy ──────────────────────────────────────────────────────────────────
def run_bot():
    global trades_version
    while True:
        portfolio     = Portfolio()
        price_history = {p: deque(maxlen=3) for p in PAIRS}  # only last 3 needed
        sl_cooldown   = {p: 0 for p in PAIRS}               # ticks remaining before re-entry allowed
        start         = time.time()

        with state_lock:
            state_update(state,
                         session_started_at=datetime.now(timezone.utc).isoformat(),
                         tick=0, positions={}, trades=[], cash=TOTAL_CAPITAL,
                         session_pnl=0.0, total_fees=0.0,
                         long_count=0, short_count=0, status="watching")

        log.info("NEW SESSION | pairs=%s capital=£%.0f tp=±%.2f%% sl=±%.2f%%",
                 ",".join(PAIRS), TOTAL_CAPITAL, TP_PCT*100, SL_PCT*100)

        tick = 0
        while time.time() - start < SESSION_SEC:
            tick += 1
            current_prices = {}

            for pair in PAIRS:
                try:
                    bid, ask, mid = fetch_prices(pair)
                except Exception as exc:
                    log.warning("%-8s price fetch failed: %s", pair, exc)
                    continue

                current_prices[pair] = mid
                hist = price_history[pair]
                hist.append(mid)
                pos  = portfolio.positions.get(pair)
                move = (mid - hist[-2]) / hist[-2] * 100 if len(hist) >= 2 else 0.0

                # ── Fill pending orders ────────────────────────────────────────
                if pos and pos.has_pending:
                    portfolio.try_fill(pair, bid, ask)
                    pos = portfolio.positions.get(pair)

                # ── Manage open position ───────────────────────────────────────
                if pos and pos.in_position:
                    gain = pos.current_gain(mid)
                    tp   = pos.tp_target
                    sl   = pos.sl_level
                    be   = pos.break_even

                    if (pos.direction == LONG  and mid >= tp) or \
                       (pos.direction == SHORT and mid <= tp):
                        portfolio.close_position(pair, tp, f"TAKE-PROFIT (limit)", LIMIT)

                    elif (pos.direction == LONG  and mid <= sl) or \
                         (pos.direction == SHORT and mid >= sl):
                        portfolio.close_position(pair, mid, f"STOP-LOSS (market)", MARKET)
                        sl_cooldown[pair] = SL_COOLDOWN
                        log.info("%-8s cooldown %d ticks before re-entry", pair, SL_COOLDOWN)

                    elif len(hist) == 3:
                        reversal = (hist[-1] < hist[-2] < hist[-3] if pos.direction == LONG
                                    else hist[-1] > hist[-2] > hist[-3])
                        if reversal and ((pos.direction == LONG  and mid >= be) or
                                         (pos.direction == SHORT and mid <= be)):
                            portfolio.close_position(pair, mid, "REVERSAL (market)", MARKET)
                        elif not pos.pyramided and gain >= PYRAMID_TRIGGER:
                            pyramid_price = bid if pos.direction == LONG else ask
                            portfolio.place_pyramid(pair, pyramid_price)
                        else:
                            log.info("%-8s %s hold  gain=%+.3f%%  TP=£%.2f  SL=£%.2f%s",
                                     pair, pos.direction, gain*100, tp, sl,
                                     " [+pyramid pending]" if pos.has_pending else "")
                    else:
                        log.info("%-8s %s hold  gain=%+.3f%%", pair, pos.direction, gain*100)

                # ── Entry signals ──────────────────────────────────────────────
                elif pos is None or not pos.has_pending:
                    if sl_cooldown[pair] > 0:
                        sl_cooldown[pair] -= 1
                        log.info("%-8s cooldown £%.2f  (%d ticks left)", pair, mid, sl_cooldown[pair])
                    elif len(hist) == 3:
                        cum_up = (hist[-1] - hist[-3]) / hist[-3]
                        cum_dn = (hist[-3] - hist[-1]) / hist[-3]
                        up = hist[-1] > hist[-2] > hist[-3]
                        dn = hist[-1] < hist[-2] < hist[-3]

                        if up and cum_up >= MOMENTUM_MIN:
                            portfolio.place_entry(pair, LONG, bid)
                        elif dn and cum_dn >= MOMENTUM_MIN:
                            portfolio.place_entry(pair, SHORT, ask)
                        else:
                            log.info("%-8s watch  £%.2f  %+.3f%%", pair, mid, move)
                    else:
                        log.info("%-8s baseline £%.2f", pair, mid)

            # ── Sync shared state (only trades list updated on change) ─────────
            pv = portfolio.portfolio_value(current_prices)
            pos_snapshot = {
                pair: {
                    "direction":  pos.direction,
                    "in_position":pos.in_position,
                    "has_pending":pos.has_pending,
                    "pyramided":  pos.pyramided,
                    "avg_entry":  round(pos.avg_entry, 2),
                    "tp_target":  pos.tp_target,
                    "sl_level":   pos.sl_level,
                    "gain_pct":   round(pos.current_gain(current_prices[pair]) * 100, 3)
                                  if pos.in_position and pair in current_prices else None,
                    "price":      current_prices.get(pair),
                    "pending_price": pos.pending["limit"] if pos.has_pending else None,
                }
                for pair, pos in portfolio.positions.items()
                if pos.in_position or pos.has_pending
            }

            with state_lock:
                trades_changed = len(portfolio.trades) != trades_version
                state_update(state,
                             tick=tick,
                             prices=dict(current_prices),
                             positions=pos_snapshot,
                             portfolio_value=round(pv, 2),
                             cash=round(portfolio.cash, 2),
                             session_pnl=round(portfolio.session_pnl, 4),
                             total_fees=round(portfolio.total_fees, 4),
                             long_count=portfolio.long_count,
                             short_count=portfolio.short_count,
                             status=_bot_status(portfolio),
                             **({'trades': list(portfolio.trades)} if trades_changed else {}))
                if trades_changed:
                    trades_version = len(portfolio.trades)

            time.sleep(POLL_SEC)

        # ── Session end: close all positions ───────────────────────────────────
        for pair in list(portfolio.positions):
            pos = portfolio.positions.get(pair)
            if pos and pos.in_position:
                try:
                    _, _, mid = fetch_prices(pair)
                    portfolio.close_position(pair, mid, "SESSION-END (market)", MARKET)
                except Exception as exc:
                    log.warning("%-8s session-end close failed: %s", pair, exc)

        log.info("Session complete | pnl=£%+.2f fees=£%.2f longs=%d shorts=%d. Restarting in 30s...",
                 portfolio.session_pnl, portfolio.total_fees,
                 portfolio.long_count, portfolio.short_count)
        with state_lock: state_update(state, status="restarting...")
        time.sleep(30)

def _bot_status(portfolio: Portfolio) -> str:
    active = [(p, pos.direction) for p, pos in portfolio.positions.items() if pos.in_position]
    if not active: return "watching"
    return " | ".join(f"{p} {'📈' if d==LONG else '📉'}" for p, d in active)

# ── Status page ───────────────────────────────────────────────────────────────
_html_cache    = ""
_html_dirty    = True
_html_lock     = threading.Lock()

def _mark_dirty():
    global _html_dirty
    with _html_lock: _html_dirty = True

def render_html(s: dict) -> str:
    # ── Per-pair position cards ──────────────────────────────────────────────
    pair_cards = ""
    all_pairs  = PAIRS + [p for p in s["positions"] if p not in PAIRS]
    for pair in all_pairs:
        pos   = s["positions"].get(pair)
        price = s["prices"].get(pair)
        if price is None and pos is None: continue

        price_str = f"£{price:,.2f}" if price else "—"
        if pos:
            d         = pos["direction"]
            d_col     = "#2ecc71" if d == LONG else "#e74c3c"
            gain      = pos.get("gain_pct")
            gain_str  = f"{gain:+.3f}%" if gain is not None else "—"
            gain_col  = "#2ecc71" if (gain or 0) > 0 else "#e74c3c"
            tp_str    = f"£{pos['tp_target']:,.2f}" if pos["tp_target"] else "—"
            sl_str    = f"£{pos['sl_level']:,.2f}"  if pos["sl_level"]  else "—"
            pend_str  = f"£{pos['pending_price']:,.2f}" if pos["pending_price"] else ""
            pyra_badge= ' <span style="color:#f39c12;font-size:10px">+PYRAMID</span>' if pos["pyramided"] else ""
            status_lbl= f'{d} ▲{pyra_badge}' if d == LONG else f'{d} ▼{pyra_badge}'
            if pos["has_pending"] and not pos["in_position"]:
                status_lbl = f'PENDING {d} @ {pend_str}'
                d_col = "#f39c12"
        else:
            d_col = gain_col = "#444"
            gain_str = tp_str = sl_str = "—"
            status_lbl = "FLAT"

        pair_cards += f"""
        <div class="card" style="border-color:{'#1e3a1e' if pos and pos.get('direction')==LONG else '#3a1e1e' if pos and pos.get('direction')==SHORT else '#1c1c38'}">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <span style="color:#e2b96f;font-weight:bold">{pair}</span>
            <span style="color:{d_col};font-size:12px">{status_lbl}</span>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:12px">
            <span style="color:#555">Price</span>   <span>{price_str}</span>
            <span style="color:#555">Gain</span>    <span style="color:{gain_col}">{gain_str}</span>
            <span style="color:#555">TP</span>      <span style="color:#2ecc71">{tp_str}</span>
            <span style="color:#555">SL</span>      <span style="color:#e74c3c">{sl_str}</span>
          </div>
        </div>"""

    # ── Trade log rows ────────────────────────────────────────────────────────
    rows = ""
    for t in reversed(s["trades"]):
        pnl     = t.get("net_pnl")
        pnl_str = f"£{pnl:+.4f}" if pnl is not None else "open"
        pnl_col = "#2ecc71" if (pnl or 0) > 0 else "#e74c3c" if (pnl or 0) < 0 else "#888"
        d_col   = "#2ecc71" if t["direction"] == LONG else "#e74c3c"
        pyra    = ' <span style="color:#f39c12;font-size:9px">PYR</span>' if t.get("is_pyramid") else ""
        rows   += f"""<tr>
          <td>{t['time']}</td>
          <td style="color:{d_col}">{t['pair']}</td>
          <td>{t['side']}{pyra}</td>
          <td style="font-size:10px;color:#555">{t['order']}</td>
          <td>£{t['price']:,.2f}</td>
          <td>{t['volume']:.5f}</td>
          <td>£{t['fee']:.4f}</td>
          <td style="color:{pnl_col};font-weight:bold">{pnl_str}</td>
          <td style="font-size:11px;color:#555">{t['reason']}</td>
        </tr>"""

    pnl     = s["session_pnl"]
    pv      = s["portfolio_value"]
    pv_diff = pv - TOTAL_CAPITAL
    pnl_col = "#2ecc71" if pnl >= 0  else "#e74c3c"
    pv_col  = "#2ecc71" if pv_diff >= 0 else "#e74c3c"

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="15">
  <title>Momentum Bot</title>
  <style>
    *    {{ box-sizing:border-box;margin:0;padding:0 }}
    body {{ font-family:'Courier New',monospace;background:#0b0b18;color:#ccc;padding:24px }}
    h1   {{ color:#e2b96f;font-size:20px;margin-bottom:4px }}
    .sub {{ color:#444;font-size:12px;margin-bottom:20px }}
    .sec {{ color:#e2b96f;font-size:10px;text-transform:uppercase;letter-spacing:2px;margin:18px 0 8px }}
    .grid{{ display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px }}
    .pgrid{{ display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px }}
    .card{{ background:#111128;border:1px solid #1c1c38;border-radius:8px;padding:14px }}
    .lbl {{ color:#444;font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px }}
    .val {{ font-size:18px;font-weight:bold }}
    table{{ width:100%;border-collapse:collapse;background:#111128;border:1px solid #1c1c38;border-radius:8px;overflow:hidden }}
    th   {{ background:#090916;padding:8px 10px;text-align:left;font-size:10px;color:#444;text-transform:uppercase }}
    td   {{ padding:7px 10px;border-bottom:1px solid #161630;font-size:12px }}
    tr:last-child td{{ border:none }}
    .dot {{ animation:pulse 2s infinite }}
    @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
  </style>
</head>
<body>
  <h1>📈📉 Multi-Pair Momentum Bot</h1>
  <div class="sub">
    {",".join(PAIRS)} &nbsp;·&nbsp; Kraken Pro · maker {FEE_MAKER*100:.2f}% taker {FEE_TAKER*100:.2f}% margin {MARGIN_OPEN*100:.2f}%+{MARGIN_4H*100:.2f}%/4h
    &nbsp;·&nbsp; tick #{s['tick']} &nbsp;·&nbsp;
    <span class="dot">● {s['status']}</span>
  </div>

  <div class="sec">Portfolio</div>
  <div class="grid">
    <div class="card"><div class="lbl">Value</div><div class="val" style="color:{pv_col}">£{pv:,.2f}</div></div>
    <div class="card"><div class="lbl">Cash</div><div class="val">£{s['cash']:,.2f}</div></div>
    <div class="card"><div class="lbl">Session P&amp;L</div><div class="val" style="color:{pnl_col}">£{pnl:+.2f}</div></div>
    <div class="card"><div class="lbl">Total Fees</div><div class="val" style="color:#e67e22">£{s['total_fees']:.2f}</div></div>
    <div class="card"><div class="lbl">Long closes</div><div class="val" style="color:#2ecc71">{s['long_count']}</div></div>
    <div class="card"><div class="lbl">Short closes</div><div class="val" style="color:#e74c3c">{s['short_count']}</div></div>
  </div>

  <div class="sec">Positions</div>
  <div class="pgrid">{pair_cards}</div>

  <div class="sec">Trade Log</div>
  <table>
    <tr><th>Time</th><th>Pair</th><th>Side</th><th>Order</th><th>Price</th><th>Vol</th><th>Fee</th><th>P&amp;L</th><th>Reason</th></tr>
    {"".join(rows) or "<tr><td colspan='9' style='text-align:center;color:#333;padding:20px'>No trades yet</td></tr>"}
  </table>
</body>
</html>"""

class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _html_cache, _html_dirty
        with _html_lock:
            dirty = _html_dirty
        if dirty:
            with state_lock: s = dict(state)
            html = render_html(s) if s["prices"] else \
                   "<html><body style='background:#0b0b18;color:#eee;font-family:monospace;padding:30px'><h2>⏳ Starting...</h2></body></html>"
            with _html_lock:
                _html_cache = html
                _html_dirty = False
        else:
            with _html_lock: html = _html_cache

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def log_message(self, *args): pass

def state_update(d: dict, **kw):
    """Update state dict and mark HTML cache dirty."""
    d.update(kw)
    _mark_dirty()

# ── Entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Starting | pairs=%s port=%d capital=£%.0f", ",".join(PAIRS), PORT, TOTAL_CAPITAL)
    threading.Thread(target=run_bot, daemon=True).start()
    log.info("Status page → http://0.0.0.0:%d", PORT)
    HTTPServer(("0.0.0.0", PORT), StatusHandler).serve_forever()
