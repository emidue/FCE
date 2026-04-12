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

:: Verificar Python
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python no encontrado. Instalalo desde https://www.python.org
    echo         Asegurate de marcar "Add Python to PATH"
    pause
    exit /b 1
)

:: Crear venv si no existe
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo [*] Creando entorno virtual...
    python -m venv "%VENV_DIR%"
)

:: Activar venv e instalar dependencias
echo [*] Instalando dependencias...
call "%VENV_DIR%\Scripts\activate.bat"
pip install -q -r "%BACKEND_DIR%\requirements.txt"

:: Copiar .env si no existe
if not exist "%BACKEND_DIR%\.env" (
    echo [*] Creando .env desde .env.example...
    copy "%BACKEND_DIR%\.env.example" "%BACKEND_DIR%\.env" >nul
    echo [!] Edita %BACKEND_DIR%\.env con tus datos reales.
)

:: Iniciar backend
echo [*] Iniciando backend en puerto %BACKEND_PORT%...
start "VetFactura - Backend" /min cmd /c "cd /d "%BACKEND_DIR%" && "%VENV_DIR%\Scripts\python.exe" -m uvicorn main:app --reload --host 0.0.0.0 --port %BACKEND_PORT%"

:: Iniciar frontend
echo [*] Iniciando frontend en puerto %FRONTEND_PORT%...
start "VetFactura - Frontend" /min cmd /c "cd /d "%FRONTEND_DIR%" && python -m http.server %FRONTEND_PORT% --bind 0.0.0.0"

:: Esperar que el backend levante
timeout /t 3 /nobreak >nul

:: Abrir navegador
start http://localhost:%FRONTEND_PORT%

echo.
echo ══════════════════════════════════════════
echo   VetFactura C levantado correctamente
echo ══════════════════════════════════════════
echo   Backend  (API):  http://localhost:%BACKEND_PORT%
echo   Frontend (Web):  http://localhost:%FRONTEND_PORT%
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
