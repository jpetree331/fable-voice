@echo off
rem ============================================================
rem   START  —  double-click to start Fable's voice (FableVoiceBot)
rem   Safe to click twice: a mutex inside voice_bot prevents doubles.
rem   The bot runs in the background via Task Scheduler, so this
rem   window can close; give it ~a minute to load its models, then
rem   open the voice page (port 8012).
rem ============================================================
title Starting Fable Voice...
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
