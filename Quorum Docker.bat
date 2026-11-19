@echo off
rem Quorum Web in Docker: starts (or updates) the container and opens the browser UI.
rem It keeps running in the background; stop it with: docker compose down
setlocal
cd /d "%~dp0"
title Quorum Docker

docker info >nul 2>nul || (
  echo Docker Desktop is not running. Start it, then try again.
  pause
  exit /b 1
)
docker compose up -d --build || (pause & exit /b 1)

docker compose exec -T quorum claude auth status | findstr /c:"\"loggedIn\": true" >nul || (
  echo [!] Claude is not signed in inside the container. Run:
  echo     docker compose exec -it quorum claude auth login
)
docker compose exec -T quorum codex login status >nul 2>nul || (
  echo [!] ChatGPT is not signed in inside the container. Run:
  echo     docker compose exec -it quorum codex login --device-auth
)

start "" http://127.0.0.1:8765
timeout /t 5 >nul
