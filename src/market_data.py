"""yfinance market-data helpers shared by scanner.py (ORB signal scan) and
executor.py (live price + position-management bars) - free, keyless data
source, no IBKR market-data subscription needed. Pulled out of cycle.py's
own private helpers of the same shape since both processes need the exact
same fetch conventions.
"""
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

ET = ZoneInfo("America/New_York")

# Needs to comfortably cover I3's rvol lookback (strategy_config's own
# I3_rvol_lookback_days, 14 by default) in TRADING days, so calendar days
# padded for weekends/holidays. Well within yfinance's ~60-day 5-min cap.
INTRADAY_FETCH_LOOKBACK_DAYS = 25


def _yahoo_symbol(ticker: str) -> str:
    return ticker.replace(" ", "-")


def fetch_daily(ticker: str, period: str = "260d") -> pd.DataFrame:
    return yf.Ticker(_yahoo_symbol(ticker)).history(period=period, interval="1d")


def fetch_intraday(ticker: str, period: str = f"{INTRADAY_FETCH_LOOKBACK_DAYS}d") -> pd.DataFrame:
    bars = yf.Ticker(_yahoo_symbol(ticker)).history(period=period, interval="5m", prepost=True)
    if not bars.empty:
        bars.index = bars.index.tz_convert(ET)
    return bars


def fetch_5min_bars(ticker: str) -> pd.DataFrame | None:
    """Today + yesterday's 5-minute bars - used by position management for
    the current price and swing-low/high trailing-stop reference."""
    try:
        bars = yf.Ticker(_yahoo_symbol(ticker)).history(period="2d", interval="5m")
        return bars if not bars.empty else None
    except Exception:
        return None


def current_price(ticker: str) -> float | None:
    bars = fetch_5min_bars(ticker)
    if bars is None or bars.empty:
        return None
    return float(bars["Close"].iloc[-1])


def find_latest_swing_low(bars: pd.DataFrame) -> float | None:
    lows = bars["Low"].to_numpy()
    n = len(lows)
    for i in range(n - 3, 1, -1):
        if lows[i] < lows[i - 1] and lows[i] < lows[i - 2] and lows[i] < lows[i + 1] and lows[i] < lows[i + 2]:
            return float(lows[i])
    return None


def find_latest_swing_high(bars: pd.DataFrame) -> float | None:
    """Mirror of find_latest_swing_low, for a short's trailing stop."""
    highs = bars["High"].to_numpy()
    n = len(highs)
    for i in range(n - 3, 1, -1):
        if highs[i] > highs[i - 1] and highs[i] > highs[i - 2] and highs[i] > highs[i + 1] and highs[i] > highs[i + 2]:
            return float(highs[i])
    return None
