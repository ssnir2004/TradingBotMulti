# Architecture

3-user shared-strategy, live-only trading dashboard. Separate repo/server
from TradingBot on purpose (see "Why a separate system" below) - based on
the planning document this repo was scaffolded from.

## Goal and constraints

- 3 users, each with their own separate **live** IBKR account, trading in
  parallel.
- One fixed strategy - users cannot switch or edit it.
- Server capped at **1GB RAM** - the constraint that drives every
  architectural choice below.
- No paper trading at all.
- No backtest/optimization in this system (v1) - live engine only.
- Every user sees **only themselves** (no other user's positions/P&L/
  status) - except the admin (see Screens).
- Login: an admin Users screen, same pattern as TradingBot's own.

## Why a separate system

Measured on TradingBot's existing server (2026-09-09): one IB Gateway
process (Java+Xvfb+IBC) costs only ~59MB RSS - much cheaper than initially
assumed. The real weight is the dashboard process itself (~165MB, driven
by pandas/backtest/PDF/Excel) and a trading-engine process (~26MB). Since
this system needs no backtest/PDF/Excel at all, splitting the dashboard
into a separate lightweight process (~100MB) and running the strategy scan
once (shared) instead of once per account is what makes 3 live users fit
in 1GB with headroom - see the budget below.

| Component | Estimate |
|---|---|
| Scanner (1) | ~40MB |
| Dashboard (1, no PDF/Excel/backtest) | ~100MB |
| 3x (Gateway ~60MB + Executor ~25MB) | ~255MB |
| OS + systemd + Caddy + margin | ~150-200MB |
| **Total** | **~545-595MB of 1GB** - ~400MB headroom |

## Architecture - 4 process types

1. **Scanner** (`scanner.py`, one process, always on) - runs the fixed
   strategy's signal detection: universe refresh + filter evaluation,
   merged into one continuous process (unlike TradingBot, which keeps
   these separate to support multiple strategies - not needed here).
   Internally still a cheap batched gap-scan feeding the real per-symbol
   filter evaluation only on survivors (see scanner.py's own docstring) -
   the same 2-stage design TradingBot proved out, just running
   continuously instead of once a morning. Writes to the shared
   `scan_results` table.
2. **Dashboard** (`run_dashboard.py` / `web/app.py`, one FastAPI process)
   - serves everyone. Every user sees the same shared scan plus their own
     personal panel only. Never talks to IBKR directly and carries no
     pandas/backtest/PDF/Excel - that's what keeps it light.
3. **Executor** (`executor.py`, up to 3 processes, one per **connected**
   user) - a live Gateway plus a lightweight process that reads the shared
   `scan_results` and trades **only that user's own account**, sized off
   **their own** capital/risk/quotas. A user who hasn't connected consumes
   nothing beyond a DB row.
4. **Admin** - not a separate process, a `role` within the same system
   (see Screens).

## Core principle

When a symbol clears every filter in `scan_results`, **every Executor
checks itself independently** against that shared signal (already
holding it? at today's cap? enough free capital?) - three users can react
differently to the exact same signal, on purpose.

## Database schema

### Global (shared)
```
strategy_config   (id=1, rules_json, updated_at, updated_by)
scan_results      (symbol, side, generated_at, pass, price,
                    filters_detail_json, signal_detail_json)  -- upsert per symbol
scan_status       (id=1, last_scan_at, next_scan_at, status, last_error)
scan_log          (id, timestamp, event, payload_json)  -- optional audit trail
```

### Per-user
```
users             (id, username, password_hash, role ['admin'|'trader'], is_active, created_at)
ibkr_credentials  (user_id PK/FK, ibkr_username, ibkr_password_encrypted, saved_at)
user_gateway      (user_id PK/FK, live_port)
user_settings     (user_id PK/FK, connected, trading_enabled,
                    portfolio_value, max_risk_pct, max_trades_per_day)
positions         (user_id, symbol, side, entry_price, entry_time, qty,
                    initial_stop, stop_price, stop_order_id, r_multiple,
                    mfe_price, mae_price, trail_activated, hold_overnight)
trades            (user_id, timestamp, symbol, side, size, fill_price, order_id, status, exec_id)
execution_log     (user_id, timestamp, event, payload_json)
executor_status   (user_id PK/FK, last_cycle_at, last_cycle_status)
```

`max_concurrent_positions`/`max_position_size_pct_of_portfolio` live in the
shared `strategy_config` (strategy policy); `portfolio_value`/
`max_risk_pct`/`max_trades_per_day` are per-user (each user's own
capital) - the same split already proven in TradingBot.

## Screens

### 1. Main dashboard (`/`, everyone)
- **Trader**: shared scan (top) + their own panel only (bottom) - Connect
  flow, trading-enabled toggle, their positions/trades, risk settings they
  edit themselves. **No information about any other user, ever.**
- **Admin** (same page): everything above, plus an extra status strip
  shown only to them - every user's Gateway/Executor connection health
  only (no P&L, no positions). Rendered server-side only when
  `role == 'admin'` (see `web/app.py`'s `dashboard()`), so a regular
  user's own HTML never contains this data - there's no way to "discover"
  it exists client-side.

### 2. Admin Overview (`/admin/overview`, admin only)
Not linked from any regular user's navigation. Full P&L + positions for
every user, for oversight.

### 3. Admin Users (`/admin/users`, admin only)
User management (create/enable/disable, username, role) - no money, no
positions.

## Connect flow

Same proven mechanism as TradingBot's `src/gateway_provisioning.py`: the
user enters their IBKR credentials once (encrypted, nobody can read them
back - not even the admin) -> clicks Connect -> their Gateway comes up ->
waits for 2FA approval on their own phone -> "Start trading engine" starts
their Executor.

## Scheduling

- **Scanner**: every minute, 9:45 ET through the strategy's own
  `latest_entry_et` (lesson from TradingBot: a slower cadence misses
  breakout bars).
- **Executor** (every connected user): reads `scan_results` at its own
  independent cadence (also every minute), ignoring any row whose
  `generated_at` is too stale (>2 minutes) so a dead/stuck Scanner can't
  drive a stale entry. No tight coupling to the Scanner's own timing -
  SQLite WAL handles the concurrent read/write, same as TradingBot today.

## One Executor tick (`executor.py`'s `run_cycle`)

1. Check stop-outs - always, even while paused
2. Manage open positions (`no_stop_delayed_trail`: real hard stop at
   entry until MFE clears `trailing_trigger_R`, then swing-low trailing
   takes over completely - see `src/position_mgmt.py`) - always
3. If `trading_enabled=false` -> stop here
4. Otherwise: read `pass=true` and fresh rows from `scan_results`; for
   each, skip if already held/at today's quota/at max positions; size off
   THIS user's own capital/risk; place the order through THEIR OWN
   Gateway; record the position
5. Force-close at end of day (`force_close_et`) - same for everyone,
   session-driven, not user-driven
6. Update `executor_status`

## Decisions made building this out

- **Strategy**: **ORB Long v4.2 (Hard Stop 2.5R + Early Trailing)** -
  the strategy actually active live in TradingBot today, not the seeded
  `rules.json` default (an earlier pass here wrongly built against that
  default - "Long Breakout Conservative" - before this correction).
  Opening-range breakout/retest entry (9:30-9:45 ET range, confirm bar or
  later retest), gated by RVOL/ATR% volatility filters and an RSI/EMA/
  VWAP confluence check; a real -2.5R hard stop at entry, replaced
  entirely by swing-low trailing once MFE clears +1.20R. Ported from
  TradingBot's `src/orb.py`/`src/db.py`'s `EXTRA_STRATEGY_PRESETS` entry
  of the same name - see `src/strategy.py`. No edit UI in v1 (matches
  "users cannot switch/edit the strategy"); an admin who needs to change
  it can update the `strategy_config` row directly.
  Two simplifications from the source preset, both documented no-ops
  against its actual LIVE behavior (not its backtest-only behavior):
  `custom_universe: "sp500_marketcap_1b"` is scanned as the plain S&P 500
  list instead (every constituent already exceeds $1B by the index's own
  inclusion bar), and `risk.position_size_multiplier` is dropped (TradingBot's
  own live `cycle.py` never actually reads this field - only its
  backtester does).
- **ES VWAP filter**: wired but off by default (`executor.py`'s
  `ES_VWAP_FILTER_ENABLED`), matching TradingBot's own live behavior
  today (no connected account has real CME futures entitlement, so it's
  never actually enabled there either) - flipping it on later needs no
  code change.
- **Admin trades too**: the admin has their own `positions`/
  `user_settings`/Gateway/Executor exactly like every other user, plus the
  two extra admin screens - not a pure oversight role.
- **Sessions**: identical signed-cookie mechanism to TradingBot's own
  `web/auth.py` (no session store, cookie carries only the username, role
  is re-read from the DB on every request).
- **Notifications**: Telegram, admin-only (see `src/notify.py`) - a single
  `TELEGRAM_CHAT_ID` in `.env` is the admin's own phone; every user's
  Executor sends its alerts there, tagged with that user's username, so
  the admin has one feed across every account without regular traders
  needing their own notification channel.

## Still open (raised in the original plan, unresolved / deferred)

- Exact one-time provisioning on whichever server this actually lands on
  (systemd unit installation, Caddy/DuckDNS domain, IBC/TWS install paths)
  - `deploy/` ships the templates; the paths inside assume `/opt/
    tradingbotmulti` and a `tradingbotmulti` system user, matching
    TradingBot's own convention. Once that's done once, by hand,
    `.github/workflows/deploy.yml` takes over shipping code changes (see
    README.md's "Continuous deployment" section) - it deliberately only
    restarts the two shared/stateless processes, never a connected user's
    live Gateway/Executor.
