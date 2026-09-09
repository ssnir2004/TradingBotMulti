"""Scanner - the one shared, always-on process that runs the single fixed
strategy's signal detection for everyone. Each tick (every minute, 9:45
through the strategy's own latest_entry_et - see docs/architecture.md's
Scheduling section) does two passes, mirroring TradingBot's own proven
two-stage design: even for ORB (an intraday opening-range-breakout signal,
not a gap-based one), TradingBot's live entry_scan only ever evaluates
symbols that already cleared the SAME morning gap-scan watchlist every
other strategy shares (see morning_prefilter.py's own docstring: "gap-up
survivors feed the long strategy" - any long strategy, not just gap-based
ones) - so this collapses that same two stages into one continuous
process instead of a once-a-morning script + a per-account cycle:

  1. ONE batched yf.download() over the whole universe (a couple of
     seconds) to find which symbols are gapping up at all today (a cheap,
     necessary pre-filter - running the full per-symbol ORB evaluation
     below against the whole S&P 500 every minute would be far too slow/
     rate-limited against yfinance).
  2. Only for those survivors, the real ORB evaluation (opening-range
     breakout/retest, volatility filters, RSI/EMA/VWAP confluence) that
     decides pass/fail and computes the initial stop every Executor sizes
     its own entry off of.

Universe is the full S&P 500 list rather than TradingBot's own narrower
"sp500_marketcap_1b" custom universe (see docs/architecture.md's
"Decisions" section) - every S&P 500 constituent already exceeds $1B
market cap by the index's own inclusion bar, so this is a documented
no-op simplification, not a behavior change.

Writes every survivor (pass or fail) to scan_results so the dashboard's
shared scan table has something to show even between real signals. Pure
market data - never touches IBKR, so this one process serves every
connected user's Executor regardless of how many are actually trading.
"""
import logging
import sys
import time as time_module
import traceback
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

from src import db, market_data, strategy
from src.sp500_tickers import SP500_TICKERS

PROJECT_DIR = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
SCAN_INTERVAL_SECONDS = 60
SCAN_START = dt_time(9, 45)
# Same default TradingBot's morning_prefilter.py uses - gap-scanning is
# market-data screening, mode/strategy-agnostic, not something ORB's own
# rules_json configures (it has no D3-style gap field at all).
GAP_PREFILTER_MIN_GAP_PCT = 3.0
MAX_SURVIVORS_PER_SIDE = 40  # generous cap - a normal market day rarely gaps this many S&P 500 names at once

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s scanner: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("scanner")


def _yahoo_symbol(ticker: str) -> str:
    return ticker.replace(" ", "-")


def _latest_entry_time(rules: dict) -> dt_time:
    raw = rules.get("time_filter", {}).get("latest_entry_et", "15:30")
    hour, minute = (int(p) for p in raw.split(":", 1))
    return dt_time(hour, minute)


def _in_scan_window(rules: dict, now_et: datetime) -> bool:
    if now_et.weekday() >= 5:
        return False
    return SCAN_START <= now_et.time() <= _latest_entry_time(rules)


def _sides_for_direction(direction: str) -> list[str]:
    return {"long_only": ["long"], "short_only": ["short"], "both": ["long", "short"]}.get(direction, ["long"])


def _gap_prefilter(min_price: float, sides: list[str]) -> dict[str, list[str]]:
    """Stage 1: one cheap batched daily-bar download for the whole
    universe, returning {"long": [...survivor tickers...], "short": [...]},
    each capped at MAX_SURVIVORS_PER_SIDE and sorted by gap size (biggest
    first)."""
    yahoo_tickers = [_yahoo_symbol(t) for t in SP500_TICKERS]

    data = yf.download(
        tickers=" ".join(yahoo_tickers), period="2d", interval="1d",
        group_by="ticker", threads=5, progress=False, auto_adjust=True,
    )
    if data.empty:
        return {side: [] for side in sides}

    candidates = {side: [] for side in sides}
    multi_ticker = len(SP500_TICKERS) > 1
    for ibkr_ticker, yahoo_ticker in zip(SP500_TICKERS, yahoo_tickers):
        try:
            bars = data[yahoo_ticker] if multi_ticker else data
            bars = bars.dropna(how="all")
            if len(bars) < 2:
                continue
            prior_close = float(bars.iloc[-2]["Close"])
            today_close = float(bars.iloc[-1]["Close"])
            if today_close < min_price or not prior_close:
                continue
            gap_pct = (today_close - prior_close) / prior_close * 100

            if "long" in sides and gap_pct >= GAP_PREFILTER_MIN_GAP_PCT:
                candidates["long"].append((gap_pct, ibkr_ticker))
            if "short" in sides and gap_pct <= -GAP_PREFILTER_MIN_GAP_PCT:
                candidates["short"].append((gap_pct, ibkr_ticker))
        except (KeyError, IndexError, ValueError):
            continue

    result = {}
    for side in sides:
        ordered = sorted(candidates[side], key=lambda pair: pair[0], reverse=(side == "long"))
        result[side] = [ticker for _gap, ticker in ordered[:MAX_SURVIVORS_PER_SIDE]]
    return result


def run_scan_tick():
    config = db.get_strategy_config()
    if config is None:
        logger.error("No strategy_config seeded - run with init_db(seed_strategy_path=...) first")
        return
    rules = config["rules"]
    now_et = datetime.now(ET)
    if not _in_scan_window(rules, now_et):
        db.set_scan_status("idle")
        return

    sides = _sides_for_direction(rules.get("direction", "long_only"))
    min_price = rules.get("universe_filters", {}).get("min_price_usd", 0)
    db.set_scan_status("running")

    survivors_by_side = _gap_prefilter(min_price, sides)
    results = []
    universe_symbols: set[str] = set()
    errors = 0
    for side, tickers in survivors_by_side.items():
        for ticker in tickers:
            universe_symbols.add(ticker)
            try:
                daily = market_data.fetch_daily(ticker)
                intraday = market_data.fetch_intraday(ticker)
                detail = strategy.evaluate_orb_entry(daily, intraday, rules)
                if "error" in detail:
                    continue
                results.append({
                    "symbol": ticker, "side": side, "pass": detail["pass"], "price": detail.get("price"),
                    "filters_detail": {k: v for k, v in detail.items() if k not in ("pass",)},
                    "signal_detail": {"model": detail.get("model"), "initial_stop": detail.get("initial_stop")} if detail.get("pass") else {},
                })
            except Exception as exc:  # noqa: BLE001 - one bad ticker must not kill the scan
                errors += 1
                logger.warning("ORB evaluation failed for %s (%s): %s", ticker, side, exc)

    db.replace_scan_results(results)
    db.trim_stale_scan_results(universe_symbols)
    next_run = now_et + timedelta(seconds=SCAN_INTERVAL_SECONDS)
    total_survivors = sum(len(v) for v in survivors_by_side.values())
    db.set_scan_status(
        "ok" if errors == 0 or errors < max(1, total_survivors) * 0.5 else "degraded",
        last_error=(f"{errors}/{total_survivors} candidate evaluations failed" if errors else None),
        next_scan_at_iso=next_run.isoformat(),
    )
    passing = sum(1 for r in results if r["pass"])
    logger.info("tick: %d gap candidates, %d passing all filters, %d errors", total_survivors, passing, errors)
    db.log_scan_event("tick_complete", candidates=total_survivors, passing=passing, errors=errors)


def main():
    db.init_db(seed_strategy_path=PROJECT_DIR / "strategy_config.json")
    logger.info("Scanner starting, DB at %s", db.DB_PATH)
    while True:
        started = time_module.monotonic()
        try:
            run_scan_tick()
        except Exception:
            logger.exception("scan tick crashed")
            db.set_scan_status("error", last_error=traceback.format_exc()[-2000:])
        elapsed = time_module.monotonic() - started
        time_module.sleep(max(1.0, SCAN_INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    main()
