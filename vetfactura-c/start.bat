@echo off
chcp 65001 >nul
title VetFactura C

set PROJECT_DIR=%~dp0
set BACKEND_DIR=%PROJECT_DIR%backend
set FRONTEND_DIR=%PROJECT_DIR%frontend
set VENV_DIR=%BACKEND_DIR%\venv
set BACKEND_PORT=8000
set FRONTEND_PORT=3000

echo.
echo ══════════════════════════════════════════
echo   VetFactura C — Iniciando...
echo ══════════════════════════════════════════
echo.

:: Verificar que install.bat se haya ejecutado
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [ERROR] El entorno virtual no existe.
    echo         Ejecuta primero "install.bat" haciendo doble clic.
    echo.
    pause
    exit /b 1
)

:: Copiar .env si falta
if not exist "%BACKEND_DIR%\.env" (
    if exist "%BACKEND_DIR%\.env.example" (
        copy "%BACKEND_DIR%\.env.example" "%BACKEND_DIR%\.env" >nul
    )
)

:: Iniciar backend
echo [*] Iniciando backend  (puerto %BACKEND_PORT%)...
start "VetFactura - Backend" /min cmd /c "cd /d "%BACKEND_DIR%" && "%VENV_DIR%\Scripts\python.exe" -m uvicorn main:app --host 0.0.0.0 --port %BACKEND_PORT%"

:: Iniciar frontend
echo [*] Iniciando frontend (puerto %FRONTEND_PORT%)...
start "VetFactura - Frontend" /min cmd /c "cd /d "%FRONTEND_DIR%" && "%VENV_DIR%\Scripts\python.exe" -m http.server %FRONTEND_PORT% --bind 0.0.0.0"

:: Esperar que el backend levante
timeout /t 3 /nobreak >nul

:: Decidir URL (factura.com si el hosts tiene el alias, sino localhost:3000)
set FRONT_URL=http://localhost:%FRONTEND_PORT%
findstr /C:"factura.com" "%SystemRoot%\System32\drivers\etc\hosts" >nul 2>&1
if %errorlevel% equ 0 set FRONT_URL=http://factura.com

:: Abrir navegador
start %FRONT_URL%

echo.
echo ══════════════════════════════════════════
echo   VetFactura C levantado correctamente
echo ══════════════════════════════════════════
echo   Frontend (Web):  %FRONT_URL%
echo   Backend  (API):  http://localhost:%BACKEND_PORT%
echo   API docs:        http://localhost:%BACKEND_PORT%/docs
echo ══════════════════════════════════════════
echo.
echo   Presiona cualquier tecla para DETENER todo.
echo.
pause >nul

:: Cerrar procesos
echo [*] Deteniendo servicios...
taskkill /fi "WINDOWTITLE eq VetFactura - Backend*" /f >nul 2>&1
taskkill /fi "WINDOWTITLE eq VetFactura - Frontend*" /f >nul 2>&1
echo [OK] Servicios detenidos.
timeout /t 2 /nobreak >nul
