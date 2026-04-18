@echo off
chcp 65001 >nul
title VetFactura C - Instalacion

:: Auto-elevar a administrador (necesario para hosts y netsh portproxy)
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [*] Solicitando permisos de administrador...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"

set PROJECT_DIR=%~dp0
set BACKEND_DIR=%PROJECT_DIR%backend
set VENV_DIR=%BACKEND_DIR%\venv
set HOSTS_FILE=%SystemRoot%\System32\drivers\etc\hosts
set ALIAS=factura.com
set FRONTEND_PORT=3000
set ALIAS_PORT=80

echo.
echo ══════════════════════════════════════════
echo   VetFactura C — Instalacion
echo ══════════════════════════════════════════
echo.

:: 1) Verificar Python
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python no esta instalado o no esta en el PATH.
    echo.
    echo   1. Descargalo desde: https://www.python.org/downloads/
    echo   2. En el instalador, marcá "Add Python to PATH".
    echo   3. Volvé a ejecutar este script.
    echo.
    pause
    exit /b 1
)
for /f "tokens=2 delims= " %%v in ('python --version') do set PYVER=%%v
echo [OK] Python detectado: %PYVER%
echo.

:: 2) Crear venv
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo [*] Creando entorno virtual en %VENV_DIR% ...
    python -m venv "%VENV_DIR%"
    if %errorlevel% neq 0 (
        echo [ERROR] No se pudo crear el entorno virtual.
        pause
        exit /b 1
    )
) else (
    echo [OK] Entorno virtual ya existe.
)
echo.

:: 3) Instalar dependencias
echo [*] Actualizando pip ...
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip >nul

echo [*] Instalando dependencias (puede tardar unos minutos) ...
"%VENV_DIR%\Scripts\python.exe" -m pip install -r "%BACKEND_DIR%\requirements.txt"
if %errorlevel% neq 0 (
    echo [ERROR] Fallo la instalacion de dependencias.
    pause
    exit /b 1
)
echo [OK] Dependencias instaladas.
echo.

:: 4) Copiar .env si no existe
if not exist "%BACKEND_DIR%\.env" (
    if exist "%BACKEND_DIR%\.env.example" (
        copy "%BACKEND_DIR%\.env.example" "%BACKEND_DIR%\.env" >nul
        echo [OK] Archivo .env creado desde .env.example.
    )
) else (
    echo [OK] Archivo .env ya existe.
)
echo.

:: 5) Agregar alias %ALIAS% al hosts
echo [*] Configurando alias local "%ALIAS%" -^> 127.0.0.1 ...
findstr /C:"%ALIAS%" "%HOSTS_FILE%" >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] El alias "%ALIAS%" ya estaba en el archivo hosts.
) else (
    >>"%HOSTS_FILE%" echo.
    >>"%HOSTS_FILE%" echo 127.0.0.1    %ALIAS%    # VetFactura C
    if %errorlevel% equ 0 (
        echo [OK] Alias agregado a %HOSTS_FILE%.
    ) else (
        echo [!] No se pudo escribir en el archivo hosts.
    )
)
echo.

:: 6) Redireccion de puerto 80 -> 3000 (para poder usar http://factura.com sin puerto)
echo [*] Configurando redireccion puerto %ALIAS_PORT% -^> %FRONTEND_PORT% ...
netsh interface portproxy show v4tov4 | findstr /C:"127.0.0.1       %ALIAS_PORT%" >nul 2>&1
if %errorlevel% equ 0 (
    netsh interface portproxy delete v4tov4 listenaddress=127.0.0.1 listenport=%ALIAS_PORT% >nul 2>&1
)
netsh interface portproxy add v4tov4 listenaddress=127.0.0.1 listenport=%ALIAS_PORT% connectaddress=127.0.0.1 connectport=%FRONTEND_PORT% >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] Redireccion configurada: 127.0.0.1:%ALIAS_PORT% -^> 127.0.0.1:%FRONTEND_PORT%.
) else (
    echo [!] No se pudo configurar la redireccion de puertos.
    echo     Podes igual acceder usando http://factura.com:%FRONTEND_PORT%
)

:: 7) Abrir regla firewall para loopback en puerto 80 (por si acaso)
netsh advfirewall firewall show rule name="VetFactura Loopback 80" >nul 2>&1
if %errorlevel% neq 0 (
    netsh advfirewall firewall add rule name="VetFactura Loopback 80" dir=in action=allow protocol=TCP localport=%ALIAS_PORT% profile=any >nul 2>&1
)
echo.

echo ══════════════════════════════════════════
echo   Instalacion completada correctamente
echo ══════════════════════════════════════════
echo.
echo   Para iniciar: doble clic en "start.bat"
echo   Luego abrí en el navegador:
echo       http://%ALIAS%
echo.
echo   (Si no funciona sin puerto, probá: http://%ALIAS%:%FRONTEND_PORT%)
echo.
pause
