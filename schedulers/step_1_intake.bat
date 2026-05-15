@echo off
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
python steps\step_1_intake.py >> "logs\step_1_task.log" 2>&1
