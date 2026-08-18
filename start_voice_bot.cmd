@echo off
rem Fable Voice autostart wrapper (Task Scheduler: "FableVoiceBot").
rem PYTHONUTF8: pipecat's runner prints emoji; Windows consoles default to cp1252.
rem A named mutex inside voice_bot.py guarantees two schedulers can never stack.
set PYTHONUTF8=1
cd /d E:\git\fable-voice
.venv\Scripts\python.exe voice_bot.py -t webrtc --host 0.0.0.0 --port 8011 >> logs\voice_bot.log 2>&1
