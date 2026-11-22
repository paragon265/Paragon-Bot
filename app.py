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
        s.strip() for s in os.getenv("CUSTOM_SYMBOLS", "BTC/USDT:USDT,ETH/USDT:USDT").split(",")
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
    conn.execute("""CREATE TABLE IF NOT EXISTS api_keys (
                    id INTEGER PRIMARY KEY,
                    label TEXT,
                    api_key TEXT,
                    secret_key TEXT,
                    passphrase TEXT
                    )""")
    conn.commit()
    conn.close()


def encrypt_value(value: str) -> str:
    return cipher.encrypt(value.encode()).decode()


def decrypt_value(token: str) -> str:
    return cipher.decrypt(token.encode()).decode()


def save_api_keys(label: str, api_key: str, secret_key: str, passphrase: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "DELETE FROM api_keys WHERE label = ?", (label,)
    )
    conn.execute(
        "INSERT INTO api_keys (label, api_key, secret_key, passphrase) VALUES (?, ?, ?, ?)",
        (
            label,
            encrypt_value(api_key),
            encrypt_value(secret_key),
            encrypt_value(passphrase),
        ),
    )
    conn.commit()
    conn.close()


def load_api_keys(label: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        """
        SELECT api_key, secret_key, passphrase
        FROM api_keys
        WHERE label = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (label,),
    ).fetchone()
    conn.close()
    if row:
        return decrypt_value(row[0]), decrypt_value(row[1]), decrypt_value(row[2])
    return None, None, None


def add_log(action: str, symbol: str, detail: str, pnl: float = 0.0):
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
    db_key, db_secret, db_passphrase = load_api_keys("bitget_demo")
    exchange = ccxt.bitget({
        "apiKey": db_key or os.getenv("BITGET_API_KEY", ""),
        "secret": db_secret or os.getenv("BITGET_API_SECRET", ""),
        "password": db_passphrase or os.getenv("BITGET_API_PASSPHRASE", ""),
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",
        },
    })
    exchange.set_sandbox_mode(True)
    return exchange


def resolve_symbols(exchange: ccxt.bitget) -> list[str]:
    markets = exchange.load_markets()
    all_perps = []
    for sym, m in markets.items():
        if m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("active"):
            all_perps.append(sym)

    if not STATE["scan_all_top"]:
        valid = [s for s in STATE["custom_symbols"] if s in all_perps]
        return valid if valid else ["BTC/USDT:USDT", "ETH/USDT:USDT"]

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
        total_pnl = 0.0
        daily_pnl_list = []

        try:
            exchange = get_bitget_demo()
            symbols = resolve_symbols(exchange)

            db_key, db_secret, db_passphrase = load_api_keys("bitget_demo")
            has_keys = bool(
                (db_key and db_secret and db_passphrase)
                or (
                    os.getenv("BITGET_API_KEY")
                    and os.getenv("BITGET_API_SECRET")
                    and os.getenv("BITGET_API_PASSPHRASE")
                )
            )

            if has_keys:
                try:
                    bal = exchange.fetch_balance({"type": "swap"})
                    balance_usdt = bal.get("total", {}).get("USDT")
                    equity_usdt = bal.get("total", {}).get("total") or balance_usdt

                    raw_positions = exchange.fetch_positions()
                    for p in raw_positions:
                        contracts = float(p.get("contracts") or 0)
                        if contracts > 0:
                            pnl = float(p.get("unrealizedPnl") or 0.0)
                            total_pnl += pnl
                            open_positions.append(
                                {
                                    "symbol": p.get("symbol"),
                                    "side": (p.get("side") or "").upper(),
                                    "contracts": contracts,
                                    "entryPrice": float(p.get("entryPrice") or 0),
                                    "markPrice": float(p.get("markPrice") or 0),
                                    "unrealizedPnl": round(pnl, 4),
                                    "leverage": p.get("leverage") or leverage,
                                    "marginMode": p.get("marginMode") or margin_mode,
                                }
                            )
                except Exception as e:
                    add_log("BALANCE/POS ERR", "ACCOUNT", str(e))

                try:
                    trades = exchange.fetch_my_trades(limit=40)
                    pnl_by_day = {}
                    for t in trades:
                        info = t.get("info", {})
                        rp = float(info.get("profit") or info.get("realizedPnl") or 0.0)
                        if rp != 0:
                            dt = t.get("datetime") or now_str[:10]
                            day = dt[:10]
                            pnl_by_day[day] = round(pnl_by_day.get(day, 0.0) + rp, 4)
                    daily_pnl_list = [
                        {"date": d, "pnl": round(total, 4)}
                        for d, total in sorted(pnl_by_day.items(), reverse=True)
                    ]
                except Exception:
                    pass

            active_symbols = {pos["symbol"] for pos in open_positions}

            for sym in symbols:
                try:
                    ohlcv_4h = exchange.fetch_ohlcv(sym, timeframe="4h", limit=120)
                    ohlcv_1h = exchange.fetch_ohlcv(sym, timeframe="1h", limit=120)
                    ohlcv_15m = exchange.fetch_ohlcv(sym, timeframe="15m", limit=120)

                    setup = analyze_mtf(ohlcv_4h, ohlcv_1h, ohlcv_15m, min_rr=min_rr)
                    setup["executed"] = False

                    if (
                        STATE["auto_trade"]
                        and has_keys
                        and setup["signal"] in ("LONG", "SHORT")
                        and sym not in active_symbols
                        and len(open_positions) < max_open
                        and balance_usdt
                        and balance_usdt > 5
                    ):
                        entry = float(setup["entry"])
                        sl = float(setup["stop_loss"])
                        tp = float(setup["take_profit"])
                        stop_dist = abs(entry - sl)
                        risk_usdt = float(balance_usdt) * (risk_pct / 100.0)

                        if stop_dist > 0 and risk_usdt > 0:
                            market = exchange.market(sym)
                            contract_size = float(market.get("contractSize") or 1.0)
                            raw_qty = risk_usdt / (stop_dist * contract_size)
                            amount = float(exchange.amount_to_precision(sym, raw_qty))

                            if amount > 0:
                                try:
                                    exchange.set_margin_mode(margin_mode, sym, {"leverage": leverage})
                                except Exception:
                                    pass
                                try:
                                    exchange.set_leverage(leverage, sym, {"marginMode": margin_mode})
                                except Exception:
                                    pass

                                order_side = "buy" if setup["signal"] == "LONG" else "sell"
                                params = {
                                    "triggerPrice": exchange.price_to_precision(sym, tp),
                                    "stopLossPrice": exchange.price_to_precision(sym, sl),
                                }
                                exchange.create_order(sym, "market", order_side, amount, None, params)
                                setup["executed"] = True
                                active_symbols.add(sym)
                                add_log(
                                    f"OPEN {setup['signal']}",
                                    sym,
                                    f"Qty: {amount} @ ~{entry} | SL: {sl} | TP: {tp}",
                                )

                    results.append(setup)

                except Exception as e:
                    add_log("SCAN ERR", sym, str(e))

            STATE["last_scan"] = now_str
            STATE["demo_balance_usdt"] = round(float(balance_usdt), 2) if balance_usdt is not None else None
            STATE["equity_usdt"] = round(float(equity_usdt), 2) if equity_usdt is not None else None
            STATE["unrealized_pnl_total"] = round(total_pnl, 2)
            STATE["open_positions"] = open_positions
            STATE["scan_results"] = results
            STATE["daily_pnl"] = daily_pnl_list
            STATE["last_error"] = None

        except Exception as e:
            STATE["last_error"] = str(e)
            add_log("GLB ERROR", "SYSTEM", str(e))

    return STATE


@app.route("/")
def index():
    db_key, db_secret, db_passphrase = load_api_keys("bitget_demo")
    has_keys = bool(
        (db_key and db_secret and db_passphrase)
        or (
            os.getenv("BITGET_API_KEY")
            and os.getenv("BITGET_API_SECRET")
            and os.getenv("BITGET_API_PASSPHRASE")
        )
    )
    return render_template("index.html", state=STATE, has_keys=has_keys)


@app.route("/status")
def status():
    return jsonify({"status": "alive", "last_scan": STATE["last_scan"]})


@app.route("/manual-scan", methods=["POST"])
def manual_scan():
    expected_token = os.getenv("SCAN_TOKEN", "")
    if expected_token:
        provided = request.headers.get("X-Scan-Token") or ""
        if provided != expected_token:
            return jsonify({"error": "Unauthorized"}), 401
    sync_and_scan()
    return jsonify(STATE)


@app.route("/toggle-autotrade", methods=["POST"])
def toggle_autotrade():
    STATE["auto_trade"] = not STATE["auto_trade"]
    add_log(
        "MODE",
        "SYSTEM",
        f"Auto-Trade set to {'ENABLED' if STATE['auto_trade'] else 'PAUSED (Monitor Only)'}",
    )
    return jsonify({"auto_trade": STATE["auto_trade"]})


@app.route("/update-pairs", methods=["POST"])
def update_pairs():
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "custom")
    if mode == "all_top":
        STATE["scan_all_top"] = True
        limit = int(data.get("top_limit", STATE["top_pairs_limit"]))
        STATE["top_pairs_limit"] = max(5, min(limit, 50))
        add_log("PAIR MODE", "SYSTEM", f"Switched to Top {STATE['top_pairs_limit']} USDT Perpetuals")
    else:
        STATE["scan_all_top"] = False
        raw_pairs = data.get("custom_pairs", "")
        parsed = [p.strip().upper() for p in raw_pairs.split(",") if p.strip()]
        normalized = [p if ":" in p else f"{p}:USDT" if p.endswith("USDT") else f"{p}/USDT:USDT" for p in parsed]
        STATE["custom_symbols"] = normalized if normalized else ["BTC/USDT:USDT", "ETH/USDT:USDT"]
        add_log("PAIR MODE", "SYSTEM", f"Active pairs: {', '.join(STATE['custom_symbols'])}")
    return jsonify({
        "scan_all_top": STATE["scan_all_top"],
        "top_pairs_limit": STATE["top_pairs_limit"],
        "custom_symbols": STATE["custom_symbols"],
    })


@app.route("/close-position", methods=["POST"])
def close_position():
    data = request.get_json(silent=True) or {}
    symbol = data.get("symbol")
    side = (data.get("side") or "").upper()
    contracts = float(data.get("contracts") or 0)
    if not symbol or contracts <= 0:
        return jsonify({"error": "Invalid position parameters"}), 400
    try:
        exchange = get_bitget_demo()
        close_side = "sell" if side == "LONG" else "buy"
        exchange.create_order(symbol, "market", close_side, contracts, None, {"reduceOnly": True})
        add_log("MANUAL CLOSE", symbol, f"Closed {contracts} contracts from dashboard")
        return jsonify({"status": "closed"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/save-keys", methods=["POST"])
def save_keys_route():
    data = request.get_json(silent=True) or {}
    api_key = data.get("api_key", "")
    secret_key = data.get("secret_key", "")
    passphrase = data.get("passphrase", "")
    save_api_keys("bitget_demo", api_key, secret_key, passphrase)
    add_log("KEYS", "SYSTEM", "Demo API keys saved")
    return jsonify({"status": "saved"})


@app.route("/get-keys", methods=["GET"])
def get_keys_route():
    has_keys = bool(load_api_keys("bitget_demo")[0])
    return jsonify({"has_keys": has_keys})


if __name__ == "__main__":
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
init_db()
