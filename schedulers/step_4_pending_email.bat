@echo off
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
python steps\step_4_pending_email.py >> "logs\step_4_task.log" 2>&1
