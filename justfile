# Apollo AIR-1 dashboard — dev/test recipes (venv-based, for local use).
# CI (.github/workflows/ci.yml) runs the same steps GitHub-hosted without a
# venv and deploys via the self-hosted runner on pushes to main.

# One-time setup: venv with app deps + dev tools
install:
    python3 -m venv venv
    ./venv/bin/pip install --upgrade pip
    ./venv/bin/pip install -r requirements.txt ruff pytest

# Flask dev server on :5858 (uses the real InfluxDB/MQTT on bodhi via .env)
dev:
    ./venv/bin/python app.py

lint:
    ./venv/bin/ruff check .

compile:
    ./venv/bin/python -m compileall -q .

test:
    ./venv/bin/pytest -q

# Rebuild + restart the compose stack. Run on bodhi.
deploy-local:
    docker compose up -d --build
