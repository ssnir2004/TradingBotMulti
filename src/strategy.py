"""The single fixed strategy's decision logic - the exact same D1-D3
(daily)/I1-I3 (intraday) filter definitions and initial-stop rules
TradingBot's cycle.py uses for its default "Long Breakout Conservative"
strategy (see strategy_config.json), extracted here as pure functions (no
data fetching, no wall-clock "now") so scanner.py and executor.py's own
sizing math can never quietly drift apart from each other.

Long and short are exact mirrors, kept generic even though the seeded
strategy_config.json is long_only - a future rules change to direction
"short_only"/"both" needs no code change here.
"""
from datetime import time as dt_time

import pandas as pd


def _compute_rsi_series(closes: pd.Series, period: int) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.mask(avg_loss == 0, 100.0)


def _compute_rsi(closes: pd.Series, period: int) -> float | None:
    if len(closes) < period + 1:
        return None
    value = _compute_rsi_series(closes, period).iloc[-1]
    return float(value) if pd.notna(value) else None


def _compute_ema(closes: pd.Series, period: int) -> float | None:
    if len(closes) < period:
        return None
    value = closes.ewm(span=period, adjust=False).mean().iloc[-1]
    return float(value) if pd.notna(value) else None


def compute_atr(daily: pd.DataFrame, period: int = 14) -> float | None:
    """Wilder's ATR as of the last COMPLETE trading day in `daily` -
    excludes daily.iloc[-1] (today, still in progress)."""
    completed = daily.iloc[:-1]
    if len(completed) < period + 1:
        return None
    high, low, close = completed["High"], completed["Low"], completed["Close"]
    prev_close = close.shift(1)
    true_range = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    value = true_range.ewm(alpha=1 / period, min_periods=period, adjust=False).mean().iloc[-1]
    return float(value) if pd.notna(value) else None


def evaluate_filters(daily: pd.DataFrame, intraday: pd.DataFrame, rules: dict, side: str) -> dict:
    """The D1-D3/I1-I3 decision logic. `daily` must already end at the
    prior trading day relative to `intraday`'s last date (daily.iloc[-2]
    is "yesterday"). Returns a dict always carrying "pass" (bool); once
    evaluable also carries D1..I3 and price/rvol/gap_pct/stop_ref/atr for
    sizing and for the dashboard's scan table."""
    daily_filters = rules["daily_filters"]
    intraday_filters = rules["intraday_filters"]

    if len(daily) < 201:
        return {"pass": False, "side": side, "error": "not enough daily history"}
    prior_day = daily.iloc[-2]
    sma200 = daily["Close"].iloc[-201:-1].mean()

    if intraday.empty:
        return {"pass": False, "side": side, "error": "no intraday data"}

    current_price = float(intraday["Close"].iloc[-1])
    as_of_date = intraday.index[-1].date()
    today_bars = intraday[intraday.index.date == as_of_date]
    if today_bars.empty:
        return {"pass": False, "side": side, "error": "no bars for today yet"}

    premarket_bars = today_bars[today_bars.index.time < dt_time(9, 30)]
    regular_bars = today_bars[today_bars.index.time >= dt_time(9, 30)]

    prior_close = float(prior_day["Close"])
    gap_pct = (current_price - prior_close) / prior_close * 100 if prior_close else 0.0
    rsi_value = None

    if side == "long":
        d1 = current_price > float(prior_day["High"])
        d2 = float(prior_day["Close"]) > float(sma200)
        d3 = gap_pct >= daily_filters["D3_min_gap_pct_from_prior_close"]
        premarket_extreme = float(premarket_bars["High"].max()) if not premarket_bars.empty else float("-inf")
        i1 = current_price > premarket_extreme
        if "I2_rsi_above" in intraday_filters:
            rsi_value = _compute_rsi(intraday["Close"], intraday_filters.get("I2_rsi_period", 14))
            i2 = rsi_value is not None and rsi_value > intraday_filters["I2_rsi_above"]
        elif intraday_filters.get("I2_ema_above"):
            ema_value = _compute_ema(intraday["Close"], intraday_filters.get("I2_ema_period", 9))
            i2 = ema_value is not None and current_price > ema_value
        else:
            extreme_so_far = float(today_bars["High"].iloc[:-1].max()) if len(today_bars) > 1 else float("-inf")
            i2 = current_price > extreme_so_far
        stop_ref = float(regular_bars["Low"].min()) if not regular_bars.empty else float(today_bars["Low"].min())
    else:
        d1 = current_price < float(prior_day["Low"])
        d2 = float(prior_day["Close"]) < float(sma200)
        d3 = gap_pct <= -daily_filters["D3_min_gap_pct_down_from_prior_close"]
        premarket_extreme = float(premarket_bars["Low"].min()) if not premarket_bars.empty else float("inf")
        i1 = current_price < premarket_extreme
        if "I2_rsi_below" in intraday_filters:
            rsi_value = _compute_rsi(intraday["Close"], intraday_filters.get("I2_rsi_period", 14))
            i2 = rsi_value is not None and rsi_value < intraday_filters["I2_rsi_below"]
        elif intraday_filters.get("I2_ema_below"):
            ema_value = _compute_ema(intraday["Close"], intraday_filters.get("I2_ema_period", 9))
            i2 = ema_value is not None and current_price < ema_value
        else:
            extreme_so_far = float(today_bars["Low"].iloc[:-1].min()) if len(today_bars) > 1 else float("inf")
            i2 = current_price < extreme_so_far
        stop_ref = float(regular_bars["High"].max()) if not regular_bars.empty else float(today_bars["High"].max())

    atr_value = compute_atr(daily)

    lookback = intraday_filters["I3_rvol_lookback_days"]
    as_of_time = intraday.index[-1].time()
    prior_dates = sorted({d for d in intraday.index.date if d < as_of_date})[-lookback:]
    prior_volume_by_this_time = [
        float(intraday[(intraday.index.date == d) & (intraday.index.time <= as_of_time)]["Volume"].sum())
        for d in prior_dates
    ]
    avg_volume_by_this_time = (sum(prior_volume_by_this_time) / len(prior_volume_by_this_time)) if prior_volume_by_this_time else 0.0
    today_volume_so_far = float(today_bars["Volume"].sum())
    rvol = today_volume_so_far / avg_volume_by_this_time if avg_volume_by_this_time else 0.0
    i3 = rvol >= intraday_filters["I3_rvol_min"]

    passed = bool(d1 and d2 and d3 and i1 and i2 and i3)
    return {
        "pass": passed, "side": side,
        "D1": bool(d1), "D2": bool(d2), "D3": bool(d3), "I1": bool(i1), "I2": bool(i2), "I3": bool(i3),
        "price": current_price, "rvol": rvol, "gap_pct": gap_pct, "stop_ref": stop_ref, "rsi": rsi_value, "atr": atr_value,
    }


# exit.initial_stop_rule value -> the offset applied off stop_ref (a flat %
# off today's session low/high) or off entry price (an ATR multiple).
INITIAL_STOP_RULES = {
    "lod_minus_1pct": {"kind": "session_extreme", "multiplier": 0.99},
    "hod_plus_1pct": {"kind": "session_extreme", "multiplier": 1.01},
    "atr_2x": {"kind": "atr_multiple", "atr_multiplier": 2.0},
}


def resolve_initial_stop(detail: dict, rules: dict, side: str) -> float:
    """Turns evaluate_filters' own detail dict (stop_ref/price/atr) into
    the actual initial stop price, per exit.initial_stop_rule. Missing or
    unrecognized falls back to the side's own natural rule."""
    default_rule = "lod_minus_1pct" if side == "long" else "hod_plus_1pct"
    rule_name = rules.get("exit", {}).get("initial_stop_rule", default_rule)
    rule = INITIAL_STOP_RULES.get(rule_name, INITIAL_STOP_RULES[default_rule])
    if rule["kind"] == "atr_multiple":
        atr = detail.get("atr")
        if atr:
            price = detail["price"]
            return (price - rule["atr_multiplier"] * atr) if side == "long" else (price + rule["atr_multiplier"] * atr)
        rule = INITIAL_STOP_RULES[default_rule]
    return detail["stop_ref"] * rule["multiplier"]
