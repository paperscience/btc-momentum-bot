#!/usr/bin/env python3
"""
BTC/GBP fee-aware momentum paper trader
Runs on Render as a Web Service (bot in background thread, status page on HTTP).
Uses Kraken public REST API — no external dependencies.
"""

import os, time, logging, json, threading
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from urllib.error import URLError
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────
PAIR           = os.getenv("PAIR",            "ETHGBP")
POLL_SEC       = int(os.getenv("POLL_SEC",    "15"))
SESSION_SEC    = int(os.getenv("SESSION_SEC", "86400"))
START_GBP      = float(os.getenv("START_GBP", "10000"))
FEE_RATE       = float(os.getenv("FEE_RATE",  "0.0026"))
TP_PCT         = float(os.getenv("TP_PCT",    "0.0070"))
SL_PCT         = float(os.getenv("SL_PCT",    "0.0040"))
MOMENTUM_MIN   = float(os.getenv("MOMENTUM_MIN", "0.0003"))
TRADE_FRAC     = float(os.getenv("TRADE_FRAC", "0.90"))
PORT           = int(os.getenv("PORT",        "8080"))
KRAKEN_API     = "https://api.kraken.com/0/public"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("momentum")

# ── Shared state (bot → status page) ─────────────────────────────────────────
state = {
    "pair": PAIR,
    "started_at": datetime.now(timezone.utc).isoformat(),
    "session_started_at": None,
    "tick": 0,
    "last_price": None,
    "last_tick_at": None,
    "in_position": False,
    "entry_price": None,
    "current_gain_pct": None,
    "cash": START_GBP,
    "btc": 0.0,
    "portfolio_value": START_GBP,
    "trades": [],
    "session_pnl": 0.0,
    "total_fees": 0.0,
    "status": "starting...",
}
state_lock = threading.Lock()

# ── Kraken REST helpers ───────────────────────────────────────────────────────
def get_ticker(pair: str) -> dict:
    url = f"{KRAKEN_API}/Ticker?{urlencode({'pair': pair})}"
    req = Request(url, headers={"User-Agent": "momentum-bot/2.0"})
    with urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    if data.get("error"):
        raise RuntimeError(f"Kraken error: {data['error']}")
    return next(iter(data["result"].values()))

def mid_price(pair: str) -> float:
    t = get_ticker(pair)
    return (float(t["b"][0]) + float(t["a"][0])) / 2

# ── Paper trading ─────────────────────────────────────────────────────────────
@dataclass
class PaperAccount:
    cash: float = START_GBP
    btc:  float = 0.0
    entry_price:  Optional[float] = None
    entry_cost:   Optional[float] = None
    break_even:   Optional[float] = None
    trades: list  = field(default_factory=list)
    start_cash:   float = START_GBP

    @property
    def in_position(self): return self.btc > 0.00001

    def buy_market(self, price):
        spend     = round(self.cash * TRADE_FRAC, 8)
        fee       = round(spend * FEE_RATE, 8)
        net_spend = spend + fee
        volume    = round((spend - fee) / price, 8)
        self.cash -= net_spend; self.btc += volume
        self.entry_price = price; self.entry_cost = spend
        self.break_even  = price * (1 + FEE_RATE * 2)
        t = dict(side="BUY", time=_ts(), price=price, volume=volume,
                 fee=fee, net_pnl=None, reason="MOMENTUM")
        self.trades.append(t)
        log.info("BUY  %.6f @ £%.2f | fee=£%.4f | break-even=£%.2f",
                 volume, price, fee, self.break_even)
        return t

    def sell_market(self, price, reason):
        volume   = self.btc
        proceeds = round(volume * price, 8)
        fee      = round(proceeds * FEE_RATE, 8)
        net_recv = proceeds - fee
        gain_pct = (price - self.entry_price) / self.entry_price * 100
        net_pnl  = net_recv - self.entry_cost
        self.cash += net_recv; self.btc = 0.0
        self.entry_price = self.entry_cost = self.break_even = None
        t = dict(side="SELL", time=_ts(), price=price, volume=volume,
                 fee=fee, net_pnl=net_pnl, reason=reason)
        self.trades.append(t)
        log.info("SELL %.6f @ £%.2f | net_pnl=£%+.4f (%+.3f%%) | %s",
                 volume, price, net_pnl, gain_pct, reason)
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
            state["session_started_at"] = datetime.now(timezone.utc).isoformat()
            state["tick"] = 0
            state["trades"] = []
            state["cash"] = START_GBP
            state["btc"] = 0.0

        log.info("NEW SESSION | pair=%s tp=+%.2f%% sl=-%.2f%%", PAIR, TP_PCT*100, SL_PCT*100)

        while time.time() - start < SESSION_SEC:
            try:
                price = mid_price(PAIR)
            except Exception as exc:
                log.warning("Price fetch failed: %s", exc)
                time.sleep(POLL_SEC); continue

            prices.append(price)
            move_pct = (price - prices[-2]) / prices[-2] * 100 if len(prices) >= 2 else 0.0
            gain = ((price - account.entry_price) / account.entry_price * 100
                    if account.in_position else None)

            # EXIT
            if account.in_position:
                g = (price - account.entry_price) / account.entry_price
                if g >= TP_PCT:
                    account.sell_market(price, "TAKE-PROFIT")
                elif g <= -SL_PCT:
                    account.sell_market(price, "STOP-LOSS")
                elif len(prices) >= 3:
                    if prices[-1] < prices[-2] < prices[-3] and price >= account.break_even:
                        account.sell_market(price, "REVERSAL")
                    else:
                        log.info("tick=%-3d £%.2f %+.3f%% → hold (gain=%+.3f%%)",
                                 len(prices), price, move_pct, g*100)
                else:
                    log.info("tick=%-3d £%.2f %+.3f%% → hold (gain=%+.3f%%)",
                             len(prices), price, move_pct, g*100)

            # ENTRY
            elif len(prices) >= 3:
                up1 = prices[-1] > prices[-2]
                up2 = prices[-2] > prices[-3]
                cum = (prices[-1] - prices[-3]) / prices[-3]
                if up1 and up2 and cum >= MOMENTUM_MIN:
                    account.buy_market(price)
                else:
                    reason = ("momentum not sustained" if not (up1 and up2)
                              else f"rise {cum*100:.3f}% < min")
                    log.info("tick=%-3d £%.2f %+.3f%% → no entry (%s)",
                             len(prices), price, move_pct, reason)
            else:
                log.info("tick=%-3d £%.2f → collecting baseline...", len(prices), price)

            # Update shared state
            pv = account.portfolio_value(price)
            session_pnl = sum(t["net_pnl"] for t in account.trades
                              if t["side"] == "SELL" and t["net_pnl"])
            total_fees  = sum(t["fee"] for t in account.trades)
            with state_lock:
                state["tick"]            = len(prices)
                state["last_price"]      = price
                state["last_tick_at"]    = datetime.now(timezone.utc).isoformat()
                state["in_position"]     = account.in_position
                state["entry_price"]     = account.entry_price
                state["current_gain_pct"] = gain
                state["cash"]            = round(account.cash, 2)
                state["btc"]             = account.btc
                state["portfolio_value"] = round(pv, 2)
                state["trades"]          = list(account.trades)
                state["session_pnl"]     = round(session_pnl, 4)
                state["total_fees"]      = round(total_fees, 4)
                state["status"]          = "holding" if account.in_position else "watching"

            if len(prices) > 100: prices = prices[-50:]
            time.sleep(POLL_SEC)

        if account.in_position:
            price = mid_price(PAIR)
            account.sell_market(price, "SESSION-END")

        log.info("Session complete. Restarting in 30s...")
        with state_lock: state["status"] = "restarting..."
        time.sleep(30)

# ── Status page ───────────────────────────────────────────────────────────────
def render_html(s):
    trades_html = ""
    for t in reversed(s["trades"]):
        colour  = "#2ecc71" if t["side"] == "BUY" else "#e74c3c"
        pnl_str = f"£{t['net_pnl']:+.4f}" if t["net_pnl"] is not None else "—"
        trades_html += f"""
        <tr>
          <td>{t['time']}</td>
          <td style="color:{colour};font-weight:bold">{t['side']}</td>
          <td>£{t['price']:,.2f}</td>
          <td>{t['volume']:.6f}</td>
          <td>£{t['fee']:.4f}</td>
          <td>{pnl_str}</td>
          <td>{t['reason']}</td>
        </tr>"""

    pnl       = s["portfolio_value"] - START_GBP
    pnl_col   = "#2ecc71" if pnl >= 0 else "#e74c3c"
    gain_str  = (f"{s['current_gain_pct']:+.3f}%" if s["current_gain_pct"] is not None else "—")
    pos_col   = "#2ecc71" if s["in_position"] else "#95a5a6"

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="15">
  <title>{s['pair']} Momentum Bot</title>
  <style>
    body  {{ font-family: monospace; background:#1a1a2e; color:#eee; padding:30px; }}
    h1    {{ color:#e2b96f; margin-bottom:4px; }}
    .sub  {{ color:#888; margin-bottom:30px; font-size:13px; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:16px; margin-bottom:30px; }}
    .card {{ background:#16213e; border-radius:10px; padding:18px; }}
    .card .label {{ color:#888; font-size:12px; margin-bottom:6px; }}
    .card .value {{ font-size:22px; font-weight:bold; }}
    table {{ width:100%; border-collapse:collapse; background:#16213e; border-radius:10px; overflow:hidden; }}
    th    {{ background:#0f3460; padding:10px; text-align:left; font-size:12px; color:#aaa; }}
    td    {{ padding:9px 10px; border-bottom:1px solid #1a1a2e; font-size:13px; }}
    tr:last-child td {{ border-bottom:none; }}
    .dot  {{ display:inline-block; width:8px; height:8px; border-radius:50%; background:{pos_col}; margin-right:6px; }}
  </style>
</head>
<body>
  <h1>📈 {s['pair']} Momentum Bot</h1>
  <div class="sub">Auto-refreshes every 15s &nbsp;·&nbsp; Started {s['started_at'][:19]}Z &nbsp;·&nbsp; <span class="dot"></span>{s['status']}</div>

  <div class="grid">
    <div class="card">
      <div class="label">Last Price</div>
      <div class="value">£{s['last_price']:,.2f}</div>
    </div>
    <div class="card">
      <div class="label">Portfolio Value</div>
      <div class="value">£{s['portfolio_value']:,.2f}</div>
    </div>
    <div class="card">
      <div class="label">Session P&amp;L (closed)</div>
      <div class="value" style="color:{pnl_col}">£{s['session_pnl']:+.2f}</div>
    </div>
    <div class="card">
      <div class="label">Total Fees Paid</div>
      <div class="value" style="color:#e67e22">£{s['total_fees']:.2f}</div>
    </div>
    <div class="card">
      <div class="label">Position</div>
      <div class="value" style="color:{pos_col}">
        {"IN  " + gain_str if s['in_position'] else "FLAT"}
      </div>
    </div>
    <div class="card">
      <div class="label">Entry Price</div>
      <div class="value">{"£{:,.2f}".format(s['entry_price']) if s['entry_price'] else "—"}</div>
    </div>
    <div class="card">
      <div class="label">Cash</div>
      <div class="value">£{s['cash']:,.2f}</div>
    </div>
    <div class="card">
      <div class="label">Tick</div>
      <div class="value">#{s['tick']}</div>
    </div>
  </div>

  <table>
    <tr>
      <th>TIME</th><th>SIDE</th><th>PRICE</th><th>VOLUME</th>
      <th>FEE</th><th>NET P&amp;L</th><th>REASON</th>
    </tr>
    {"".join(trades_html) if trades_html else "<tr><td colspan='7' style='text-align:center;color:#555;padding:20px'>No trades yet</td></tr>"}
  </table>
</body>
</html>"""

class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with state_lock:
            s = dict(state)
        if s["last_price"] is None:
            html = "<html><body style='background:#1a1a2e;color:#eee;font-family:monospace;padding:30px'><h2>⏳ Bot starting, waiting for first tick...</h2></body></html>"
        else:
            html = render_html(s)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def log_message(self, *args): pass  # suppress HTTP access logs

# ── Entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Starting bot | pair=%s port=%d", PAIR, PORT)

    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    log.info("Status page → http://0.0.0.0:%d", PORT)
    server = HTTPServer(("0.0.0.0", PORT), StatusHandler)
    server.serve_forever()
