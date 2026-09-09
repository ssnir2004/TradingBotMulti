"""Pure position-management decision logic for ORB Long v4.2's
"no_stop_delayed_trail" exit style (see strategy_config.json's
exit.hard_stop_R/trailing_trigger_R): a real hard stop placed at entry
(hard_stop_R x initial risk) protects the position until MFE (max
favorable excursion) clears trailing_trigger_R, at which point trailing
takes over completely - the hard stop is never checked again once
trailing has activated. executor.py supplies the live data (current
price, 5-minute bars); this module only ever decides.
"""


def trailing_activation_decision(pos: dict, mfe_r: float, exit_cfg: dict) -> dict:
    """Only meaningful while pos["trail_activated"] is False. Fires once
    MFE clears exit_cfg["trailing_trigger_R"] - the caller then computes
    an initial trailing stop level itself (same swing-low reference
    trailing_stop_decision uses) and flips trail_activated to True."""
    trigger = exit_cfg.get("trailing_trigger_R", 1.20)
    if mfe_r >= trigger:
        return {"action": "activate_trailing"}
    return {"action": "hold"}


def trailing_stop_decision(pos: dict, swing_stop_candidate: float | None) -> dict:
    """Only meaningful once pos["trail_activated"] is True - trails the
    stop to the low of the last N 5-minute bars, only ever tightening it
    (never loosens an already-better stop)."""
    if swing_stop_candidate is None:
        return {"action": "hold"}
    initial_stop = pos["initial_stop"]
    if swing_stop_candidate <= initial_stop:
        return {"action": "hold"}
    current_stop = pos.get("stop_price", initial_stop)
    if swing_stop_candidate > current_stop:
        return {"action": "trail_stop", "new_stop_price": swing_stop_candidate}
    return {"action": "hold"}
