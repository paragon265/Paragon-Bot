import os
import threading
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template, request
import ccxt
from strategy_mtf import analyze_mtf
import sqlite3
from cryptography.fernet import Fernet

app = Flask(__name__)
SCAN_LOCK = threading.Lock()

STATE = {
    "last_scan": None,
    "auto_trade": os.getenv("AUTO_TRADE", "false").lower() == "true",
    "scan_all_top": os.getenv("SCAN_ALL_TOP", "false").lower() == "true",
    "top_pairs_limit": int(os.getenv("TOP_PAIRS_LIMIT", "15")),
    "custom_symbols": [
        s.strip()
        for s in os.getenv("SYMBOLS", "BTC/USDT:USDT,BGB/USDT:USDT").split(",")
        if s.strip()
    ],
    "demo_balance_usdt": None,
    "equity_usdt": None,
    "unrealized_pnl_total": 0.0,
    "open_positions": [],
    "scan_results": [],
    "execution_logs": [],
    "daily_pnl": [],
    "last_error": None,
}

DB_PATH = "keys.db"
FERNET_KEY = os.getenv("FERNET_KEY") or Fernet.generate_key().decode()
cipher = Fernet(FERNET_KEY.encode())

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS api_keys (id INTEGER PRIMARY KEY, label TEXT, api_key TEXT, secret_key TEXT)")
    conn.commit()
    conn.close()

init_db()
def encrypt_value(value: str) -> str:
    return cipher.encrypt(value.encode()).decode()

def decrypt_value(token: str) -> str:
    return cipher.decrypt(token.encode()).decode()

def save_api_keys(label: str, api_key: str, secret_key: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM api_keys WHERE label = ?", (label,))
    conn.execute(
        "INSERT INTO api_keys (label, api_key, secret_key) VALUES (?, ?, ?)",
        (label, encrypt_value(api_key), encrypt_value(secret_key)),
    )
    conn.commit()
    conn.close()

def load_api_keys(label: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT api_key, secret_key FROM api_keys WHERE label = ? ORDER BY id DESC LIMIT 1",
        (label,),
    ).fetchone()
    conn.close()
    if not row:
        return None, None
    return decrypt_value(row[0]), decrypt_value(row[1])

def _add_log(action: str, symbol: str, detail: str, pnl: float = 0.0):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    STATE["execution_logs"].insert(
        0,
        {
            "time": now_str,
            "action": action,
            "symbol": symbol,
            "detail": detail,
            "pnl": round(pnl, 4),
        },
    )
    STATE["execution_logs"] = STATE["execution_logs"][:60]


def get_bitget_demo() -> ccxt.bitget:
    db_key, db_secret = load_api_keys("bitget_demo")
    exchange = ccxt.bitget({
        "apiKey": db_key or os.getenv("BITGET_API_KEY", ""),
        "secret": db_secret or os.getenv("BITGET_API_SECRET", ""),
        "password": os.getenv("BITGET_API_PASSPHRASE", ""),
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",
        },
    })
    exchange.set_sandbox_mode(True)
    return exchange


def resolve_symbols(exchange: ccxt.bitget) -> list[str]:
    markets = exchange.load_markets()
    all_perps = [
        sym
        for sym, m in markets.items()
        if m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("active", True)
    ]

    if not STATE["scan_all_top"]:
        valid = [s for s in STATE["custom_symbols"] if s in all_perps]
        return valid if valid else ["BTC/USDT:USDT", "BGB/USDT:USDT"]

    try:
        tickers = exchange.fetch_tickers(all_perps)
        all_perps.sort(
            key=lambda s: float(tickers.get(s, {}).get("quoteVolume") or 0),
            reverse=True,
        )
    except Exception:
        pass

    return all_perps[: STATE["top_pairs_limit"]]


def sync_and_scan() -> dict:
    with SCAN_LOCK:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        min_rr = float(os.getenv("MIN_RR", "1.5"))
        leverage = int(os.getenv("LEVERAGE", "5"))
        margin_mode = os.getenv("MARGIN_MODE", "isolated")
        risk_pct = float(os.getenv("RISK_PCT", "1.0"))
        max_open = int(os.getenv("MAX_OPEN_POSITIONS", "2"))

        results = []
        open_positions = []
        balance_usdt = None
        equity_usdt = None
        total_upnl = 0.0

        try:
            exchange = get_bitget_demo()
            symbols = resolve_symbols(exchange)

            has_keys = bool(
                os.getenv("BITGET_API_KEY")
                and os.getenv("BITGET_API_SECRET")
                and os.getenv("BITGET_API_PASSPHRASE")
            )

            if has_keys:
                bal = exchange.fetch_balance({"type": "swap", "productType": "USDT-FUTURES"})
                usdt_info = bal.get("USDT", {})
                balance_usdt = usdt_info.get("free")
                equity_usdt = usdt_info.get("total") or balance_usdt

                raw_positions = exchange.fetch_positions()
                for p in raw_positions:
                    contracts = float(p.get("contracts") or 0)
                    if contracts > 0:
                        upnl = float(p.get("unrealizedPnl") or 0.0)
                        total_upnl += upnl
                        open_positions.append({
                            "symbol": p.get("symbol"),
                            "side": (p.get("side") or "").upper(),
                            "contracts": contracts,
                            "entryPrice": float(p.get("entryPrice") or 0),
                            "markPrice": float(p.get("markPrice") or 0),
                            "unrealizedPnl": round(upnl, 4),
                            "leverage": p.get("leverage") or leverage,
                            "marginMode": p.get("marginMode") or margin_mode,
                        })

                try:
                    trades = exchange.fetch_my_trades(limit=40)
                    pnl_by_day = {}
                    for t in trades:
                        info = t.get("info", {})
                        profit = float(info.get("profit") or info.get("realizedPnl") or 0.0)
                        if profit != 0:
                            dt = (t.get("datetime") or now_str)[:10]
                            pnl_by_day[dt] = round(pnl_by_day.get(dt, 0.0) + profit, 4)
                    STATE["daily_pnl"] = [
                        {"date": d, "futures": v, "total": v}
                        for d, v in sorted(pnl_by_day.items(), reverse=True)
                    ]
                except Exception:
                    pass

            active_symbols = {pos["symbol"] for pos in open_positions}

            for sym in symbols:
                ohlcv_1d = exchange.fetch_ohlcv(sym, "1d", limit=30)
                ohlcv_4h = exchange.fetch_ohlcv(sym, "4h", limit=50)
                ohlcv_1h = exchange.fetch_ohlcv(sym, "1h", limit=50)
                ohlcv_15m = exchange.fetch_ohlcv(sym, "15m", limit=50)

                setup = analyze_mtf(sym, ohlcv_1d, ohlcv_4h, ohlcv_1h, ohlcv_15m, min_rr=min_rr)
                setup["executed"] = False

                if (
                    STATE["auto_trade"]
                    and has_keys
                    and setup["signal"] in ("LONG", "SHORT")
                    and sym not in active_symbols
                    and len(open_positions) < max_open
                    and balance_usdt
                    and float(balance_usdt) > 0
                ):
                    entry = float(setup["entry"])
                    sl = float(setup["stop_loss"])
                    tp = float(setup["take_profit"])
                    stop_dist = abs(entry - sl)
                    risk_usdt = float(balance_usdt) * (risk_pct / 100.0)

                    market = exchange.market(sym)
                    contract_size = float(market.get("contractSize") or 1.0)
                    raw_qty = risk_usdt / (stop_dist * contract_size) if stop_dist > 0 else 0.0
                    amount = float(exchange.amount_to_precision(sym, max(raw_qty, 0.0)))

                    if amount > 0:
                        try:
                            exchange.set_margin_mode(margin_mode, sym, {"leverage": str(leverage)})
                        except Exception:
                            pass
                        try:
                            exchange.set_leverage(leverage, sym, {"marginMode": margin_mode})
                        except Exception:
                            pass

                        order_side = "buy" if setup["signal"] == "LONG" else "sell"
                        params = {
                            "stopLoss": {"triggerPrice": exchange.price_to_precision(sym, sl)},
                            "takeProfit": {"triggerPrice": exchange.price_to_precision(sym, tp)},
                        }
                        exchange.create_order(sym, "market", order_side, amount, None, params)
                        setup["executed"] = True
                        setup["order_size"] = amount
                        active_symbols.add(sym)
                        _add_log(
                            f"OPEN {setup['signal']} {leverage}x",
                            sym,
                            f"Size: {amount} | Entry: {entry} | SL: {sl} | TP: {tp} ({setup['rr']}R)",
                        )

                results.append(setup)

            STATE["last_scan"] = now_str
            STATE["demo_balance_usdt"] = round(float(balance_usdt), 2) if balance_usdt is not None else None
            STATE["equity_usdt"] = round(float(equity_usdt), 2) if equity_usdt is not None else None
            STATE["unrealized_pnl_total"] = round(total_upnl, 4)
            STATE["open_positions"] = open_positions
            STATE["scan_results"] = results
            STATE["last_error"] = None

        except Exception as exc:
            STATE["last_scan"] = now_str
            STATE["last_error"] = str(exc)
            _add_log("SCAN ERROR", "SYSTEM", str(exc))

        return {
            "status": "ok" if STATE["last_error"] is None else "error",
            "last_scan": STATE["last_scan"],
            "auto_trade": STATE["auto_trade"],
            "demo_balance_usdt": STATE["demo_balance_usdt"],
            "equity_usdt": STATE["equity_usdt"],
            "unrealized_pnl_total": STATE["unrealized_pnl_total"],
            "open_positions": STATE["open_positions"],
            "scan_results": STATE["scan_results"],
            "execution_logs": STATE["execution_logs"],
            "daily_pnl": STATE["daily_pnl"],
            "last_error": STATE["last_error"],
        }


@app.route("/")
def dashboard():
    has_keys = bool(
        os.getenv("BITGET_API_KEY")
        and os.getenv("BITGET_API_SECRET")
        and os.getenv("BITGET_API_PASSPHRASE")
    )
    return render_template("index.html", state=STATE, has_keys=has_keys)


@app.route("/health")
def health():
    return jsonify({"status": "alive", "last_scan": STATE["last_scan"]})


@app.route("/scan", methods=["GET", "POST"])
def scan():
    expected_token = os.getenv("SCAN_TOKEN", "")
    provided_token = request.args.get("token") or request.headers.get("X-Scan-Token") or ""
    if expected_token and provided_token != expected_token:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(sync_and_scan())


@app.route("/manual-scan", methods=["POST"])
def manual_scan():
    return jsonify(sync_and_scan())


@app.route("/toggle-autotrade", methods=["POST"])
def toggle_autotrade():
    STATE["auto_trade"] = not STATE["auto_trade"]
    _add_log(
        "MODE CHANGE",
        "SYSTEM",
        f"Auto-Trade set to {'ENABLED' if STATE['auto_trade'] else 'PAUSED (Monitor Only)'}",
    )
    return jsonify({"auto_trade": STATE["auto_trade"]})


@app.route("/update-pairs", methods=["POST"])
def update_pairs():
    payload = request.get_json(silent=True) or {}
    mode = payload.get("mode", "custom")
    if mode == "all_top":
        STATE["scan_all_top"] = True
        STATE["top_pairs_limit"] = min(max(int(payload.get("limit", 15)), 2), 40)
        _add_log("PAIR MODE", "ALL PERPS", f"Switched to Top {STATE['top_pairs_limit']} USDT-M Perpetuals")
    else:
        STATE["scan_all_top"] = False
        raw = payload.get("symbols", "BTC/USDT:USDT,BGB/USDT:USDT")
        parsed = [s.strip().upper() for s in raw.split(",") if s.strip()]
        normalized = [s if ":USDT" in s else f"{s}:USDT" if "/USDT" in s else f"{s}/USDT:USDT" for s in parsed]
        STATE["custom_symbols"] = normalized or ["BTC/USDT:USDT", "BGB/USDT:USDT"]
        _add_log("PAIR MODE", "CUSTOM", f"Active pairs: {', '.join(STATE['custom_symbols'])}")
    return jsonify({
        "scan_all_top": STATE["scan_all_top"],
        "top_pairs_limit": STATE["top_pairs_limit"],
        "custom_symbols": STATE["custom_symbols"],
    })


@app.route("/close-position", methods=["POST"])
def close_position():
    payload = request.get_json(silent=True) or {}
    symbol = payload.get("symbol")
    side = (payload.get("side") or "").upper()
    contracts = float(payload.get("contracts") or 0)
    if not symbol or contracts <= 0:
        return jsonify({"error": "Invalid position parameters"}), 400
    try:
        exchange = get_bitget_demo()
        close_side = "sell" if side == "LONG" else "buy"
        exchange.create_order(symbol, "market", close_side, contracts, None, {"reduceOnly": True})
        _add_log("MANUAL CLOSE", symbol, f"Closed {side} ({contracts} contracts) from dashboard")
        return jsonify(sync_and_scan())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@app.route("/save-keys", methods=["POST"])
def save_keys_route():
    data = request.json
    save_api_keys("bitget_demo", data.get("api_key", ""), data.get("secret_key", ""))
    return jsonify({"status": "keys saved"})

@app.route("/get-keys", methods=["GET"])
def get_keys_route():
    api_key, secret_key = load_api_keys("bitget_demo")
    return jsonify({"has_keys": bool(api_key)})
        
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
