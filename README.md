# TradingBotMulti

Shared-strategy, live-only trading dashboard for 3 users, each with their
own IBKR live account. See [docs/architecture.md](docs/architecture.md) for
the full design (RAM budget, schema, screens, scheduling).

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"   # -> SESSION_SECRET
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # -> CREDENTIALS_ENCRYPTION_KEY
# fill both into .env

# Dashboard (creates tradingbotmulti.db on first run, seeded from strategy_config.json)
python run_dashboard.py
# -> http://127.0.0.1:8000 - first visit prompts you to create the admin account

# Scanner (separate process - safe to run without any IBKR connection)
python scanner.py

# Executor for one user, once they've connected their Gateway from the dashboard
python executor.py --user-id 1
```

IB Gateway itself isn't part of this repo - see `deploy/ibc/` and
TradingBot's own `DEPLOY.md` for the IBC/Xvfb install steps, which are
identical here (just live-mode only, one instance per user).

## Deploying

`deploy/` has the systemd units and Caddy config, assuming an `/opt/
tradingbotmulti` checkout and a `tradingbotmulti` system user (mirrors
TradingBot's own deploy convention):

1. `deploy/scanner.service`, `deploy/dashboard.service` - one each, always on.
2. `deploy/ibgateway-live@.service`, `deploy/executor@.service` - per-user,
   started/stopped from the dashboard's Connect flow
   (`src/gateway_provisioning.py`), never run manually.
3. `deploy/sudoers-tradingbotmulti` - scoped sudo rules that let the
   dashboard start/stop only those exact unit patterns.
4. `deploy/Caddyfile` - HTTPS reverse proxy in front of the dashboard.
5. `deploy/ibc/` - IBC config templates; real per-user configs are
   generated automatically by `src/gateway_provisioning.py`, never hand-written.

## Continuous deployment

`.github/workflows/deploy.yml` runs on every push to `main` (and can also
be fired manually from the Actions tab). It only ships CODE - it assumes
the steps above (Python/venv, IBC+Xvfb+IB Gateway, systemd units, sudoers,
Caddy) are already done manually, once, on the server - and it only
restarts `scanner.service`/`dashboard.service`. It deliberately never
restarts `ibgateway-live@*`/`executor@*` - those hold live IBKR sessions
for connected users, and bouncing them on every push would force a fresh
2FA approval and interrupt live position management for everyone. Restart
a specific user's `executor@<user_id>.service` by hand when its own code
actually changed (ideally outside market hours).

Setup, one time:

1. Create a dedicated `deploy` system user on the server and add the
   GitHub Actions runner's public key to its `~/.ssh/authorized_keys`.
2. Install `deploy/sudoers-deploy` (see that file's own header) - grants
   that user passwordless sudo for exactly `systemctl restart
   scanner.service` and `systemctl restart dashboard.service`, nothing else.
3. In the GitHub repo, add these under Settings -> Secrets and variables
   -> Actions -> **Secrets**:
   - `DEPLOY_HOST` - server hostname/IP
   - `DEPLOY_USER` - `deploy` (or whatever you named it)
   - `DEPLOY_SSH_KEY` - the matching private key
   - `DEPLOY_PORT` - optional, defaults to 22
4. If the server checkout isn't at `/opt/tradingbotmulti`, update
   `DEPLOY_PATH` at the top of `.github/workflows/deploy.yml`.
5. Push to `main` (this repo currently only has `claude/task-xaly0d` -
   merge it into `main` to get deploys going).

## Repository layout

```
scanner.py          shared signal-detection process
executor.py          per-user trading engine (systemd: executor@<user_id>)
trade_exec.py        order-execution subprocess spawned by executor.py
run_dashboard.py      FastAPI dashboard entry point
strategy_config.json  seed for the single fixed strategy (strategy_config table)
src/
  db.py               SQLite schema + all persistence
  strategy.py          ORB Long v4.2 signal logic + initial-stop rules (pure)
  position_mgmt.py      breakeven/trailing decision logic (pure)
  market_data.py        yfinance fetch helpers
  ibkr_client.py         thin ib_async wrapper
  gateway_provisioning.py per-user Gateway/Executor systemd control
  secrets_store.py        Fernet encryption for stored IBKR credentials
  notify.py                admin-only Telegram alerts
web/
  app.py               FastAPI routes (dashboard, admin screens, API)
  auth.py                signed-cookie session auth
  templates/, static/     Jinja2 templates + CSS
deploy/                systemd units, Caddyfile, sudoers, IBC config templates
docs/architecture.md    full design doc
```
