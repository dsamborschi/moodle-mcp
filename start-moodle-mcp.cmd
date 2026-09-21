@echo off
cd /d "%~dp0"
set "PATH=%~dp0.tools\uv;%PATH%"
".venv\Scripts\python.exe" "moodle_mcp_server.py"
