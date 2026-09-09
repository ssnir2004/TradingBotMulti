"""ES (E-mini S&P 500 futures) VWAP directional market filter, ported from
TradingBot's src/es_filter.py. Market direction is BULLISH when ES trades
above its own session VWAP - a long entry is only allowed while BULLISH.

Fails OPEN: whenever ES's own direction can't be determined (no futures
market-data entitlement, a disconnected Gateway, insufficient bars),
check() allows the trade through rather than silently blocking the whole
strategy the instant one third-party feed hiccups.

Off by default (see executor.py's ES_VWAP_FILTER_ENABLED) - matches
TradingBot's own live behavior today: this gate needs real CME futures
market-data entitlement no connected account currently has, so it's never
actually enabled there either. Kept fully wired (not stubbed out) so
flipping it on later needs no code change, just that one constant.
"""
import pandas as pd

from src.strategy import _compute_vwap_series

ES_SYMBOL = "ES"
ES_EXCHANGE = "CME"
ES_BAR_SIZE = "5 mins"


def compute_market_direction(es_bars_today: pd.DataFrame | None) -> dict | None:
    """`es_bars_today` must already be sliced to a single session. Returns
    {"es_price", "es_vwap", "direction"} off the last bar, or None if
    there's no bar yet or VWAP can't be computed."""
    if es_bars_today is None or es_bars_today.empty:
        return None
    vwap_series = _compute_vwap_series(es_bars_today)
    es_vwap = vwap_series.iloc[-1] if not vwap_series.empty else None
    if es_vwap is None or pd.isna(es_vwap):
        return None
    es_price = float(es_bars_today["Close"].iloc[-1])
    return {"es_price": es_price, "es_vwap": float(es_vwap), "direction": "BULLISH" if es_price > es_vwap else "BEARISH"}


def check(direction: dict | None) -> dict:
    """Pure gate decision for a long entry. direction=None -> allowed=True,
    reason "es_data_unavailable" (see module docstring)."""
    if direction is None:
        return {"allowed": True, "reason": "es_data_unavailable", "direction": None, "es_price": None, "es_vwap": None}
    bullish = direction["direction"] == "BULLISH"
    return {
        "allowed": bullish, "reason": "es_ok" if bullish else "ES Below VWAP",
        "direction": direction["direction"], "es_price": direction["es_price"], "es_vwap": direction["es_vwap"],
    }


def fetch_live_direction(ib) -> dict | None:
    """Live IBKR fetch - a fresh ES ContFuture (IBKR's own continuous-
    front-month contract) qualified and queried for today's 5-minute
    bars. Never raises - any failure comes back as None, which check()
    already treats as fail-open."""
    from ib_async import ContFuture

    try:
        contract = ContFuture(ES_SYMBOL, ES_EXCHANGE)
        qualified = ib.qualifyContracts(contract)
        if not qualified or qualified[0] is None:
            return None
        bars = ib.reqHistoricalData(
            qualified[0], endDateTime="", durationStr="1 D",
            barSizeSetting=ES_BAR_SIZE, whatToShow="TRADES", useRTH=False, formatDate=2,
            timeout=30,
        )
        if not bars:
            return None
        df = pd.DataFrame({
            "High": [float(b.high) for b in bars], "Low": [float(b.low) for b in bars],
            "Close": [float(b.close) for b in bars], "Volume": [float(b.volume) for b in bars],
        }, index=pd.DatetimeIndex([pd.Timestamp(b.date) for b in bars]))
        if df.empty:
            return None
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        today = df.index.max().date()
        df = df[df.index.date == today]
        return compute_market_direction(df)
    except Exception:
        return None
