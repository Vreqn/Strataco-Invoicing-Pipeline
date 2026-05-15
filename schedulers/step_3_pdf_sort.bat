@echo off
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
python steps\step_3_pdf_sort.py >> "logs\step_3_task.log" 2>&1
