@echo off
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
python steps\step_5_to_ap.py >> "logs\step_5_task.log" 2>&1
