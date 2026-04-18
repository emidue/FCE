@echo off
chcp 65001 >nul
title VetFactura C - Quitar alias factura.com

net session >nul 2>&1
if %errorLevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

set HOSTS_FILE=%SystemRoot%\System32\drivers\etc\hosts
set ALIAS=factura.com
set ALIAS_PORT=80

echo.
echo [*] Quitando redireccion de puerto %ALIAS_PORT% ...
netsh interface portproxy delete v4tov4 listenaddress=127.0.0.1 listenport=%ALIAS_PORT% >nul 2>&1

echo [*] Quitando regla de firewall ...
netsh advfirewall firewall delete rule name="VetFactura Loopback 80" >nul 2>&1

echo [*] Quitando "%ALIAS%" del archivo hosts ...
set TMPFILE=%TEMP%\hosts_vetfactura.tmp
findstr /V /C:"%ALIAS%" "%HOSTS_FILE%" > "%TMPFILE%"
copy /Y "%TMPFILE%" "%HOSTS_FILE%" >nul
del "%TMPFILE%" >nul 2>&1

echo.
echo [OK] Alias y redireccion removidos.
echo.
pause
