"""The shared dashboard - one FastAPI process serving every user. Never
talks to IBKR directly (yfinance/systemd-status reads only) and carries no
pandas/backtest/PDF/Excel machinery, which is the whole reason it stays
light on RAM (see docs/architecture.md's RAM budget) - Scanner and each
user's own Executor are separate processes that own all of that.

Three screens (see docs/architecture.md's "Screens" section):
  - / - shared scan (everyone) + the logged-in user's own panel only,
    plus (admin only) a connection-status strip for every user - no P&L,
    no positions, just up/down.
  - /admin/overview - full P&L + positions for every user, admin only,
    not linked from anywhere a regular user's nav reaches.
  - /admin/users - user management, admin only.
"""
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src import db, gateway_provisioning, secrets_store
from web.auth import COOKIE_NAME, MAX_AGE_SECONDS, make_session_cookie, read_username, require_admin, require_user

PROJECT_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(PROJECT_DIR / "web" / "templates"))

app = FastAPI(title="TradingBotMulti")
app.mount("/static", StaticFiles(directory=str(PROJECT_DIR / "web" / "static")), name="static")


@app.on_event("startup")
def _startup():
    db.init_db(seed_strategy_path=PROJECT_DIR / "strategy_config.json")


# ------------------------------------------------------------- auth pages --
@app.get("/setup")
def setup_page(request: Request):
    if db.any_users_exist():
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "setup.html")


@app.post("/setup")
def setup_submit(username: str = Form(...), password: str = Form(...)):
    if db.any_users_exist():
        return RedirectResponse("/login", status_code=303)
    db.create_user(username.strip(), password, role="admin")
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE_NAME, make_session_cookie(username.strip()), max_age=MAX_AGE_SECONDS, httponly=True, samesite="lax")
    return response


@app.get("/login")
def login_page(request: Request, error: str | None = None):
    if not db.any_users_exist():
        return RedirectResponse("/setup")
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
def login_submit(username: str = Form(...), password: str = Form(...)):
    user = db.verify_user(username.strip(), password)
    if user is None:
        return RedirectResponse("/login?error=1", status_code=303)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE_NAME, make_session_cookie(user["username"]), max_age=MAX_AGE_SECONDS, httponly=True, samesite="lax")
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


# ---------------------------------------------------------- main dashboard-
@app.get("/")
def dashboard(request: Request):
    if not db.any_users_exist():
        return RedirectResponse("/setup", status_code=303)
    username = read_username(request)
    if not username:
        return RedirectResponse("/login", status_code=303)
    user = db.get_user_by_username(username)
    if user is None or not user["is_active"]:
        return RedirectResponse("/login", status_code=303)

    config = db.get_strategy_config()
    context = {
        "user": user,
        "strategy_name": (config["rules"].get("strategy_name") if config else "(not configured)"),
    }
    if user["role"] == "admin":
        context["all_statuses"] = _all_user_connection_statuses()
    return templates.TemplateResponse(request, "dashboard.html", context)


def _all_user_connection_statuses() -> list[dict]:
    result = []
    for u in db.list_users():
        if not u["is_active"]:
            continue
        settings = db.get_user_settings(u["id"])
        executor_status = db.get_executor_status(u["id"]) or {}
        gw_status = {}
        if db.has_ibkr_credentials(u["id"]):
            try:
                gw_status = gateway_provisioning.status(u["id"])
            except Exception:
                gw_status = {"error": True}
        result.append({
            "username": u["username"], "role": u["role"], "connected": bool(settings["connected"]),
            "trading_enabled": bool(settings["trading_enabled"]),
            "gateway_active": gw_status.get("gateway_active", False),
            "executor_active": gw_status.get("executor_active", False),
            "last_cycle_status": executor_status.get("last_cycle_status"),
            "last_cycle_at": executor_status.get("last_cycle_at"),
        })
    return result


def _current_admin_or_none(request: Request) -> dict | None:
    username = read_username(request)
    if not username:
        return None
    user = db.get_user_by_username(username)
    if user is None or not user["is_active"] or user["role"] != "admin":
        return None
    return user


# ----------------------------------------------------------------- admin --
@app.get("/admin/overview")
def admin_overview(request: Request):
    admin = _current_admin_or_none(request)
    if admin is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "admin_overview.html", {"user": admin})


@app.get("/admin/api/connection_status")
def admin_api_connection_status(admin: dict = Depends(require_admin)):
    """Connection/health only - no P&L, no positions (see docs/architecture.md's
    "no way to discover the admin strip exists" requirement) - the light
    endpoint the dashboard's own admin strip polls, separate from
    /admin/api/overview's heavier full P&L payload used by /admin/overview."""
    return {"statuses": _all_user_connection_statuses()}


@app.get("/admin/api/overview")
def admin_api_overview(admin: dict = Depends(require_admin)):
    users_out = []
    for u in db.list_users():
        settings = db.get_user_settings(u["id"])
        positions = db.get_open_positions(u["id"])
        trades = db.get_trades(u["id"], limit=20)
        executor_status = db.get_executor_status(u["id"]) or {}
        users_out.append({
            "id": u["id"], "username": u["username"], "role": u["role"], "is_active": bool(u["is_active"]),
            "settings": settings, "positions": positions, "recent_trades": trades, "executor_status": executor_status,
        })
    return {"users": users_out}


@app.get("/admin/users")
def admin_users_page(request: Request):
    admin = _current_admin_or_none(request)
    if admin is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "admin_users.html", {"user": admin, "users": db.list_users()})


@app.post("/admin/users/create")
def admin_users_create(username: str = Form(...), password: str = Form(...), role: str = Form("trader"), admin: dict = Depends(require_admin)):
    if db.get_user_by_username(username.strip()) is not None:
        raise HTTPException(status_code=400, detail="Username already exists")
    db.create_user(username.strip(), password, role=role)
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{target_id}/toggle_active")
def admin_users_toggle_active(target_id: int, admin: dict = Depends(require_admin)):
    target = db.get_user(target_id)
    if target is None:
        raise HTTPException(status_code=404)
    db.set_user_active(target_id, not target["is_active"])
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{target_id}/role")
def admin_users_set_role(target_id: int, role: str = Form(...), admin: dict = Depends(require_admin)):
    db.set_user_role(target_id, role)
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{target_id}/delete")
def admin_users_delete(target_id: int, admin: dict = Depends(require_admin)):
    if target_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    db.delete_user(target_id)
    return RedirectResponse("/admin/users", status_code=303)


# ------------------------------------------------------------------- api --
@app.get("/api/scan")
def api_scan(user: dict = Depends(require_user)):
    return {"status": db.get_scan_status(), "results": db.get_scan_results()}


@app.get("/api/me")
def api_me(user: dict = Depends(require_user)):
    settings = db.get_user_settings(user["id"])
    gw_status = {}
    if db.has_ibkr_credentials(user["id"]):
        try:
            gw_status = gateway_provisioning.status(user["id"])
        except gateway_provisioning.ProvisioningError as exc:
            gw_status = {"error": str(exc)}
    return {
        "settings": settings,
        "positions": db.get_open_positions(user["id"]),
        "trades": db.get_trades(user["id"], limit=50),
        "executor_status": db.get_executor_status(user["id"]),
        "has_credentials": db.has_ibkr_credentials(user["id"]),
        "gateway_status": gw_status,
    }


@app.post("/api/settings")
def api_settings(
    trading_enabled: bool | None = Form(None),
    portfolio_value: float | None = Form(None),
    max_risk_pct: float | None = Form(None),
    max_trades_per_day: int | None = Form(None),
    user: dict = Depends(require_user),
):
    fields = {}
    if trading_enabled is not None:
        fields["trading_enabled"] = trading_enabled
    if portfolio_value is not None:
        fields["portfolio_value"] = portfolio_value
    if max_risk_pct is not None:
        fields["max_risk_pct"] = max_risk_pct
    if max_trades_per_day is not None:
        fields["max_trades_per_day"] = max_trades_per_day
    db.update_user_settings(user["id"], **fields)
    return {"ok": True}


@app.post("/api/credentials")
def api_credentials(ibkr_username: str = Form(...), ibkr_password: str = Form(...), user: dict = Depends(require_user)):
    encrypted = secrets_store.encrypt(ibkr_password)
    db.set_ibkr_credentials(user["id"], ibkr_username.strip(), encrypted)
    return {"ok": True}


@app.post("/api/connect")
def api_connect(user: dict = Depends(require_user)):
    try:
        gateway_provisioning.provision_and_connect(user["id"])
    except gateway_provisioning.CredentialsNotSetError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except gateway_provisioning.ProvisioningError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True}


@app.post("/api/resume_executor")
def api_resume_executor(user: dict = Depends(require_user)):
    try:
        gateway_provisioning.resume_executor(user["id"])
    except gateway_provisioning.ProvisioningError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True}


@app.post("/api/disconnect")
def api_disconnect(user: dict = Depends(require_user)):
    try:
        gateway_provisioning.disconnect(user["id"])
    except gateway_provisioning.ProvisioningError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True}


@app.get("/api/gateway_status")
def api_gateway_status(user: dict = Depends(require_user)):
    if not db.has_ibkr_credentials(user["id"]):
        return {"has_credentials": False}
    try:
        return {"has_credentials": True, **gateway_provisioning.status(user["id"])}
    except gateway_provisioning.ProvisioningError as exc:
        return {"has_credentials": True, "error": str(exc)}
