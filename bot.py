#!/usr/bin/env python3
"""
ETH/GBP fee-aware momentum paper trader — Kraken Pro limit order edition
Runs on Render as a Web Service (bot thread + HTTP status page).
No external dependencies.

Order logic:
  ENTRY  — limit BUY at current bid (maker fee 0.14%)
  TP     — limit SELL at entry × (1 + TP_PCT) (maker fee 0.14%)
  SL     — market SELL when price drops below SL level (taker fee 0.24%)
  REVERSAL — market SELL if 2 down ticks and above break-even (taker fee 0.24%)

Round-trip fees:
  Limit/Limit  (TP hit)   : 0.14% + 0.14% = 0.28%  break-even ~0.30%
  Limit/Market (SL/rev)   : 0.14% + 0.24% = 0.38%  break-even ~0.40%
  vs old market/market    : 0.26% + 0.26% = 0.52%
"""

import os, time, logging, json, threading
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────
PAIR         = os.getenv("PAIR",          "ETHGBP")
POLL_SEC     = int(os.getenv("POLL_SEC",  "15"))
SESSION_SEC  = int(os.getenv("SESSION_SEC","86400"))
START_GBP    = float(os.getenv("START_GBP","10000"))
FEE_MAKER    = float(os.getenv("FEE_MAKER","0.0014"))  # 0.14% Kraken Pro maker
FEE_TAKER    = float(os.getenv("FEE_TAKER","0.0024"))  # 0.24% Kraken Pro taker
TP_PCT       = float(os.getenv("TP_PCT",  "0.0050"))   # +0.50% TP (lower now fees are cheaper)
SL_PCT       = float(os.getenv("SL_PCT",  "0.0030"))   # -0.30% SL (tighter too)
MOMENTUM_MIN = float(os.getenv("MOMENTUM_MIN","0.0002"))# lower entry bar (was 0.0003)
TRADE_FRAC   = float(os.getenv("TRADE_FRAC","0.90"))
LIMIT_EXPIRY = int(os.getenv("LIMIT_EXPIRY","3"))      # cancel limit buy after N ticks unfilled
PORT         = int(os.getenv("PORT",      "8080"))
KRAKEN_API   = "https://api.kraken.com/0/public"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("momentum")

# ── Shared state ──────────────────────────────────────────────────────────────
state = {
    "pair": PAIR, "started_at": datetime.now(timezone.utc).isoformat(),
    "session_started_at": None, "tick": 0,
    "last_price": None, "last_bid": None, "last_ask": None,
    "in_position": False, "entry_price": None, "tp_target": None,
    "current_gain_pct": None, "pending_buy": None,
    "cash": START_GBP, "btc": 0.0, "portfolio_value": START_GBP,
    "trades": [], "session_pnl": 0.0, "total_fees": 0.0, "status": "starting...",
}
state_lock = threading.Lock()

# ── Kraken REST ───────────────────────────────────────────────────────────────
def get_ticker(pair: str) -> dict:
    url = f"{KRAKEN_API}/Ticker?{urlencode({'pair': pair})}"
    req = Request(url, headers={"User-Agent": "momentum-bot/3.0"})
    with urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    if data.get("error"):
        raise RuntimeError(f"Kraken error: {data['error']}")
    return next(iter(data["result"].values()))

def get_prices(pair: str):
    """Returns (bid, ask, mid)"""
    t   = get_ticker(pair)
    bid = float(t["b"][0])
    ask = float(t["a"][0])
    return bid, ask, (bid + ask) / 2

# ── Paper trading ─────────────────────────────────────────────────────────────
@dataclass
class PaperAccount:
    cash:        float = START_GBP
    btc:         float = 0.0
    entry_price: Optional[float] = None
    entry_cost:  Optional[float] = None
    break_even:  Optional[float] = None   # min sell price to cover both legs
    tp_target:   Optional[float] = None   # limit sell price
    pending_buy: Optional[dict]  = None   # {"limit": price, "spend": amount, "ticks": n}
    trades:      list = field(default_factory=list)
    start_cash:  float = START_GBP

    @property
    def in_position(self): return self.btc > 0.00001

    @property
    def has_pending_buy(self): return self.pending_buy is not None

    def place_limit_buy(self, bid: float):
        """Simulate placing a limit buy at the bid."""
        spend = round(self.cash * TRADE_FRAC, 8)
        self.pending_buy = {"limit": bid, "spend": spend, "ticks": 0}
        log.info("LIMIT BUY pending @ £%.2f (spend=£%.2f, expires in %d ticks)",
                 bid, spend, LIMIT_EXPIRY)

    def try_fill_buy(self, bid: float, ask: float) -> bool:
        """Fill limit buy if market bid <= our limit. Returns True if filled."""
        pb = self.pending_buy
        pb["ticks"] += 1

        if bid <= pb["limit"] or ask <= pb["limit"]:
            # Filled at our limit price (maker)
            fill_price = pb["limit"]
            spend      = pb["spend"]
            fee        = round(spend * FEE_MAKER, 8)
            net_spend  = spend + fee
            volume     = round((spend - fee) / fill_price, 8)

            self.cash       -= net_spend
            self.btc        += volume
            self.entry_price = fill_price
            self.entry_cost  = spend
            # break-even: worst case exit is taker (SL/reversal)
            self.break_even  = fill_price * (1 + FEE_MAKER + FEE_TAKER)
            self.tp_target   = round(fill_price * (1 + TP_PCT), 4)
            self.pending_buy = None

            t = dict(side="BUY", order="LIMIT", time=_ts(), price=fill_price,
                     volume=volume, fee=fee, net_pnl=None, reason="MOMENTUM")
            self.trades.append(t)
            log.info("BUY  FILLED %.6f @ £%.2f | fee=£%.4f (maker) | TP=£%.2f SL=£%.2f",
                     volume, fill_price, fee,
                     self.tp_target, fill_price * (1 - SL_PCT))
            return True

        if pb["ticks"] >= LIMIT_EXPIRY:
            log.info("LIMIT BUY expired (price moved away)")
            self.pending_buy = None

        return False

    def sell_limit_tp(self, price: float) -> dict:
        """TP hit — simulate limit sell fill at tp_target (maker fee)."""
        fill_price = self.tp_target
        return self._sell(fill_price, FEE_MAKER, "TAKE-PROFIT (limit)")

    def sell_market(self, price: float, reason: str) -> dict:
        """SL / reversal — market sell at current price (taker fee)."""
        return self._sell(price, FEE_TAKER, reason)

    def _sell(self, price: float, fee_rate: float, reason: str) -> dict:
        volume   = self.btc
        proceeds = round(volume * price, 8)
        fee      = round(proceeds * fee_rate, 8)
        net_recv = proceeds - fee
        gain_pct = (price - self.entry_price) / self.entry_price * 100
        net_pnl  = net_recv - self.entry_cost
        self.cash       += net_recv
        self.btc         = 0.0
        self.entry_price = self.entry_cost = self.break_even = self.tp_target = None
        order = "LIMIT" if "limit" in reason.lower() else "MARKET"
        t = dict(side="SELL", order=order, time=_ts(), price=price,
                 volume=volume, fee=fee, net_pnl=net_pnl, reason=reason)
        self.trades.append(t)
        log.info("SELL %s %.6f @ £%.2f | fee=£%.4f (%.2f%%) | net_pnl=£%+.4f (%+.3f%%)",
                 order, volume, price, fee, fee_rate*100, net_pnl, gain_pct)
        return t

    def portfolio_value(self, price): return self.cash + self.btc * price

def _ts(): return datetime.now(timezone.utc).strftime("%H:%M:%S")

# ── Strategy ──────────────────────────────────────────────────────────────────
def run_bot():
    while True:
        account = PaperAccount()
        prices  = []
        start   = time.time()

        with state_lock:
            state.update(session_started_at=datetime.now(timezone.utc).isoformat(),
                         tick=0, trades=[], cash=START_GBP, btc=0.0,
                         pending_buy=None, tp_target=None)

        log.info("NEW SESSION | pair=%s tp=+%.2f%% sl=-%.2f%% maker=%.2f%% taker=%.2f%%",
                 PAIR, TP_PCT*100, SL_PCT*100, FEE_MAKER*100, FEE_TAKER*100)

        while time.time() - start < SESSION_SEC:
            try:
                bid, ask, mid = get_prices(PAIR)
            except Exception as exc:
                log.warning("Price fetch failed: %s", exc)
                time.sleep(POLL_SEC); continue

            prices.append(mid)
            move_pct = (mid - prices[-2]) / prices[-2] * 100 if len(prices) >= 2 else 0.0

            # ── Check pending limit buy ────────────────────────────────────────
            if account.has_pending_buy:
                account.try_fill_buy(bid, ask)

            # ── EXIT (in position) ─────────────────────────────────────────────
            elif account.in_position:
                g = (mid - account.entry_price) / account.entry_price

                if mid >= account.tp_target:
                    # TP limit hit — fill at tp_target
                    account.sell_limit_tp(mid)
                elif mid <= account.entry_price * (1 - SL_PCT):
                    account.sell_market(mid, "STOP-LOSS (market)")
                elif len(prices) >= 3:
                    reversal = prices[-1] < prices[-2] < prices[-3]
                    if reversal and mid >= account.break_even:
                        account.sell_market(mid, "REVERSAL (market)")
                    else:
                        log.info("tick=%-3d £%.2f %+.3f%% → hold (gain=%+.3f%% | TP=£%.2f)",
                                 len(prices), mid, move_pct, g*100, account.tp_target)
                else:
                    log.info("tick=%-3d £%.2f %+.3f%% → hold (gain=%+.3f%%)",
                             len(prices), mid, move_pct, g*100)

            # ── ENTRY ──────────────────────────────────────────────────────────
            elif len(prices) >= 3:
                up1 = prices[-1] > prices[-2]
                up2 = prices[-2] > prices[-3]
                cum = (prices[-1] - prices[-3]) / prices[-3]
                if up1 and up2 and cum >= MOMENTUM_MIN:
                    account.place_limit_buy(bid)
                else:
                    reason = ("momentum not sustained" if not (up1 and up2)
                              else f"rise {cum*100:.3f}% < min")
                    log.info("tick=%-3d £%.2f %+.3f%% → no entry (%s)",
                             len(prices), mid, move_pct, reason)
            else:
                log.info("tick=%-3d £%.2f → collecting baseline...", len(prices), mid)

            # ── Update shared state ────────────────────────────────────────────
            pv = account.portfolio_value(mid)
            gain = ((mid - account.entry_price) / account.entry_price * 100
                    if account.in_position else None)
            session_pnl = sum(t["net_pnl"] for t in account.trades
                              if t["side"] == "SELL" and t["net_pnl"])
            total_fees  = sum(t["fee"] for t in account.trades)
            with state_lock:
                state.update(
                    tick=len(prices), last_price=mid, last_bid=bid, last_ask=ask,
                    last_tick_at=datetime.now(timezone.utc).isoformat(),
                    in_position=account.in_position,
                    entry_price=account.entry_price,
                    tp_target=account.tp_target,
                    current_gain_pct=gain,
                    pending_buy=account.pending_buy,
                    cash=round(account.cash, 2), btc=account.btc,
                    portfolio_value=round(pv, 2),
                    trades=list(account.trades),
                    session_pnl=round(session_pnl, 4),
                    total_fees=round(total_fees, 4),
                    status=("holding" if account.in_position
                            else "pending buy" if account.has_pending_buy
                            else "watching"),
                )

            if len(prices) > 100: prices = prices[-50:]
            time.sleep(POLL_SEC)

        # Close out at session end
        if account.has_pending_buy:
            account.pending_buy = None
        if account.in_position:
            _, _, mid = get_prices(PAIR)
            account.sell_market(mid, "SESSION-END (market)")

        log.info("Session complete. Restarting in 30s...")
        with state_lock: state["status"] = "restarting..."
        time.sleep(30)

# ── Status page ───────────────────────────────────────────────────────────────
def render_html(s):
    trades_rows = ""
    for t in reversed(s["trades"]):
        colour  = "#2ecc71" if t["side"] == "BUY" else "#e74c3c"
        pnl_str = f"£{t['net_pnl']:+.4f}" if t["net_pnl"] is not None else "—"
        order_badge = (f'<span style="font-size:10px;background:#0f3460;padding:2px 5px;'
                       f'border-radius:3px">{t.get("order","")}</span>')
        trades_rows += f"""
        <tr>
          <td>{t['time']}</td>
          <td style="color:{colour};font-weight:bold">{t['side']} {order_badge}</td>
          <td>£{t['price']:,.2f}</td>
          <td>{t['volume']:.6f}</td>
          <td>£{t['fee']:.4f}</td>
          <td style="color:{'#2ecc71' if t['net_pnl'] and t['net_pnl']>0 else '#e74c3c'}">{pnl_str}</td>
          <td style="font-size:12px;color:#aaa">{t['reason']}</td>
        </tr>"""

    pnl     = s["session_pnl"]
    pnl_col = "#2ecc71" if pnl >= 0 else "#e74c3c"
    pos_col = "#2ecc71" if s["in_position"] else ("#f39c12" if s["pending_buy"] else "#95a5a6")
    gain_str = f"{s['current_gain_pct']:+.3f}%" if s["current_gain_pct"] is not None else "—"
    tp_str   = f"£{s['tp_target']:,.2f}" if s["tp_target"] else "—"
    pending  = f"£{s['pending_buy']['limit']:,.2f}" if s["pending_buy"] else "—"
    spread   = ((s["last_ask"] - s["last_bid"]) / s["last_bid"] * 100) if s["last_bid"] else 0

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="15">
  <title>{s['pair']} Bot</title>
  <style>
    * {{ box-sizing:border-box; margin:0; padding:0; }}
    body  {{ font-family:'Courier New',monospace; background:#0d0d1a; color:#ddd; padding:24px; }}
    h1    {{ color:#e2b96f; font-size:22px; margin-bottom:4px; }}
    .sub  {{ color:#555; font-size:12px; margin-bottom:24px; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin-bottom:24px; }}
    .card {{ background:#12122a; border:1px solid #1e1e40; border-radius:8px; padding:16px; }}
    .lbl  {{ color:#555; font-size:11px; margin-bottom:6px; text-transform:uppercase; letter-spacing:1px; }}
    .val  {{ font-size:20px; font-weight:bold; }}
    .fees {{ color:#e67e22 !important; }}
    table {{ width:100%; border-collapse:collapse; background:#12122a; border-radius:8px; overflow:hidden; border:1px solid #1e1e40; }}
    th    {{ background:#0a0a1f; padding:10px 12px; text-align:left; font-size:11px; color:#555; letter-spacing:1px; text-transform:uppercase; }}
    td    {{ padding:9px 12px; border-bottom:1px solid #1a1a30; font-size:13px; }}
    tr:last-child td {{ border:none; }}
    .dot  {{ display:inline-block; width:8px; height:8px; border-radius:50%; background:{pos_col}; margin-right:5px; animation: pulse 2s infinite; }}
    @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:0.4}} }}
    .badge{{ font-size:10px; background:#1a1a40; padding:2px 6px; border-radius:3px; margin-left:6px; }}
  </style>
</head>
<body>
  <h1>📈 {s['pair']} Momentum Bot <span class="badge">Kraken Pro · Limit Orders</span></h1>
  <div class="sub">
    Auto-refreshes every 15s &nbsp;·&nbsp;
    Session started {s.get('session_started_at','')[:19]}Z &nbsp;·&nbsp;
    <span class="dot"></span>{s['status']} &nbsp;·&nbsp;
    Bid £{s['last_bid']:,.2f} / Ask £{s['last_ask']:,.2f} / Spread {spread:.4f}%
  </div>

  <div class="grid">
    <div class="card"><div class="lbl">Last Price</div><div class="val">£{s['last_price']:,.2f}</div></div>
    <div class="card"><div class="lbl">Portfolio Value</div><div class="val">£{s['portfolio_value']:,.2f}</div></div>
    <div class="card"><div class="lbl">Closed P&amp;L</div><div class="val" style="color:{pnl_col}">£{pnl:+.2f}</div></div>
    <div class="card"><div class="lbl">Fees Paid</div><div class="val fees">£{s['total_fees']:.2f}</div></div>
    <div class="card"><div class="lbl">Position</div><div class="val" style="color:{pos_col}">{"IN  "+gain_str if s['in_position'] else "PENDING" if s['pending_buy'] else "FLAT"}</div></div>
    <div class="card"><div class="lbl">TP Target</div><div class="val">{tp_str}</div></div>
    <div class="card"><div class="lbl">Pending Limit Buy</div><div class="val">{pending}</div></div>
    <div class="card"><div class="lbl">Cash</div><div class="val">£{s['cash']:,.2f}</div></div>
  </div>

  <table>
    <tr><th>Time</th><th>Side</th><th>Price</th><th>Volume</th><th>Fee</th><th>Net P&amp;L</th><th>Reason</th></tr>
    {"".join(trades_rows) or "<tr><td colspan='7' style='text-align:center;color:#333;padding:24px'>No trades yet</td></tr>"}
  </table>
</body>
</html>"""

class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with state_lock: s = dict(state)
        if s["last_price"] is None:
            html = "<html><body style='background:#0d0d1a;color:#eee;font-family:monospace;padding:30px'><h2>⏳ Starting up...</h2></body></html>"
        else:
            html = render_html(s)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def log_message(self, *args): pass

# ── Entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Starting | pair=%s port=%d maker=%.2f%% taker=%.2f%%",
             PAIR, PORT, FEE_MAKER*100, FEE_TAKER*100)
    threading.Thread(target=run_bot, daemon=True).start()
    log.info("Status page → http://0.0.0.0:%d", PORT)
    HTTPServer(("0.0.0.0", PORT), StatusHandler).serve_forever()
