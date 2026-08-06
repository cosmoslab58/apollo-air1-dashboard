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

# Deploy from anywhere (ssh to bodhi and run deploy-local there). The manual
# path when CI can't do it -- an Actions outage, a missed webhook. This repo
# went without it, so the only way to ship by hand was an ad-hoc ssh line.
deploy:
    ssh bodhi.lab 'cd ~/projects/apollo-air1-dashboard && just deploy-local'

# Rebuild + restart the compose stack. Run on bodhi (CI calls this).
deploy-local:
    docker compose up -d --build

# Drop build cache and dangling images (CI calls this after deploying)
prune:
    docker builder prune -f
    docker image prune -f
