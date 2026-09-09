"""Entry point for the dashboard process (deploy/dashboard.service):
`python run_dashboard.py`. Listens on 127.0.0.1 only - deploy/Caddyfile is
what makes it reachable from the internet, with HTTPS.
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("web.app:app", host="127.0.0.1", port=8000, log_level="info")
