@echo off
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
python steps\step_6_paid_archive.py >> "logs\step_6_task.log" 2>&1
