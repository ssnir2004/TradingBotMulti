"""SQLite persistence for the whole system - Scanner, Dashboard, and every
user's own Executor all open their own connection to this ONE file (WAL
mode lets them read/write concurrently without blocking each other, same
proven pattern as TradingBot's src/db.py). No ORM: plain sqlite3 with
sqlite3.Row for dict-like access, kept deliberately small since this
system carries a single fixed strategy and live-only trading - see
docs/architecture.md for the schema this implements.
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import bcrypt

PROJECT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_DIR / "tradingbotmulti.db"

ROLES = ("admin", "trader")

FIRST_GATEWAY_PORT = 5001  # arbitrary, just needs to not collide with anything else on the box


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'trader',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ibkr_credentials (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    ibkr_username TEXT NOT NULL,
    ibkr_password_encrypted BLOB NOT NULL,
    saved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_gateway (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    live_port INTEGER UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    connected INTEGER NOT NULL DEFAULT 0,
    trading_enabled INTEGER NOT NULL DEFAULT 0,
    portfolio_value REAL NOT NULL DEFAULT 25000,
    max_risk_pct REAL NOT NULL DEFAULT 1.0,
    max_trades_per_day INTEGER NOT NULL DEFAULT 5
);

CREATE TABLE IF NOT EXISTS positions (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL DEFAULT 'long',
    entry_price REAL NOT NULL,
    entry_time TEXT NOT NULL,
    qty INTEGER NOT NULL,
    initial_stop REAL NOT NULL,
    stop_price REAL NOT NULL,
    stop_order_id INTEGER,
    r_multiple REAL NOT NULL DEFAULT 0,
    mfe_price REAL,
    mae_price REAL,
    trail_activated INTEGER NOT NULL DEFAULT 0,
    hold_overnight INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, symbol)
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    size INTEGER NOT NULL,
    fill_price REAL NOT NULL,
    order_id INTEGER,
    status TEXT NOT NULL,
    exec_id TEXT
);

CREATE TABLE IF NOT EXISTS execution_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    event TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS executor_status (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    last_cycle_at TEXT,
    last_cycle_status TEXT
);

CREATE TABLE IF NOT EXISTS strategy_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    rules_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS scan_results (
    symbol TEXT PRIMARY KEY,
    side TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    pass INTEGER NOT NULL,
    price REAL,
    filters_detail_json TEXT,
    signal_detail_json TEXT
);

CREATE TABLE IF NOT EXISTS scan_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_scan_at TEXT,
    next_scan_at TEXT,
    status TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS scan_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""


def init_db(seed_strategy_path: Path | None = None):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        if seed_strategy_path is not None and get_strategy_config(conn) is None:
            rules = json.loads(Path(seed_strategy_path).read_text())
            set_strategy_config(rules, updated_by="seed", conn=conn)
            conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------ users --
def create_user(username: str, password: str, role: str = "trader") -> int:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, is_active, created_at) VALUES (?, ?, ?, 1, ?)",
            (username, password_hash, role, datetime.now().isoformat(timespec="seconds")),
        )
        user_id = cur.lastrowid
        conn.execute("INSERT INTO user_settings (user_id) VALUES (?)", (user_id,))
        conn.commit()
        return user_id
    finally:
        conn.close()


def verify_user(username: str, password: str) -> dict | None:
    """Returns the user row (only if active) on a correct password, else None."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if row is None or not row["is_active"]:
            return None
        if not bcrypt.checkpw(password.encode("utf-8"), row["password_hash"].encode("utf-8")):
            return None
        return _row_to_dict(row)
    finally:
        conn.close()


def get_user_by_username(username: str) -> dict | None:
    conn = get_conn()
    try:
        return _row_to_dict(conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone())
    finally:
        conn.close()


def get_user(user_id: int) -> dict | None:
    conn = get_conn()
    try:
        return _row_to_dict(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())
    finally:
        conn.close()


def any_users_exist() -> bool:
    conn = get_conn()
    try:
        return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    finally:
        conn.close()


def list_users() -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_active_user_ids() -> list[int]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT id FROM users WHERE is_active = 1 ORDER BY id").fetchall()
        return [r["id"] for r in rows]
    finally:
        conn.close()


def set_user_active(user_id: int, is_active: bool):
    conn = get_conn()
    try:
        conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if is_active else 0, user_id))
        conn.commit()
    finally:
        conn.close()


def set_user_role(user_id: int, role: str):
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    conn = get_conn()
    try:
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        conn.commit()
    finally:
        conn.close()


def delete_user(user_id: int):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------- ibkr creds -----
def set_ibkr_credentials(user_id: int, ibkr_username: str, ibkr_password_encrypted: bytes):
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO ibkr_credentials (user_id, ibkr_username, ibkr_password_encrypted, saved_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 ibkr_username = excluded.ibkr_username,
                 ibkr_password_encrypted = excluded.ibkr_password_encrypted,
                 saved_at = excluded.saved_at""",
            (user_id, ibkr_username, ibkr_password_encrypted, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()


def get_ibkr_credentials(user_id: int) -> dict | None:
    conn = get_conn()
    try:
        return _row_to_dict(conn.execute("SELECT * FROM ibkr_credentials WHERE user_id = ?", (user_id,)).fetchone())
    finally:
        conn.close()


def has_ibkr_credentials(user_id: int) -> bool:
    return get_ibkr_credentials(user_id) is not None


# -------------------------------------------------------------- gateway ----
def get_or_assign_gateway_port(user_id: int) -> int:
    """Every user (admin included - the admin trades too in this system, see
    docs/architecture.md) gets their own permanent live port, assigned once
    on first use and reused forever after."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT live_port FROM user_gateway WHERE user_id = ?", (user_id,)).fetchone()
        if row is not None:
            return row["live_port"]
        taken = {r["live_port"] for r in conn.execute("SELECT live_port FROM user_gateway").fetchall()}
        port = FIRST_GATEWAY_PORT
        while port in taken:
            port += 1
        conn.execute("INSERT INTO user_gateway (user_id, live_port) VALUES (?, ?)", (user_id, port))
        conn.commit()
        return port
    finally:
        conn.close()


# ---------------------------------------------------------- user settings --
def get_user_settings(user_id: int) -> dict:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO user_settings (user_id) VALUES (?)", (user_id,))
            conn.commit()
            row = conn.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def update_user_settings(user_id: int, **fields):
    """fields may include any of: trading_enabled, portfolio_value,
    max_risk_pct, max_trades_per_day, connected."""
    if not fields:
        return
    allowed = {"connected", "trading_enabled", "portfolio_value", "max_risk_pct", "max_trades_per_day"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown user_settings field(s): {unknown}")
    conn = get_conn()
    try:
        conn.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,))
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE user_settings SET {set_clause} WHERE user_id = ?", (*fields.values(), user_id))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------- strategy config -
def get_strategy_config(conn: sqlite3.Connection | None = None) -> dict | None:
    owns_conn = conn is None
    conn = conn or get_conn()
    try:
        row = conn.execute("SELECT * FROM strategy_config WHERE id = 1").fetchone()
        if row is None:
            return None
        result = dict(row)
        result["rules"] = json.loads(result["rules_json"])
        return result
    finally:
        if owns_conn:
            conn.close()


def set_strategy_config(rules: dict, updated_by: str = "", conn: sqlite3.Connection | None = None):
    owns_conn = conn is None
    conn = conn or get_conn()
    try:
        conn.execute(
            """INSERT INTO strategy_config (id, rules_json, updated_at, updated_by) VALUES (1, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET rules_json = excluded.rules_json,
                 updated_at = excluded.updated_at, updated_by = excluded.updated_by""",
            (json.dumps(rules), datetime.now().isoformat(timespec="seconds"), updated_by),
        )
        if owns_conn:
            conn.commit()
    finally:
        if owns_conn:
            conn.close()


# ----------------------------------------------------------- scan_results --
def replace_scan_results(rows: list[dict]):
    """Full replace, upsert-per-symbol style (mirrors TradingBot's own
    watchlist convention) - called once per Scanner tick with everything
    that was actually evaluated this pass. Stale rows from symbols no
    longer in the universe are pruned by trim_old_scan_results, not here."""
    conn = get_conn()
    try:
        now = datetime.now().isoformat(timespec="seconds")
        for row in rows:
            conn.execute(
                """INSERT INTO scan_results (symbol, side, generated_at, pass, price, filters_detail_json, signal_detail_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(symbol) DO UPDATE SET
                     side = excluded.side, generated_at = excluded.generated_at, pass = excluded.pass,
                     price = excluded.price, filters_detail_json = excluded.filters_detail_json,
                     signal_detail_json = excluded.signal_detail_json""",
                (
                    row["symbol"], row["side"], row.get("generated_at", now), 1 if row["pass"] else 0,
                    row.get("price"), json.dumps(row.get("filters_detail", {})), json.dumps(row.get("signal_detail", {})),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def get_scan_results(passing_only: bool = False) -> list[dict]:
    conn = get_conn()
    try:
        sql = "SELECT * FROM scan_results"
        if passing_only:
            sql += " WHERE pass = 1"
        sql += " ORDER BY symbol"
        rows = conn.execute(sql).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["pass"] = bool(d["pass"])
            d["filters_detail"] = json.loads(d.pop("filters_detail_json") or "{}")
            d["signal_detail"] = json.loads(d.pop("signal_detail_json") or "{}")
            result.append(d)
        return result
    finally:
        conn.close()


def trim_stale_scan_results(keep_symbols: set[str]):
    """Removes any scan_results row for a symbol no longer in the current
    universe pass (e.g. dropped from the index) - called once per Scanner
    tick right after replace_scan_results."""
    if not keep_symbols:
        return
    conn = get_conn()
    try:
        rows = conn.execute("SELECT symbol FROM scan_results").fetchall()
        stale = [r["symbol"] for r in rows if r["symbol"] not in keep_symbols]
        conn.executemany("DELETE FROM scan_results WHERE symbol = ?", [(s,) for s in stale])
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------ scan_status --
def set_scan_status(status: str, last_error: str | None = None, next_scan_at_iso: str | None = None):
    conn = get_conn()
    try:
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO scan_status (id, last_scan_at, next_scan_at, status, last_error) VALUES (1, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET last_scan_at = excluded.last_scan_at,
                 next_scan_at = COALESCE(excluded.next_scan_at, scan_status.next_scan_at),
                 status = excluded.status, last_error = excluded.last_error""",
            (now, next_scan_at_iso, status, last_error),
        )
        conn.commit()
    finally:
        conn.close()


def set_scan_next_run(next_scan_at_iso: str):
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO scan_status (id, next_scan_at) VALUES (1, ?)
               ON CONFLICT(id) DO UPDATE SET next_scan_at = excluded.next_scan_at""",
            (next_scan_at_iso,),
        )
        conn.commit()
    finally:
        conn.close()


def get_scan_status() -> dict | None:
    conn = get_conn()
    try:
        return _row_to_dict(conn.execute("SELECT * FROM scan_status WHERE id = 1").fetchone())
    finally:
        conn.close()


def log_scan_event(event: str, **payload):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO scan_log (timestamp, event, payload_json) VALUES (?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), event, json.dumps(payload, default=str)),
        )
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------- positions ---
def get_open_positions(user_id: int) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM positions WHERE user_id = ? ORDER BY symbol", (user_id,)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["trail_activated"] = bool(d["trail_activated"])
            d["hold_overnight"] = bool(d["hold_overnight"])
            result.append(d)
        return result
    finally:
        conn.close()


_POSITION_COLUMNS = (
    "symbol", "side", "entry_price", "entry_time", "qty", "initial_stop", "stop_price",
    "stop_order_id", "r_multiple", "mfe_price", "mae_price", "trail_activated", "hold_overnight",
)


def upsert_position(user_id: int, pos: dict):
    conn = get_conn()
    try:
        values = [pos.get(c) for c in _POSITION_COLUMNS]
        values[_POSITION_COLUMNS.index("trail_activated")] = 1 if pos.get("trail_activated") else 0
        values[_POSITION_COLUMNS.index("hold_overnight")] = 1 if pos.get("hold_overnight") else 0
        placeholders = ", ".join("?" for _ in _POSITION_COLUMNS)
        columns = ", ".join(_POSITION_COLUMNS)
        update_clause = ", ".join(f"{c} = excluded.{c}" for c in _POSITION_COLUMNS if c != "symbol")
        conn.execute(
            f"""INSERT INTO positions (user_id, {columns}) VALUES (?, {placeholders})
                ON CONFLICT(user_id, symbol) DO UPDATE SET {update_clause}""",
            (user_id, *values),
        )
        conn.commit()
    finally:
        conn.close()


def remove_position(user_id: int, symbol: str):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM positions WHERE user_id = ? AND symbol = ?", (user_id, symbol))
        conn.commit()
    finally:
        conn.close()


def set_hold_overnight(user_id: int, symbol: str, value: bool):
    conn = get_conn()
    try:
        conn.execute("UPDATE positions SET hold_overnight = ? WHERE user_id = ? AND symbol = ?", (1 if value else 0, user_id, symbol))
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------- trades --
def record_trade(user_id: int, symbol: str, side: str, size: int, fill_price: float, order_id, status: str, exec_id: str | None = None):
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO trades (user_id, timestamp, symbol, side, size, fill_price, order_id, status, exec_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, datetime.now().isoformat(timespec="seconds"), symbol, side, size, fill_price, order_id, status, exec_id),
        )
        conn.commit()
    finally:
        conn.close()


def trade_exec_id_exists(exec_id: str) -> bool:
    conn = get_conn()
    try:
        return conn.execute("SELECT 1 FROM trades WHERE exec_id = ?", (exec_id,)).fetchone() is not None
    finally:
        conn.close()


def get_trades(user_id: int, limit: int = 200) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM trades WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_todays_entries(user_id: int, side: str) -> int:
    conn = get_conn()
    try:
        action = "BUY" if side == "long" else "SELL"
        today = datetime.now().date().isoformat()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE user_id = ? AND side = ? AND status = 'Filled' AND timestamp >= ?",
            (user_id, action, today),
        ).fetchone()
        return row["n"]
    finally:
        conn.close()


# ----------------------------------------------------------- execution log -
def log_execution(user_id: int, event: str, **payload):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO execution_log (user_id, timestamp, event, payload_json) VALUES (?, ?, ?, ?)",
            (user_id, datetime.now().isoformat(timespec="seconds"), event, json.dumps(payload, default=str)),
        )
        conn.commit()
    finally:
        conn.close()


def get_execution_log(user_id: int, limit: int = 200) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM execution_log WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json") or "{}")
            result.append(d)
        return result
    finally:
        conn.close()


# --------------------------------------------------------- executor status -
def record_cycle_run(user_id: int, status: str):
    conn = get_conn()
    try:
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO executor_status (user_id, last_cycle_at, last_cycle_status) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET last_cycle_at = excluded.last_cycle_at,
                 last_cycle_status = excluded.last_cycle_status""",
            (user_id, now, status),
        )
        conn.commit()
    finally:
        conn.close()


def get_executor_status(user_id: int) -> dict | None:
    conn = get_conn()
    try:
        return _row_to_dict(conn.execute("SELECT * FROM executor_status WHERE user_id = ?", (user_id,)).fetchone())
    finally:
        conn.close()


def get_all_executor_status() -> dict[int, dict]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM executor_status").fetchall()
        return {r["user_id"]: dict(r) for r in rows}
    finally:
        conn.close()
