#!/usr/bin/env python3
"""
ETH/GBP fee-aware momentum paper trader — Kraken Pro, limit orders, long + short
Runs on Render as a Web Service. No external dependencies.

Order logic:
  LONG  entry  — 2 rising ticks  → limit BUY  at bid  (maker 0.14%)
  SHORT entry  — 2 falling ticks → limit SELL at ask  (maker 0.14%)
  TP exit      — limit order at target price           (maker 0.14%)
  SL / reversal— market order                          (taker 0.24%)
  Margin fees  — 0.02% open + 0.02% per 4h rollover (Kraken rates)

Round-trip fees (excl. margin):
  Limit/Limit  : 0.14% + 0.14% = 0.28%
  Limit/Market : 0.14% + 0.24% = 0.38%
"""

import os, time, logging, json, threading
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────
PAIR          = os.getenv("PAIR",           "ETHGBP")
POLL_SEC      = int(os.getenv("POLL_SEC",   "15"))
SESSION_SEC   = int(os.getenv("SESSION_SEC","86400"))
START_GBP     = float(os.getenv("START_GBP","10000"))
FEE_MAKER     = float(os.getenv("FEE_MAKER","0.0014"))   # 0.14% Kraken Pro maker
FEE_TAKER     = float(os.getenv("FEE_TAKER","0.0024"))   # 0.24% Kraken Pro taker
MARGIN_OPEN   = float(os.getenv("MARGIN_OPEN","0.0002")) # 0.02% margin opening fee
MARGIN_4H     = float(os.getenv("MARGIN_4H","0.0002"))   # 0.02% rollover per 4h
TP_PCT        = float(os.getenv("TP_PCT",  "0.0050"))    # +/-0.50% take-profit
SL_PCT        = float(os.getenv("SL_PCT",  "0.0030"))    # +/-0.30% stop-loss
MOMENTUM_MIN  = float(os.getenv("MOMENTUM_MIN","0.0002"))
TRADE_FRAC    = float(os.getenv("TRADE_FRAC","0.90"))
LIMIT_EXPIRY  = int(os.getenv("LIMIT_EXPIRY","3"))       # ticks before cancelling unfilled limit
PORT          = int(os.getenv("PORT","8080"))
KRAKEN_API    = "https://api.kraken.com/0/public"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("momentum")

# ── Shared state ──────────────────────────────────────────────────────────────
state = {
    "pair": PAIR,
    "started_at": datetime.now(timezone.utc).isoformat(),
    "session_started_at": None,
    "tick": 0, "last_price": None, "last_bid": None, "last_ask": None,
    "position_type": None,      # "long", "short", or None
    "entry_price": None, "tp_target": None, "sl_level": None,
    "current_gain_pct": None, "pending_order": None,
    "cash": START_GBP, "btc": 0.0, "portfolio_value": START_GBP,
    "trades": [], "session_pnl": 0.0, "total_fees": 0.0,
    "long_trades": 0, "short_trades": 0, "status": "starting...",
}
state_lock = threading.Lock()

# ── Kraken REST ───────────────────────────────────────────────────────────────
def get_ticker(pair: str) -> dict:
    url = f"{KRAKEN_API}/Ticker?{urlencode({'pair': pair})}"
    req = Request(url, headers={"User-Agent": "momentum-bot/4.0"})
    with urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    if data.get("error"):
        raise RuntimeError(f"Kraken error: {data['error']}")
    return next(iter(data["result"].values()))

def get_prices(pair: str):
    t = get_ticker(pair)
    bid = float(t["b"][0]); ask = float(t["a"][0])
    return bid, ask, (bid + ask) / 2

def _ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

# ── Paper trading account ─────────────────────────────────────────────────────
@dataclass
class PaperAccount:
    cash:         float = START_GBP
    btc:          float = 0.0         # > 0 = long position held
    short_vol:    float = 0.0         # > 0 = short position open (borrowed ETH sold)
    entry_price:  Optional[float] = None
    entry_cost:   Optional[float] = None  # cash deployed
    entry_time:   Optional[float] = None  # unix ts for rollover calc
    break_even:   Optional[float] = None
    tp_target:    Optional[float] = None
    sl_level:     Optional[float] = None
    position_type: Optional[str] = None  # "long" | "short"
    pending:      Optional[dict] = None  # pending limit order
    trades:       list = field(default_factory=list)
    start_cash:   float = START_GBP

    @property
    def in_long(self):  return self.btc > 0.00001
    @property
    def in_short(self): return self.short_vol > 0.00001
    @property
    def in_position(self): return self.in_long or self.in_short
    @property
    def has_pending(self): return self.pending is not None

    # ── Margin rollover fee ───────────────────────────────────────────────────
    def rollover_fee(self) -> float:
        if not self.entry_time or not self.entry_cost:
            return 0.0
        hours = (time.time() - self.entry_time) / 3600
        periods = int(hours / 4)  # fee charged per 4h block
        return round(self.entry_cost * MARGIN_4H * periods, 8)

    # ── LONG entry ────────────────────────────────────────────────────────────
    def place_limit_buy(self, bid: float):
        spend = round(self.cash * TRADE_FRAC, 8)
        self.pending = {"side": "long", "limit": bid, "spend": spend, "ticks": 0}
        log.info("LIMIT BUY  pending @ £%.2f (spend=£%.2f)", bid, spend)

    def try_fill_buy(self, bid: float, ask: float) -> bool:
        p = self.pending; p["ticks"] += 1
        if bid <= p["limit"] or ask <= p["limit"]:
            price = p["limit"]; spend = p["spend"]
            margin_fee = round(spend * MARGIN_OPEN, 8)
            trade_fee  = round(spend * FEE_MAKER, 8)
            total_fee  = margin_fee + trade_fee
            net_spend  = spend + total_fee
            volume     = round((spend - trade_fee) / price, 8)
            self.cash       -= net_spend
            self.btc        += volume
            self.entry_price = price; self.entry_cost = spend
            self.entry_time  = time.time()
            self.position_type = "long"
            self.break_even  = price * (1 + FEE_MAKER + FEE_TAKER)
            self.tp_target   = round(price * (1 + TP_PCT), 4)
            self.sl_level    = round(price * (1 - SL_PCT), 4)
            self.pending     = None
            t = dict(side="BUY", direction="LONG", order="LIMIT", time=_ts(),
                     price=price, volume=volume, fee=total_fee, net_pnl=None,
                     reason="MOMENTUM↑")
            self.trades.append(t)
            log.info("LONG  FILLED %.6f @ £%.2f | fee=£%.4f | TP=£%.2f SL=£%.2f",
                     volume, price, total_fee, self.tp_target, self.sl_level)
            return True
        if p["ticks"] >= LIMIT_EXPIRY:
            log.info("LIMIT BUY expired"); self.pending = None
        return False

    # ── LONG exit ─────────────────────────────────────────────────────────────
    def close_long_limit(self, price: float) -> dict:
        return self._close_long(self.tp_target, FEE_MAKER, "TAKE-PROFIT↑ (limit)")

    def close_long_market(self, price: float, reason: str) -> dict:
        return self._close_long(price, FEE_TAKER, reason)

    def _close_long(self, price: float, fee_rate: float, reason: str) -> dict:
        volume    = self.btc
        proceeds  = round(volume * price, 8)
        trade_fee = round(proceeds * fee_rate, 8)
        roll_fee  = self.rollover_fee()
        total_fee = trade_fee + roll_fee
        net_recv  = proceeds - total_fee
        gain_pct  = (price - self.entry_price) / self.entry_price * 100
        net_pnl   = net_recv - self.entry_cost
        self.cash += net_recv; self.btc = 0.0
        self._reset_position()
        t = dict(side="SELL", direction="LONG", order="LIMIT" if "limit" in reason else "MARKET",
                 time=_ts(), price=price, volume=volume, fee=total_fee,
                 net_pnl=net_pnl, reason=reason)
        self.trades.append(t)
        log.info("LONG  CLOSE %.6f @ £%.2f | fee=£%.4f | net_pnl=£%+.4f (%+.3f%%) | %s",
                 volume, price, total_fee, net_pnl, gain_pct, reason)
        return t

    # ── SHORT entry ───────────────────────────────────────────────────────────
    def place_limit_short(self, ask: float):
        spend = round(self.cash * TRADE_FRAC, 8)
        self.pending = {"side": "short", "limit": ask, "spend": spend, "ticks": 0}
        log.info("LIMIT SHORT pending @ £%.2f (collateral=£%.2f)", ask, spend)

    def try_fill_short(self, bid: float, ask: float) -> bool:
        p = self.pending; p["ticks"] += 1
        if ask >= p["limit"] or bid >= p["limit"]:
            price      = p["limit"]; spend = p["spend"]
            margin_fee = round(spend * MARGIN_OPEN, 8)
            trade_fee  = round(spend * FEE_MAKER, 8)
            total_fee  = margin_fee + trade_fee
            volume     = round((spend - trade_fee) / price, 8)
            # We've sold volume ETH short; cash increases by proceeds, collateral locked
            self.cash       -= total_fee          # only fees leave cash now
            self.short_vol  += volume
            self.entry_price = price; self.entry_cost = spend
            self.entry_time  = time.time()
            self.position_type = "short"
            self.break_even  = price * (1 - FEE_MAKER - FEE_TAKER)  # min buyback to profit
            self.tp_target   = round(price * (1 - TP_PCT), 4)        # buy back lower
            self.sl_level    = round(price * (1 + SL_PCT), 4)        # buy back higher = loss
            self.pending     = None
            t = dict(side="SELL", direction="SHORT", order="LIMIT", time=_ts(),
                     price=price, volume=volume, fee=total_fee, net_pnl=None,
                     reason="MOMENTUM↓")
            self.trades.append(t)
            log.info("SHORT FILLED %.6f @ £%.2f | fee=£%.4f | TP=£%.2f SL=£%.2f",
                     volume, price, total_fee, self.tp_target, self.sl_level)
            return True
        if p["ticks"] >= LIMIT_EXPIRY:
            log.info("LIMIT SHORT expired"); self.pending = None
        return False

    # ── SHORT exit ────────────────────────────────────────────────────────────
    def close_short_limit(self, price: float) -> dict:
        return self._close_short(self.tp_target, FEE_MAKER, "TAKE-PROFIT↓ (limit)")

    def close_short_market(self, price: float, reason: str) -> dict:
        return self._close_short(price, FEE_TAKER, reason)

    def _close_short(self, price: float, fee_rate: float, reason: str) -> dict:
        volume    = self.short_vol
        # We buy back at price to close the short
        buyback   = round(volume * price, 8)
        trade_fee = round(buyback * fee_rate, 8)
        roll_fee  = self.rollover_fee()
        total_fee = trade_fee + roll_fee
        # P&L: we sold at entry_price, buy back at price
        gross_pnl = round((self.entry_price - price) * volume, 8)
        net_pnl   = gross_pnl - total_fee
        gain_pct  = (self.entry_price - price) / self.entry_price * 100
        self.cash      += net_pnl   # profit/loss added to cash
        self.short_vol  = 0.0
        self._reset_position()
        t = dict(side="BUY", direction="SHORT", order="LIMIT" if "limit" in reason else "MARKET",
                 time=_ts(), price=price, volume=volume, fee=total_fee,
                 net_pnl=net_pnl, reason=reason)
        self.trades.append(t)
        log.info("SHORT CLOSE %.6f @ £%.2f | fee=£%.4f | net_pnl=£%+.4f (%+.3f%%) | %s",
                 volume, price, total_fee, net_pnl, gain_pct, reason)
        return t

    def _reset_position(self):
        self.entry_price = self.entry_cost = self.entry_time = None
        self.break_even = self.tp_target = self.sl_level = None
        self.position_type = None

    def portfolio_value(self, price: float) -> float:
        long_val  = self.btc * price
        # Short: unrealised P&L = (entry - current) * volume
        short_val = ((self.entry_price - price) * self.short_vol
                     if self.in_short and self.entry_price else 0.0)
        return self.cash + long_val + short_val

    def current_gain(self, price: float) -> Optional[float]:
        if self.in_long and self.entry_price:
            return (price - self.entry_price) / self.entry_price * 100
        if self.in_short and self.entry_price:
            return (self.entry_price - price) / self.entry_price * 100
        return None

# ── Strategy ──────────────────────────────────────────────────────────────────
def run_bot():
    while True:
        account = PaperAccount()
        prices  = []
        start   = time.time()

        with state_lock:
            state.update(
                session_started_at=datetime.now(timezone.utc).isoformat(),
                tick=0, trades=[], cash=START_GBP, btc=0.0,
                pending_order=None, position_type=None,
                long_trades=0, short_trades=0,
            )

        log.info("NEW SESSION | pair=%s tp=±%.2f%% sl=±%.2f%% maker=%.2f%% taker=%.2f%%",
                 PAIR, TP_PCT*100, SL_PCT*100, FEE_MAKER*100, FEE_TAKER*100)

        while time.time() - start < SESSION_SEC:
            try:
                bid, ask, mid = get_prices(PAIR)
            except Exception as exc:
                log.warning("Price fetch failed: %s", exc)
                time.sleep(POLL_SEC); continue

            prices.append(mid)
            move_pct = (mid - prices[-2]) / prices[-2] * 100 if len(prices) >= 2 else 0.0

            # ── Pending limit fills ────────────────────────────────────────────
            if account.has_pending:
                side = account.pending["side"]
                if side == "long":
                    account.try_fill_buy(bid, ask)
                else:
                    account.try_fill_short(bid, ask)

            # ── Manage open LONG ───────────────────────────────────────────────
            elif account.in_long:
                g = (mid - account.entry_price) / account.entry_price
                if mid >= account.tp_target:
                    account.close_long_limit(mid)
                elif mid <= account.sl_level:
                    account.close_long_market(mid, "STOP-LOSS↑ (market)")
                elif len(prices) >= 3 and prices[-1] < prices[-2] < prices[-3]:
                    if mid >= account.break_even:
                        account.close_long_market(mid, "REVERSAL↓ (market)")
                    else:
                        log.info("tick=%-3d £%.2f %+.3f%% → LONG hold (%+.3f%% | TP=£%.2f)",
                                 len(prices), mid, move_pct, g*100, account.tp_target)
                else:
                    log.info("tick=%-3d £%.2f %+.3f%% → LONG hold (%+.3f%%)",
                             len(prices), mid, move_pct, g*100)

            # ── Manage open SHORT ──────────────────────────────────────────────
            elif account.in_short:
                g = (account.entry_price - mid) / account.entry_price
                if mid <= account.tp_target:
                    account.close_short_limit(mid)
                elif mid >= account.sl_level:
                    account.close_short_market(mid, "STOP-LOSS↓ (market)")
                elif len(prices) >= 3 and prices[-1] > prices[-2] > prices[-3]:
                    if mid <= account.break_even:
                        account.close_short_market(mid, "REVERSAL↑ (market)")
                    else:
                        log.info("tick=%-3d £%.2f %+.3f%% → SHORT hold (%+.3f%% | TP=£%.2f)",
                                 len(prices), mid, move_pct, g*100, account.tp_target)
                else:
                    log.info("tick=%-3d £%.2f %+.3f%% → SHORT hold (%+.3f%%)",
                             len(prices), mid, move_pct, g*100)

            # ── Entry signals ──────────────────────────────────────────────────
            elif len(prices) >= 3:
                up1   = prices[-1] > prices[-2]; up2   = prices[-2] > prices[-3]
                dn1   = prices[-1] < prices[-2]; dn2   = prices[-2] < prices[-3]
                cum_u = (prices[-1] - prices[-3]) / prices[-3]
                cum_d = (prices[-3] - prices[-1]) / prices[-3]

                if up1 and up2 and cum_u >= MOMENTUM_MIN:
                    account.place_limit_buy(bid)
                elif dn1 and dn2 and cum_d >= MOMENTUM_MIN:
                    account.place_limit_short(ask)
                else:
                    reason = ("not sustained" if not ((up1 and up2) or (dn1 and dn2))
                              else f"move {max(cum_u,cum_d)*100:.3f}% < min")
                    log.info("tick=%-3d £%.2f %+.3f%% → no entry (%s)",
                             len(prices), mid, move_pct, reason)
            else:
                log.info("tick=%-3d £%.2f → collecting baseline...", len(prices), mid)

            # ── Sync shared state ──────────────────────────────────────────────
            pv          = account.portfolio_value(mid)
            gain        = account.current_gain(mid)
            session_pnl = sum(t["net_pnl"] for t in account.trades
                              if t["net_pnl"] is not None)
            total_fees  = sum(t["fee"] for t in account.trades)
            longs       = sum(1 for t in account.trades
                              if t["direction"] == "LONG" and t["side"] == "SELL")
            shorts      = sum(1 for t in account.trades
                              if t["direction"] == "SHORT" and t["side"] == "BUY")

            with state_lock:
                state.update(
                    tick=len(prices), last_price=mid, last_bid=bid, last_ask=ask,
                    position_type=account.position_type,
                    entry_price=account.entry_price,
                    tp_target=account.tp_target,
                    sl_level=account.sl_level,
                    current_gain_pct=gain,
                    pending_order=account.pending,
                    cash=round(account.cash, 2), btc=account.btc,
                    portfolio_value=round(pv, 2),
                    trades=list(account.trades),
                    session_pnl=round(session_pnl, 4),
                    total_fees=round(total_fees, 4),
                    long_trades=longs, short_trades=shorts,
                    status=("long 📈" if account.in_long
                            else "short 📉" if account.in_short
                            else "pending..." if account.has_pending
                            else "watching"),
                )

            if len(prices) > 100: prices = prices[-50:]
            time.sleep(POLL_SEC)

        # ── Session end — close any open position ──────────────────────────────
        account.pending = None
        if account.in_long:
            _, _, mid = get_prices(PAIR)
            account.close_long_market(mid, "SESSION-END (market)")
        elif account.in_short:
            _, _, mid = get_prices(PAIR)
            account.close_short_market(mid, "SESSION-END (market)")

        log.info("Session complete. Restarting in 30s...")
        with state_lock: state["status"] = "restarting..."
        time.sleep(30)

# ── Status page ───────────────────────────────────────────────────────────────
def render_html(s):
    rows = ""
    for t in reversed(s["trades"]):
        is_open  = t["net_pnl"] is None
        pnl_str  = f"£{t['net_pnl']:+.4f}" if not is_open else "open"
        pnl_col  = ("#2ecc71" if (t["net_pnl"] or 0) > 0
                    else "#e74c3c" if (t["net_pnl"] or 0) < 0 else "#888")
        dir_col  = "#2ecc71" if t["direction"] == "LONG" else "#e74c3c"
        badge    = (f'<span style="font-size:10px;padding:2px 5px;border-radius:3px;'
                    f'background:{"#1a3a1a" if t["direction"]=="LONG" else "#3a1a1a"};'
                    f'color:{dir_col}">{t["direction"]}</span>')
        rows += f"""<tr>
          <td>{t['time']}</td>
          <td>{t['side']} {badge}</td>
          <td style="font-size:10px;color:#555">{t.get('order','')}</td>
          <td>£{t['price']:,.2f}</td>
          <td>{t['volume']:.5f}</td>
          <td>£{t['fee']:.4f}</td>
          <td style="color:{pnl_col};font-weight:bold">{pnl_str}</td>
          <td style="font-size:11px;color:#666">{t['reason']}</td>
        </tr>"""

    pnl      = s["session_pnl"]
    pnl_col  = "#2ecc71" if pnl >= 0 else "#e74c3c"
    pt       = s.get("position_type")
    pos_col  = "#2ecc71" if pt == "long" else "#e74c3c" if pt == "short" else (
               "#f39c12" if s["pending_order"] else "#444")
    gain_str = f"{s['current_gain_pct']:+.3f}%" if s["current_gain_pct"] is not None else "—"
    tp_str   = f"£{s['tp_target']:,.2f}" if s["tp_target"] else "—"
    sl_str   = f"£{s['sl_level']:,.2f}"  if s["sl_level"]  else "—"
    pend_str = (f"£{s['pending_order']['limit']:,.2f} ({s['pending_order']['side'].upper()})"
                if s["pending_order"] else "—")
    spread   = ((s["last_ask"] - s["last_bid"]) / s["last_bid"] * 100) if s["last_bid"] else 0
    pv_diff  = s["portfolio_value"] - START_GBP
    pv_col   = "#2ecc71" if pv_diff >= 0 else "#e74c3c"

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="15">
  <title>{s['pair']} Bot</title>
  <style>
    *     {{ box-sizing:border-box;margin:0;padding:0 }}
    body  {{ font-family:'Courier New',monospace;background:#0b0b18;color:#ccc;padding:24px }}
    h1    {{ color:#e2b96f;font-size:20px;margin-bottom:4px }}
    .sub  {{ color:#444;font-size:12px;margin-bottom:22px }}
    .grid {{ display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:22px }}
    .card {{ background:#111128;border:1px solid #1c1c38;border-radius:8px;padding:14px }}
    .lbl  {{ color:#444;font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:5px }}
    .val  {{ font-size:18px;font-weight:bold }}
    .section {{ margin-bottom:10px;color:#e2b96f;font-size:11px;text-transform:uppercase;letter-spacing:2px }}
    table {{ width:100%;border-collapse:collapse;background:#111128;border:1px solid #1c1c38;border-radius:8px;overflow:hidden }}
    th    {{ background:#090916;padding:9px 11px;text-align:left;font-size:10px;color:#444;text-transform:uppercase;letter-spacing:1px }}
    td    {{ padding:8px 11px;border-bottom:1px solid #161630;font-size:12px }}
    tr:last-child td {{ border:none }}
    .pulse{{ animation:pulse 2s infinite }}
    @keyframes pulse{{ 0%,100%{{opacity:1}}50%{{opacity:.3}} }}
  </style>
</head>
<body>
  <h1>📈📉 {s['pair']} Momentum Bot — Long &amp; Short</h1>
  <div class="sub">
    Kraken Pro · Limit orders · maker {FEE_MAKER*100:.2f}% taker {FEE_TAKER*100:.2f}% margin {MARGIN_OPEN*100:.2f}% open +{MARGIN_4H*100:.2f}%/4h &nbsp;·&nbsp;
    Bid £{s['last_bid']:,.2f} / Ask £{s['last_ask']:,.2f} / Spread {spread:.4f}% &nbsp;·&nbsp;
    <span style="color:{pos_col}" class="pulse">● {s['status']}</span>
  </div>

  <div class="section">Performance</div>
  <div class="grid" style="margin-top:8px">
    <div class="card"><div class="lbl">Last Price</div><div class="val">£{s['last_price']:,.2f}</div></div>
    <div class="card"><div class="lbl">Portfolio Value</div><div class="val" style="color:{pv_col}">£{s['portfolio_value']:,.2f}</div></div>
    <div class="card"><div class="lbl">Session P&amp;L</div><div class="val" style="color:{pnl_col}">£{pnl:+.2f}</div></div>
    <div class="card"><div class="lbl">Total Fees</div><div class="val" style="color:#e67e22">£{s['total_fees']:.2f}</div></div>
    <div class="card"><div class="lbl">Long Closes</div><div class="val" style="color:#2ecc71">{s['long_trades']}</div></div>
    <div class="card"><div class="lbl">Short Closes</div><div class="val" style="color:#e74c3c">{s['short_trades']}</div></div>
    <div class="card"><div class="lbl">Cash</div><div class="val">£{s['cash']:,.2f}</div></div>
    <div class="card"><div class="lbl">Tick</div><div class="val">#{s['tick']}</div></div>
  </div>

  <div class="section" style="margin-top:18px">Position</div>
  <div class="grid" style="margin-top:8px">
    <div class="card"><div class="lbl">Direction</div>
      <div class="val" style="color:{pos_col}">
        {('LONG ▲' if pt=='long' else 'SHORT ▼' if pt=='short' else 'FLAT')}
      </div>
    </div>
    <div class="card"><div class="lbl">Unrealised Gain</div><div class="val" style="color:{pos_col}">{gain_str}</div></div>
    <div class="card"><div class="lbl">Entry Price</div><div class="val">{"£{:,.2f}".format(s['entry_price']) if s['entry_price'] else '—'}</div></div>
    <div class="card"><div class="lbl">TP Target</div><div class="val" style="color:#2ecc71">{tp_str}</div></div>
    <div class="card"><div class="lbl">Stop Loss</div><div class="val" style="color:#e74c3c">{sl_str}</div></div>
    <div class="card"><div class="lbl">Pending Order</div><div class="val" style="color:#f39c12;font-size:14px">{pend_str}</div></div>
  </div>

  <div class="section" style="margin-top:18px">Trade Log</div>
  <table style="margin-top:8px">
    <tr><th>Time</th><th>Side</th><th>Order</th><th>Price</th><th>Volume</th><th>Fee</th><th>Net P&amp;L</th><th>Reason</th></tr>
    {"".join(rows) or "<tr><td colspan='8' style='text-align:center;color:#333;padding:20px'>No trades yet</td></tr>"}
  </table>
</body>
</html>"""

class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with state_lock: s = dict(state)
        html = (render_html(s) if s["last_price"]
                else "<html><body style='background:#0b0b18;color:#eee;font-family:monospace;padding:30px'><h2>⏳ Starting...</h2></body></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def log_message(self, *args): pass

# ── Entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Starting | pair=%s port=%d long+short enabled", PAIR, PORT)
    threading.Thread(target=run_bot, daemon=True).start()
    log.info("Status page → http://0.0.0.0:%d", PORT)
    HTTPServer(("0.0.0.0", PORT), StatusHandler).serve_forever()
