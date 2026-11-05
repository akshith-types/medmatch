@echo off
setlocal
cd /d "%~dp0"
title MedMatch

where docker >nul 2>nul || (echo Docker Desktop is not installed or not on PATH. Install it from https://www.docker.com/products/docker-desktop & pause & exit /b 1)
docker info >nul 2>nul || (echo Docker Desktop is not running. Start it, wait for "Engine running", then run this again. & pause & exit /b 1)

echo [1/5] Starting PostgreSQL and Neo4j containers (first run downloads them)...
docker compose up -d --wait postgres || (echo Could not start PostgreSQL. & pause & exit /b 1)
docker compose up -d neo4j

if not exist .env copy .env.example .env >nul

if not exist .venv (
  echo [2/5] Creating Python virtual environment...
  python -m venv .venv || (echo Python 3.10+ is required. Install it from https://www.python.org & pause & exit /b 1)
)
call .venv\Scripts\activate.bat
echo [3/5] Installing Python packages...
python -m pip install -q --upgrade pip
pip install -q -r requirements.txt || (pause & exit /b 1)

if not exist .db_ready (
  echo [4/5] Creating schema and seeding data...
  python scripts\setup_db.py || (pause & exit /b 1)
  python scripts\load_graph.py || echo Neo4j load failed - the app will use the built-in routing fallback.
  echo ok> .db_ready
) else (
  echo [4/5] Database already set up. Run reset.bat to rebuild it.
)

echo [5/5] Starting MedMatch at http://localhost:8000
start "" http://localhost:8000
python -m uvicorn app.main:app --port 8000
