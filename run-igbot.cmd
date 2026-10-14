@echo off
rem Keeps ig-comment-bot running; restarts 5 s after any stop.
cd /d C:\Users\Aakashsingh\ig-comment-bot
set PYTHONIOENCODING=utf-8
:loop
python -u igbot.py >> igbot.log 2>&1
echo %date% %time% igbot stopped (code %errorlevel%), restarting in 5 s >> igbot.log
ping -n 6 127.0.0.1 >nul
goto loop
