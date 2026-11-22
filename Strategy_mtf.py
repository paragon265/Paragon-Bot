from typing import List, Dict


def _swing_highs(candles: List[list], left: int = 2, right: int = 2) -> List[tuple]:
    highs = []
    n = len(candles)
    for i in range(left, n - right):
        h = float(candles[i][2])
        if all(h >= float(candles[j][2]) for j in range(i - left, i + right + 1) if j != i):
            highs.append((i, h, candles[i][0]))
    return highs


def _swing_lows(candles: List[list], left: int = 2, right: int = 2) -> List[tuple]:
    lows = []
    n = len(candles)
    for i in range(left, n - right):
        l = float(candles[i][3])
        if all(l <= float(candles[j][3]) for j in range(i - left, i + right + 1) if j != i):
            lows.append((i, l, candles[i][0]))
    return lows


def analyze_mtf(
    symbol: str,
    ohlcv_1d: List[list],
    ohlcv_4h: List[list],
    ohlcv_1h: List[list],
    ohlcv_15m: List[list],
    min_rr: float = 1.5,
) -> Dict:
    if len(ohlcv_1d) < 10 or len(ohlcv_4h) < 20 or len(ohlcv_1h) < 25 or len(ohlcv_15m) < 25:
        return {"symbol": symbol, "signal": "NONE", "reason": "Insufficient candle history"}

    h4_highs = [float(c[2]) for c in ohlcv_4h[-30:]]
    h4_lows = [float(c[3]) for c in ohlcv_4h[-30:]]
    range_high_4h = max(h4_highs)
    range_low_4h = min(h4_lows)
    range_mid_4h = (range_high_4h + range_low_4h) / 2.0

    prev_1d = ohlcv_1d[-2]
    price_now = float(ohlcv_15m[-1][4])

    if price_now <= range_mid_4h and float(prev_1d[4]) >= float(prev_1d[1]):
        htf_bias = "BULLISH"
    elif price_now >= range_mid_4h and float(prev_1d[4]) <= float(prev_1d[1]):
        htf_bias = "BEARISH"
    else:
        htf_bias = "BULLISH" if price_now < range_mid_4h else "BEARISH"

    h1_swings_low = _swing_lows(ohlcv_1h[:-1], left=2, right=2)
    h1_swings_high = _swing_highs(ohlcv_1h[:-1], left=2, right=2)
    h1_last = ohlcv_1h[-1]
    h1_prev = ohlcv_1h[-2]

    bullish_sweep = False
    bearish_sweep = False
    sweep_level = None

    if h1_swings_low:
        recent_low = h1_swings_low[-1][1]
        if (float(h1_last[3]) < recent_low or float(h1_prev[3]) < recent_low) and float(h1_last[4]) > recent_low:
            if float(h1_last[4]) > float(h1_last[1]) or float(h1_last[4]) > float(h1_prev[2]):
                bullish_sweep = True
                sweep_level = recent_low

    if h1_swings_high:
        recent_high = h1_swings_high[-1][1]
        if (float(h1_last[2]) > recent_high or float(h1_prev[2]) > recent_high) and float(h1_last[4]) < recent_high:
            if float(h1_last[4]) < float(h1_last[1]) or float(h1_last[4]) < float(h1_prev[3]):
                bearish_sweep = True
                sweep_level = recent_high

    m15_recent = ohlcv_15m[-12:]
    m15_low = min(float(c[3]) for c in m15_recent)
    m15_high = max(float(c[2]) for c in m15_recent)

    if bullish_sweep and htf_bias in ("BULLISH", "NEUTRAL"):
        entry = price_now
        sl = min(m15_low, float(sweep_level)) * 0.9985
        risk = entry - sl
        tp = max(range_mid_4h, entry + (risk * min_rr))
        if risk > 0 and ((tp - entry) / risk) >= min_rr:
            return {
                "symbol": symbol,
                "signal": "LONG",
                "htf_bias": htf_bias,
                "range_4h": [round(range_low_4h, 4), round(range_mid_4h, 4), round(range_high_4h, 4)],
                "entry": round(entry, 4),
                "stop_loss": round(sl, 4),
                "take_profit": round(tp, 4),
                "rr": round((tp - entry) / risk, 2),
                "reason": "1H sell-side liquidity swept + 15m bullish POI aligned with 4H range",
            }

    if bearish_sweep and htf_bias in ("BEARISH", "NEUTRAL"):
        entry = price_now
        sl = max(m15_high, float(sweep_level)) * 1.0015
        risk = sl - entry
        tp = min(range_mid_4h, entry - (risk * min_rr))
        if risk > 0 and ((entry - tp) / risk) >= min_rr:
            return {
                "symbol": symbol,
                "signal": "SHORT",
                "htf_bias": htf_bias,
                "range_4h": [round(range_low_4h, 4), round(range_mid_4h, 4), round(range_high_4h, 4)],
                "entry": round(entry, 4),
                "stop_loss": round(sl, 4),
                "take_profit": round(tp, 4),
                "rr": round((entry - tp) / risk, 2),
                "reason": "1H buy-side liquidity swept + 15m bearish POI aligned with 4H range",
            }

    return {
        "symbol": symbol,
        "signal": "NONE",
        "htf_bias": htf_bias,
        "price": round(price_now, 4),
        "range_4h": [round(range_low_4h, 4), round(range_mid_4h, 4), round(range_high_4h, 4)],
        "reason": "Waiting for valid 1H sweep + confirmation and >=1.5R 15m POI",
    }
