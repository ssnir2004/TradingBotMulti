"""Executor - one process per connected user (systemd unit
executor@<user_id>.service, started only once that user's own Gateway is
confirmed connected - see src/gateway_provisioning.py). Every connected
user runs their own Executor at an independent tempo (also every minute -
see docs/architecture.md's Scheduling section); there is no synchronization
with the Scanner beyond both reading/writing the same SQLite WAL file.

Core principle (see docs/architecture.md): when a symbol clears the shared
strategy's filters, EVERY Executor evaluates itself against that signal
independently - already holding it, at today's trade cap, out of capital -
using only THIS user's own settings and THIS user's own broker connection.
Three users can react differently to the exact same signal, on purpose.

One tick, always in this order:
  1. Stop-outs (broker-side stop already filled) - always, even paused
  2. Manage open positions (breakeven flip / swing trailing) - always
  3. If trading_enabled is off, stop here
  4. Otherwise: read scan_results, size and enter under THIS user's own
     capital/risk settings
  5. Force-close everything at the strategy's own force_close_et
  6. Record executor_status
"""
import argparse
import logging
import math
import subprocess
import sys
import time as time_module
import traceback
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
from ib_async import Stock, StopOrder

from src import db, market_data, position_mgmt, strategy
from src.ibkr_client import IBKRClient, belongs_to_account, scoped_positions
from src.notify import notify

PROJECT_DIR = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
CYCLE_INTERVAL_SECONDS = 60
TOO_EARLY_END = dt_time(9, 35)
CLOSED_START = dt_time(16, 0)
# A scan_results row older than this is a dead/stuck Scanner, not a live
# signal - ignored rather than acted on (see docs/architecture.md).
SCAN_RESULT_MAX_AGE_MINUTES = 2
SUBPROCESS_TIMEOUT = 40

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


def _env() -> dict:
    return dotenv_values(PROJECT_DIR / ".env")


def _connect(env: dict, user_id: int, client_id: int) -> IBKRClient:
    port = db.get_or_assign_gateway_port(user_id)
    return IBKRClient(env.get("IBKR_HOST", "127.0.0.1"), port, client_id)


def _force_close_time(rules: dict) -> dt_time:
    raw = rules.get("time_filter", {}).get("force_close_et", "15:51")
    hour, minute = (int(p) for p in raw.split(":", 1))
    return dt_time(hour, minute)


def time_gate(rules: dict, now_et: datetime | None = None) -> str:
    now_et = now_et or datetime.now(ET)
    if now_et.weekday() >= 5:
        return "weekend"
    t = now_et.time()
    if t < TOO_EARLY_END:
        return "too_early"
    if t >= CLOSED_START:
        return "closed"
    if t >= _force_close_time(rules):
        return "force_close"
    return "ok"


def _within_entry_window(rules: dict, now_et: datetime) -> bool:
    t = now_et.time()
    tf = rules.get("time_filter", {})
    for key, cmp in (("earliest_entry_et", lambda b: t < b), ("latest_entry_et", lambda b: t >= b)):
        raw = tf.get(key)
        if not raw:
            continue
        hour, minute = (int(p) for p in raw.split(":", 1))
        if cmp(dt_time(hour, minute)):
            return False
    return True


# --------------------------------------------------------- order helpers ---
def _qualify(ib, symbol: str):
    (contract,) = ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
    return contract


def _find_order(ib, order_id: int):
    for trade in ib.trades():
        if trade.order.orderId == order_id:
            return trade.order
    return None


def _cancel_stop(ib, order_id: int | None):
    if order_id is None:
        return
    order = _find_order(ib, order_id)
    if order is not None:
        ib.cancelOrder(order)


def _place_stop(ib, symbol: str, quantity: int, stop_price: float, side: str) -> int:
    contract = _qualify(ib, symbol)
    action = "SELL" if side == "long" else "BUY"
    order = StopOrder(action, quantity, round(stop_price, 2))
    if getattr(ib, "account", None):
        order.account = ib.account
    trade = ib.placeOrder(contract, order)
    ib.sleep(1)
    return trade.order.orderId


def _broker_position(ib, symbol: str) -> dict | None:
    for p in scoped_positions(ib):
        if p.contract.symbol == symbol and p.position != 0:
            return {"qty": p.position, "avg_cost": float(p.avgCost)}
    return None


def _market_close(user_id: int, ib, symbol: str, quantity: int, side: str) -> bool:
    action = "SELL" if side == "long" else "BUY"
    proc = subprocess.run(
        [sys.executable, str(PROJECT_DIR / "trade_exec.py"), "--user-id", str(user_id),
         "--symbol", symbol, "--side", action, "--size", str(quantity)],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    db.log_execution(user_id, "market_close_subprocess", symbol=symbol, side=side, action=action, qty=quantity, stdout=proc.stdout)
    return proc.returncode == 0


# ---------------------------------------------------------------- step 1 ---
def check_stop_outs(user_id: int, ib, positions: list[dict]) -> list[dict]:
    if not positions:
        return positions
    cutoff = datetime.now(ET) - timedelta(hours=1)
    fills = [f for f in ib.fills() if belongs_to_account(ib, f.execution.acctNumber)]
    stopped = set()
    for pos in positions:
        stop_order_id = pos.get("stop_order_id")
        if stop_order_id is None:
            continue
        side = pos.get("side", "long")
        for fill in fills:
            fill_time = fill.time
            if fill_time.tzinfo is None:
                fill_time = fill_time.replace(tzinfo=ZoneInfo("UTC"))
            if fill_time.astimezone(ET) < cutoff:
                continue
            if fill.execution.orderId == stop_order_id:
                pnl = ((pos["entry_price"] - fill.execution.avgPrice) if side == "short"
                       else (fill.execution.avgPrice - pos["entry_price"])) * pos["qty"]
                notify(f"[{pos.get('_username', user_id)}] STOP {pos['symbol']}", f"exit ${fill.execution.avgPrice:.2f}, P&L ${pnl:+.2f}")
                db.log_execution(user_id, "stop_out", symbol=pos["symbol"], side=side, fill_price=fill.execution.avgPrice, pnl=pnl)
                stopped.add(pos["symbol"])
                db.remove_position(user_id, pos["symbol"])
    return [p for p in positions if p["symbol"] not in stopped]


# ---------------------------------------------------------------- step 2 ---
def manage_position(user_id: int, ib, pos: dict, rules: dict) -> dict:
    exit_cfg = rules["exit"]
    side = pos.get("side", "long")
    price = market_data.current_price(pos["symbol"])
    if price is None:
        return pos

    entry = pos["entry_price"]
    initial_risk = (pos["initial_stop"] - entry) if side == "short" else (entry - pos["initial_stop"])
    if initial_risk <= 0:
        return pos
    r_multiple = ((entry - price) if side == "short" else (price - entry)) / initial_risk
    pos["r_multiple"] = r_multiple
    pos["mae_price"] = (min(pos.get("mae_price") or entry, price) if side == "long"
                         else max(pos.get("mae_price") or entry, price))

    if pos["state"] == "pre_breakeven":
        decision = position_mgmt.breakeven_decision(pos, exit_cfg, r_multiple)
        if decision["action"] == "breakeven_flip":
            _cancel_stop(ib, pos.get("stop_order_id"))
            pos["stop_order_id"] = _place_stop(ib, pos["symbol"], pos["qty"], decision["new_stop_price"], side)
            pos["stop_price"] = decision["new_stop_price"]
            pos["state"] = decision["new_state"]
            notify(f"BE {pos['symbol']}", f"stop -> ${entry:.2f}")
            db.log_execution(user_id, "breakeven_flip", symbol=pos["symbol"], side=side, new_stop=entry)

    if pos["state"] == "post_breakeven":
        bars = market_data.fetch_5min_bars(pos["symbol"])
        swing_stop_candidate = None
        if bars is not None and len(bars) > 5:
            swing = market_data.find_latest_swing_high(bars) if side == "short" else market_data.find_latest_swing_low(bars)
            if swing is not None:
                swing_stop_candidate = (swing + 0.01) if side == "short" else (swing - 0.01)
        decision = position_mgmt.trailing_stop_decision(pos, swing_stop_candidate)
        if decision["action"] == "trail_stop":
            _cancel_stop(ib, pos.get("stop_order_id"))
            pos["stop_order_id"] = _place_stop(ib, pos["symbol"], pos["qty"], decision["new_stop_price"], side)
            old_stop = pos.get("stop_price", pos["initial_stop"])
            pos["stop_price"] = decision["new_stop_price"]
            notify(f"TRAIL {pos['symbol']}", f"stop ${old_stop:.2f} -> ${decision['new_stop_price']:.2f}")
            db.log_execution(user_id, "trail_stop", symbol=pos["symbol"], side=side, old=old_stop, new=decision["new_stop_price"])

    if pos["qty"] > 0:
        db.upsert_position(user_id, pos)
    return pos


# ---------------------------------------------------------------- step 4 ---
def entry_scan(user_id: int, ib, positions: list[dict], rules: dict, settings: dict, username: str) -> list[dict]:
    now_et = datetime.now(ET)
    if not _within_entry_window(rules, now_et):
        return positions

    max_concurrent = rules["risk"]["max_concurrent_positions"]
    direction = rules.get("direction", "long_only")
    sides = {"long_only": ["long"], "short_only": ["short"], "both": ["long", "short"]}.get(direction, ["long"])

    held_symbols = {p.contract.symbol for p in scoped_positions(ib) if p.position != 0}
    held_symbols |= {p["symbol"] for p in positions}

    portfolio_value = settings["portfolio_value"]
    max_risk_pct = settings["max_risk_pct"]
    max_trades_per_day = settings["max_trades_per_day"]
    max_position_pct = rules["risk"]["max_position_size_pct_of_portfolio"] / 100

    scan_rows = db.get_scan_results(passing_only=True)
    max_age = timedelta(minutes=SCAN_RESULT_MAX_AGE_MINUTES)

    for side in sides:
        action = "BUY" if side == "long" else "SELL"
        side_positions = [p for p in positions if p.get("side", "long") == side]
        if len(side_positions) >= max_concurrent:
            continue
        if db.count_todays_entries(user_id, side) >= max_trades_per_day:
            continue

        candidates = [r for r in scan_rows if r["side"] == side]
        for row in candidates:
            if len(side_positions) >= max_concurrent:
                break
            if db.count_todays_entries(user_id, side) >= max_trades_per_day:
                break
            ticker = row["symbol"]
            if ticker in held_symbols:
                continue
            try:
                generated_at = datetime.fromisoformat(row["generated_at"])
            except ValueError:
                continue
            if now_et - generated_at.replace(tzinfo=ET) > max_age:
                continue

            detail = row["filters_detail"]
            price = detail.get("price")
            if price is None:
                continue
            initial_stop = strategy.resolve_initial_stop(detail, rules, side)
            r = (initial_stop - price) if side == "short" else (price - initial_stop)
            if r <= 0:
                continue

            risk_dollars = portfolio_value * (max_risk_pct / 100)
            size_by_risk = math.floor(risk_dollars / r)
            size_by_cap = math.floor(portfolio_value * max_position_pct / price)
            size = min(size_by_risk, size_by_cap)
            if size < 1:
                continue

            proc = subprocess.run(
                [sys.executable, str(PROJECT_DIR / "trade_exec.py"), "--user-id", str(user_id),
                 "--symbol", ticker, "--side", action, "--size", str(size)],
                capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
            )
            db.log_execution(user_id, "entry_attempt", symbol=ticker, side=side, qty=size, price=price, stdout=proc.stdout)
            if proc.returncode != 0:
                broker_pos = _broker_position(ib, ticker)
                if broker_pos is None:
                    continue
                fill_qty, fill_price = abs(broker_pos["qty"]), broker_pos["avg_cost"]
                db.log_execution(user_id, "delayed_fill_recovered", symbol=ticker, side=side, qty=fill_qty, price=fill_price)
                notify(f"[{username}] Delayed fill recovered: {ticker}",
                       f"order timed out but broker shows {fill_qty} shares @ ${fill_price:.2f} - now tracked with a stop")
            else:
                fill_qty, fill_price = size, price

            stop_order_id = _place_stop(ib, ticker, fill_qty, initial_stop, side)
            new_position = {
                "symbol": ticker, "side": side, "entry_price": fill_price,
                "entry_time": now_et.isoformat(timespec="seconds"), "qty": fill_qty,
                "initial_stop": initial_stop, "stop_price": initial_stop, "stop_order_id": stop_order_id,
                "state": "pre_breakeven", "r_multiple": 0.0, "mae_price": fill_price,
            }
            db.upsert_position(user_id, new_position)
            positions.append(new_position)
            side_positions.append(new_position)
            held_symbols.add(ticker)
            notify(f"[{username}] {action} {ticker}", f"@ ${fill_price:.2f}, stop ${initial_stop:.2f}, qty {fill_qty}")
            db.log_execution(user_id, "entry", symbol=ticker, side=side, price=fill_price, stop=initial_stop, qty=fill_qty)

    return positions


# ---------------------------------------------------------------- step 5 ---
def force_close_all(user_id: int, ib, positions: list[dict], username: str):
    if not positions:
        return
    held = [p for p in positions if p.get("hold_overnight")]
    to_close = [p for p in positions if not p.get("hold_overnight")]
    for pos in held:
        db.set_hold_overnight(user_id, pos["symbol"], False)
        db.log_execution(user_id, "force_close_skipped", symbol=pos["symbol"], reason="hold_overnight")
    if not to_close:
        return
    notify(f"[{username}] EOD Force Close", f"flattening {len(to_close)} position(s)")
    for pos in to_close:
        side = pos.get("side", "long")
        _cancel_stop(ib, pos.get("stop_order_id"))
        closed = _market_close(user_id, ib, pos["symbol"], pos["qty"], side)
        if not closed:
            closed = _broker_position(ib, pos["symbol"]) is None
        db.log_execution(user_id, "force_close", symbol=pos["symbol"], side=side, qty=pos["qty"], confirmed=closed)
        if closed:
            db.remove_position(user_id, pos["symbol"])
        else:
            pos["stop_order_id"] = _place_stop(ib, pos["symbol"], pos["qty"], pos["stop_price"], side)
            db.upsert_position(user_id, pos)
            notify(f"[{username}] FORCE CLOSE FAILED: {pos['symbol']}",
                   f"still holding {pos['qty']} - stop re-armed at ${pos['stop_price']:.2f}, will retry next cycle")


# -------------------------------------------------------------- one tick ---
def run_cycle(user_id: int, username: str) -> str:
    config = db.get_strategy_config()
    if config is None:
        return "no_strategy"
    rules = config["rules"]
    status = time_gate(rules)
    if status in ("weekend", "too_early", "closed"):
        return status

    env = _env()
    ibkr = None
    try:
        client_id = int(env.get("IBKR_CYCLE_CLIENT_ID", 2))
        try:
            ibkr = _connect(env, user_id, client_id)
        except Exception:
            time_module.sleep(5)
            ibkr = _connect(env, user_id, client_id)
        ib = ibkr.ib

        positions = db.get_open_positions(user_id)
        positions = check_stop_outs(user_id, ib, positions)
        positions = [manage_position(user_id, ib, p, rules) for p in positions]

        if status == "force_close":
            force_close_all(user_id, ib, positions, username)
            db.record_cycle_run(user_id, status)
            return status

        settings = db.get_user_settings(user_id)
        if settings["trading_enabled"]:
            entry_scan(user_id, ib, positions, rules, settings, username)
        else:
            db.log_execution(user_id, "entries_paused", reason="trading_disabled")

        db.record_cycle_run(user_id, status)
        return status
    except Exception as exc:  # noqa: BLE001
        db.log_execution(user_id, "cycle_error", error=str(exc), traceback=traceback.format_exc()[-2000:])
        notify(f"[{username}] Cycle CRASHED", str(exc)[:500])
        db.record_cycle_run(user_id, "error")
        raise
    finally:
        if ibkr is not None:
            ibkr.disconnect()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True, type=int)
    args = parser.parse_args()

    db.init_db(seed_strategy_path=PROJECT_DIR / "strategy_config.json")
    user = db.get_user(args.user_id)
    if user is None:
        print(f"No such user_id={args.user_id}", file=sys.stderr)
        sys.exit(1)
    logger = logging.getLogger(f"executor.{user['username']}")
    logger.info("Executor starting for user_id=%s (%s)", args.user_id, user["username"])

    while True:
        started = time_module.monotonic()
        try:
            status = run_cycle(args.user_id, user["username"])
            logger.info("cycle: %s", status)
        except Exception:
            logger.exception("cycle crashed")
        elapsed = time_module.monotonic() - started
        time_module.sleep(max(1.0, CYCLE_INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    main()
