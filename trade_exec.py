"""Order execution script, spawned as a subprocess by executor.py. Runs on
its own IBKR client id (IBKR_EXEC_CLIENT_ID) so it never collides with the
Executor's own persistent connection to the same user's Gateway.
"""
import argparse
import sys
from pathlib import Path

from dotenv import dotenv_values

from src import db
from src.ibkr_client import IBKRClient

PROJECT_DIR = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True, type=int)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--side", required=True, choices=["BUY", "SELL"])
    parser.add_argument("--size", required=True, type=int)
    args = parser.parse_args()

    db.init_db(seed_strategy_path=PROJECT_DIR / "strategy_config.json")
    env = dotenv_values(PROJECT_DIR / ".env")
    port = db.get_or_assign_gateway_port(args.user_id)

    ibkr = IBKRClient(env.get("IBKR_HOST", "127.0.0.1"), port, int(env.get("IBKR_EXEC_CLIENT_ID", 3)))

    try:
        trade = ibkr.place_order(args.symbol, args.side, args.size)
        status = trade.orderStatus.status
        fill_price = trade.orderStatus.avgFillPrice or 0
        order_id = trade.order.orderId
        # The final fill's execId, if any - lets a future reconciliation
        # pass recognize this exact fill later and skip re-logging it.
        exec_id = trade.fills[-1].execution.execId if trade.fills else None

        db.record_trade(args.user_id, args.symbol, args.side, args.size, fill_price, order_id, status, exec_id=exec_id)

        # Only a real fill counts as success - a non-zero exit tells the
        # caller (executor.py's entry_scan/force_close) "no position was
        # opened/closed", so a false success here would record a phantom
        # position for a share that was never actually bought.
        if status != "Filled" or fill_price <= 0:
            for entry in trade.log:
                print(f"trade.log: {entry}")
            print(f"{args.side} {args.size} {args.symbol}: order_id={order_id} fill_price={fill_price} status={status} (not filled)")
            sys.exit(1)

        print(f"{args.side} {args.size} {args.symbol}: order_id={order_id} fill_price={fill_price} status={status}")
    finally:
        ibkr.disconnect()


if __name__ == "__main__":
    main()
