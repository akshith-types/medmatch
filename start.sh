#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

command -v docker >/dev/null || { echo "Install Docker Desktop first."; exit 1; }
docker info >/dev/null 2>&1 || { echo "Start Docker Desktop, then run this again."; exit 1; }

echo "[1/5] Starting PostgreSQL and Neo4j containers (first run downloads them)..."
docker compose up -d --wait postgres
docker compose up -d neo4j

[ -f .env ] || cp .env.example .env
[ -d .venv ] || { echo "[2/5] Creating virtual environment..."; python3 -m venv .venv; }
source .venv/bin/activate
echo "[3/5] Installing Python packages..."
pip install -q --upgrade pip && pip install -q -r requirements.txt

if [ ! -f .db_ready ]; then
  echo "[4/5] Creating schema and seeding data..."
  python scripts/setup_db.py
  python scripts/load_graph.py || echo "Neo4j load failed - using built-in routing fallback."
  touch .db_ready
fi

echo "[5/5] MedMatch running at http://localhost:8000"
(sleep 2; (open http://localhost:8000 || xdg-open http://localhost:8000) >/dev/null 2>&1) &
python -m uvicorn app.main:app --port 8000
