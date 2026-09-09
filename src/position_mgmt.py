"""Pure position-management decision logic for the single fixed strategy's
exit rules (breakeven_trigger_R + post_breakeven swing trailing) - the
same generic default path TradingBot's cycle.manage_position falls
through to for a strategy with no special management_style. executor.py
supplies the live data (current price, 5-minute bars); this module only
ever decides.
"""


def breakeven_decision(pos: dict, exit_cfg: dict, r_multiple: float) -> dict:
    """Only meaningful while pos["state"] == "pre_breakeven" - holds the
    full position untouched (protected by its own broker-side stop) until
    r_multiple clears exit_cfg["breakeven_trigger_R"], then flips the stop
    to breakeven (entry price)."""
    trigger = exit_cfg.get("breakeven_trigger_R")
    if trigger is None:
        return {"action": "hold"}
    if r_multiple >= trigger:
        return {"action": "breakeven_flip", "new_stop_price": pos["entry_price"], "new_state": "post_breakeven"}
    return {"action": "hold"}


def trailing_stop_decision(pos: dict, swing_stop_candidate: float | None) -> dict:
    """Only meaningful while pos["state"] == "post_breakeven" - trails the
    stop to the most recent swing low (long) / swing high (short), only
    ever tightening it (never loosens an already-better stop)."""
    if swing_stop_candidate is None:
        return {"action": "hold"}
    side = pos.get("side", "long")
    initial_stop = pos["initial_stop"]
    candidate_valid = (swing_stop_candidate < initial_stop) if side == "short" else (swing_stop_candidate > initial_stop)
    if not candidate_valid:
        return {"action": "hold"}
    current_stop = pos.get("stop_price", initial_stop)
    improves = (swing_stop_candidate < current_stop) if side == "short" else (swing_stop_candidate > current_stop)
    if improves:
        return {"action": "trail_stop", "new_stop_price": swing_stop_candidate}
    return {"action": "hold"}
