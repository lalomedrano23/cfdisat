@echo off
echo ============================================
echo   CFDISAT - Instalador y Ejecutor
echo ============================================
echo.

echo [1] Instalando dependencias...
pip install -r requirements.txt
pip install satcfdi lxml beautifulsoup4 pyOpenSSL
echo.

echo [2] Verificando instalacion...
python -c "from app import create_app; app = create_app(); print('OK: Aplicacion verificada')"
echo.

echo [3] Iniciando servidor en http://localhost:5000
echo    Abre tu navegador en: http://localhost:5000
echo    Presiona Ctrl+C para detener
echo.
python app.py
pause
