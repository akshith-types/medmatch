@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python scripts\setup_db.py
python scripts\load_graph.py
echo ok> .db_ready
echo Database rebuilt. Run start.bat to launch the app.
pause
