"""The single fixed strategy's decision logic - ORB (Opening Range
Breakout) Long v4.2, ported from TradingBot's src/orb.py (its
evaluate_orb_entry) to match exactly what actually runs live there today,
not the seeded rules.json default (see docs/architecture.md's "Decisions"
section for that correction). Pure functions only (no data fetching, no
wall-clock "now") so scanner.py and executor.py's own sizing math can
never quietly drift apart from each other.

Rules shape (see strategy_config.json): opening_range forms 9:30-9:45 ET
(3x5min bars). A breakout entry fires only on the EXACT bar that confirms
the range break with a clean displacement gap off the prior bar; a retest
entry fires on any later bar that dips back to the range level and holds.
Both require volatility_filters (RVOL + ATR%-by-price-tier) and
entry_confluence (RSI trending + EMA-trending-or-above-VWAP) to pass
first. Exit is "no_stop_delayed_trail" (see position_mgmt.py): a real
hard stop at entry (hard_stop_R x initial risk) until MFE clears
trailing_trigger_R, then swing-low trailing takes over completely.
"""
from datetime import time as dt_time

import numpy as np
import pandas as pd

SESSION_OPEN_TIME = dt_time(9, 30)
OR_END_TIME = dt_time(9, 45)  # 3 x 5-minute bars (9:30, 9:35, 9:40) -> a 15-minute opening range


def compute_opening_range(today_bars: pd.DataFrame) -> dict | None:
    """today_bars: one symbol's 5-minute bars for a single trading day.
    Returns {"or_high", "or_low", "or_end_ts"} once all 3 opening-range
    bars exist, else None - "not enough bars yet" (before 9:45 ET) is
    normal, not an error."""
    or_bars = today_bars[(today_bars.index.time >= SESSION_OPEN_TIME) & (today_bars.index.time < OR_END_TIME)]
    if len(or_bars) < 3:
        return None
    return {
        "or_high": float(or_bars["High"].max()),
        "or_low": float(or_bars["Low"].min()),
        "or_end_ts": or_bars.index[-1],
    }


def _atr_pct_tier_min(price: float, tiers: list[dict]) -> float | None:
    for tier in tiers:
        lo, hi = tier["price_min"], tier["price_max"]
        if price >= lo and (hi is None or price < hi):
            return tier["atr_pct_min"]
    return None


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


def _time_to_seconds(t: dt_time) -> int:
    return t.hour * 3600 + t.minute * 60 + t.second


def _compute_rvol(today_bars: pd.DataFrame, intraday: pd.DataFrame, as_of_date, as_of_time, lookback: int) -> float:
    """Today's volume-so-far against the AVERAGE volume accumulated by
    this same time-of-day over the past `lookback` trading days (apples-
    to-apples: partial session vs partial session)."""
    prior_dates = sorted({d for d in intraday.index.date if d < as_of_date})[-lookback:]
    as_of_secs = _time_to_seconds(as_of_time)
    prior_volume_by_this_time = []
    for d in prior_dates:
        day_bars = intraday[intraday.index.date == d].sort_index()
        times = np.array([_time_to_seconds(t) for t in day_bars.index.time])
        cum = day_bars["Volume"].to_numpy(dtype=float).cumsum()
        idx = np.searchsorted(times, as_of_secs, side="right") - 1
        prior_volume_by_this_time.append(float(cum[idx]) if idx >= 0 else 0.0)
    avg_volume = (sum(prior_volume_by_this_time) / len(prior_volume_by_this_time)) if prior_volume_by_this_time else 0.0
    today_volume_so_far = float(today_bars["Volume"].sum())
    return today_volume_so_far / avg_volume if avg_volume else 0.0


def _compute_rsi_series(closes: pd.Series, period: int) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.mask(avg_loss == 0, 100.0)


def _compute_ema_series(closes: pd.Series, period: int) -> pd.Series:
    return closes.ewm(span=period, adjust=False).mean()


def _compute_vwap_series(today_bars: pd.DataFrame) -> pd.Series:
    """Session VWAP - resets every trading day (today_bars is already
    sliced to a single day)."""
    typical_price = (today_bars["High"] + today_bars["Low"] + today_bars["Close"]) / 3
    cum_vol = today_bars["Volume"].cumsum()
    cum_pv = (typical_price * today_bars["Volume"]).cumsum()
    return cum_pv / cum_vol.replace(0, pd.NA)


def _rsi_trending(rsi_series: pd.Series, bars: int) -> bool:
    """True if the last `bars` RSI values are strictly increasing (this
    strategy is long_only, so no short-side mirror needed)."""
    if len(rsi_series) < bars:
        return False
    tail = rsi_series.iloc[-bars:]
    if tail.isna().any():
        return False
    diffs = tail.diff().iloc[1:]
    return bool((diffs > 0).all())


_CONFLUENCE_LOOKBACK_BARS = 400  # Wilder smoothing has fully converged well before this many bars


def _trend_confluence_ok(intraday: pd.DataFrame, today_bars: pd.DataFrame, cfg: dict) -> bool:
    """RSI must be trending up over the last `rsi_rising_bars` bars, AND
    (EMA is trending up OR price is above session VWAP)."""
    recent_closes = intraday["Close"].iloc[-_CONFLUENCE_LOOKBACK_BARS:]
    rsi_series = _compute_rsi_series(recent_closes, cfg.get("rsi_period", 14))
    if not _rsi_trending(rsi_series, cfg.get("rsi_rising_bars", 3)):
        return False

    ema_series = _compute_ema_series(recent_closes, cfg.get("ema_period", 20))
    if len(ema_series) < 2 or ema_series.iloc[-2:].isna().any():
        ema_trending = False
    else:
        ema_trending = bool(ema_series.iloc[-1] > ema_series.iloc[-2])

    vwap_series = _compute_vwap_series(today_bars)
    current_vwap = vwap_series.iloc[-1] if not vwap_series.empty else None
    current_price = float(today_bars["Close"].iloc[-1])
    vwap_ok = current_vwap is not None and pd.notna(current_vwap) and current_price > float(current_vwap)

    return bool(ema_trending or vwap_ok)


def low_of_last_n_bars(bars: pd.DataFrame, n: int) -> float | None:
    """Trailing-stop reference once trailing activates - lowest Low of
    the last `n` 5-minute bars (a plain, always-computable reference, not
    a swing-pivot detection)."""
    if len(bars) < n:
        return None
    return float(bars["Low"].tail(n).min())


DEFAULT_MIN_STOP_DISTANCE_PCT = 0.25


def _apply_min_stop_distance(entry_price: float, technical_stop: float, rules: dict) -> float:
    """Floors a technical stop's distance from entry - a near-zero-wick
    confirm/retest bar would otherwise produce an unrealistically tight
    stop (and a wildly inflated theoretical share count). Only ever
    WIDENS a too-tight stop."""
    min_pct = rules.get("risk", {}).get("min_stop_distance_pct", DEFAULT_MIN_STOP_DISTANCE_PCT)
    min_distance = entry_price * (min_pct / 100)
    return min(technical_stop, entry_price - min_distance)


def _apply_stop_r_multiplier(entry_price: float, stop: float, rules: dict) -> float:
    """Widens an already-floored stop's distance from entry by
    risk.initial_stop_r_multiplier (1.0 = no-op)."""
    mult = rules.get("risk", {}).get("initial_stop_r_multiplier", 1.0)
    if mult == 1.0:
        return stop
    distance = (entry_price - stop) * mult
    return entry_price - distance


def evaluate_orb_entry(daily: pd.DataFrame, intraday: pd.DataFrame, rules: dict) -> dict:
    """The ORB decision logic - `daily` must already end at the day
    before the last date in `intraday`'s index. Returns a dict always
    carrying "pass" (bool). On insufficient data it carries "error"
    instead (treat as "not yet evaluable", not a failed check). On pass,
    also carries "model" ("breakout"|"retest"), "price" (entry price),
    "initial_stop", "target_price" (always None for this strategy - no
    fixed target, only a stop that moves via hard-stop-then-trailing)."""
    vol_filters = rules["volatility_filters"]
    entry_models = rules["entry_models"]
    confluence_cfg = rules.get("entry_confluence")

    if intraday.empty:
        return {"pass": False, "error": "no intraday data"}
    as_of_date = intraday.index[-1].date()
    today_bars = intraday[intraday.index.date == as_of_date]
    if today_bars.empty:
        return {"pass": False, "error": "no bars for today yet"}

    current_ts = today_bars.index[-1]
    current_price = float(today_bars["Close"].iloc[-1])

    or_range = compute_opening_range(today_bars)
    if or_range is None:
        return {"pass": False, "error": "opening range not yet formed"}
    or_high, or_low, or_end_ts = or_range["or_high"], or_range["or_low"], or_range["or_end_ts"]

    atr_value = compute_atr(daily, vol_filters.get("V2_atr_period", 14))
    if atr_value is None:
        return {"pass": False, "error": "not enough daily history for ATR"}
    atr_pct = (atr_value / current_price * 100) if current_price else 0.0
    atr_tier_min = _atr_pct_tier_min(current_price, vol_filters["V2_atr_pct_tiers"])
    atr_ok = atr_tier_min is not None and atr_pct >= atr_tier_min

    as_of_time = current_ts.time()
    rvol = _compute_rvol(today_bars, intraday, as_of_date, as_of_time, vol_filters["V1_rvol_lookback_days"])
    rvol_ok = rvol >= vol_filters["V1_rvol_min"]
    volatility_ok = bool(atr_ok and rvol_ok)

    post_or_bars = today_bars[today_bars.index > or_end_ts]
    confirm_bars = post_or_bars[post_or_bars["Close"] > or_high]
    confirmed = not confirm_bars.empty
    confirm_ts = confirm_bars.index[0] if confirmed else None

    if confluence_cfg and volatility_ok and confirmed:
        confluence_ok = _trend_confluence_ok(intraday, today_bars, confluence_cfg)
    else:
        confluence_ok = True if not confluence_cfg else None

    detail = {
        "price": current_price, "or_high": or_high, "or_low": or_low,
        "rvol": rvol, "atr_pct": atr_pct, "atr_tier_min": atr_tier_min,
        "or_formed": True, "confirmed": confirmed, "volatility_ok": volatility_ok,
        "confluence_ok": confluence_ok,
    }

    if not volatility_ok or not confirmed or not confluence_ok:
        return {"pass": False, **detail}

    # --- breakout: only exactly at the confirmation bar, with a clean
    # displacement gap off the prior bar ---
    if entry_models.get("breakout", {}).get("enabled") and current_ts == confirm_ts:
        bar_pos = today_bars.index.get_loc(confirm_ts)
        if bar_pos > 0:
            prev_bar = today_bars.iloc[bar_pos - 1]
            confirm_bar = today_bars.loc[confirm_ts]
            if confirm_bar["Low"] > prev_bar["High"]:
                entry_price = float(confirm_bar["Close"])
                stop = float(confirm_bar["Low"])
                stop = _apply_min_stop_distance(entry_price, stop, rules)
                stop = _apply_stop_r_multiplier(entry_price, stop, rules)
                if entry_price - stop > 0:
                    return {"pass": True, "model": "breakout", "initial_stop": stop, "target_price": None,
                            **detail, "price": entry_price}

    # --- retest: any bar strictly after confirmation that dips back to
    # the opening-range high and closes back above it (holds it) ---
    if entry_models.get("retest", {}).get("enabled") and current_ts > confirm_ts:
        bar = today_bars.loc[current_ts]
        retest_hit = bar["Low"] <= or_high and bar["Close"] > or_high and bar["Close"] > bar["Open"]
        if retest_hit:
            entry_price = float(bar["Close"])
            stop = float(bar["Low"])
            stop = _apply_min_stop_distance(entry_price, stop, rules)
            stop = _apply_stop_r_multiplier(entry_price, stop, rules)
            if entry_price - stop > 0:
                return {"pass": True, "model": "retest", "initial_stop": stop, "target_price": None,
                        **detail, "price": entry_price}

    return {"pass": False, **detail}
