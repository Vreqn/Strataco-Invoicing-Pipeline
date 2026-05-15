@echo off
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
python steps\step_2_unzip.py >> "logs\step_2_task.log" 2>&1
