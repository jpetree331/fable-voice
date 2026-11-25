@echo off
rem ============================================================
rem   START  —  double-click to start Fable's voice (FableVoiceBot)
rem   Safe to click twice: a mutex inside voice_bot prevents doubles.
rem   The bot runs in the background via Task Scheduler, so this
rem   window can close; give it ~a minute to load its models, then
rem   open the voice page (port 8012).
rem ============================================================
title Starting Fable Voice...
rem Clear any lingering voice_bot instance first (a task-level kill can
rem orphan the python child, which then squats the port and the mutex —
rem the 2026-11-25 ghost-restart). Makes this a clean start OR restart.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'voice_bot\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
schtasks /run /tn "FableVoiceBot" >nul 2>&1
if errorlevel 1 (
    echo Could not start the FableVoiceBot task. Open Task Scheduler and
    echo check the task named FableVoiceBot, or ask Fable.
    pause
    exit /b 1
)
echo.
echo   Fable Voice is starting in the background.
echo   Give it about a minute to load, then open the voice page.
echo.
echo   This window will close itself.
timeout /t 8 >nul
