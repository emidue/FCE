#!/usr/bin/env bash
# ─────────────────────────────────────────────
#  VetFactura C — Script de inicio
#  Levanta backend (FastAPI) y frontend (http.server)
# ─────────────────────────────────────────────
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$PROJECT_DIR/backend"
FRONTEND_DIR="$PROJECT_DIR/frontend"
VENV_DIR="$BACKEND_DIR/venv"

BACKEND_PORT=8000
FRONTEND_PORT=3000

# Colores
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

cleanup() {
    echo ""
    echo -e "${YELLOW}Deteniendo servicios...${NC}"
    [ -n "$BACKEND_PID" ] && kill "$BACKEND_PID" 2>/dev/null
    [ -n "$FRONTEND_PID" ] && kill "$FRONTEND_PID" 2>/dev/null
    wait 2>/dev/null
    echo -e "${GREEN}Servicios detenidos.${NC}"
    exit 0
}
trap cleanup SIGINT SIGTERM

# ─── 1. Verificar Python ────────────────────
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}Error: python3 no encontrado. Instalalo antes de continuar.${NC}"
    exit 1
fi

# ─── 2. Crear venv si no existe ─────────────
if [ ! -d "$VENV_DIR" ]; then
    echo -e "${YELLOW}Creando entorno virtual...${NC}"
    python3 -m venv "$VENV_DIR"
fi

# ─── 3. Activar venv e instalar dependencias ─
echo -e "${YELLOW}Instalando dependencias del backend...${NC}"
source "$VENV_DIR/bin/activate"
pip install -q -r "$BACKEND_DIR/requirements.txt"

# ─── 4. Verificar .env ─────────────────────
if [ ! -f "$BACKEND_DIR/.env" ]; then
    echo -e "${YELLOW}Archivo .env no encontrado. Copiando desde .env.example...${NC}"
    cp "$BACKEND_DIR/.env.example" "$BACKEND_DIR/.env"
    echo -e "${YELLOW}Editá $BACKEND_DIR/.env con tus datos reales.${NC}"
fi

# ─── 5. Iniciar backend ────────────────────
echo -e "${GREEN}Iniciando backend (FastAPI) en puerto $BACKEND_PORT...${NC}"
cd "$BACKEND_DIR"
uvicorn main:app --reload --host 0.0.0.0 --port "$BACKEND_PORT" &
BACKEND_PID=$!

# ─── 6. Iniciar frontend ───────────────────
echo -e "${GREEN}Iniciando frontend en puerto $FRONTEND_PORT...${NC}"
cd "$FRONTEND_DIR"
python3 -m http.server "$FRONTEND_PORT" --bind 0.0.0.0 &
FRONTEND_PID=$!

# ─── 7. Listo ──────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════${NC}"
echo -e "${GREEN}  VetFactura C levantado correctamente${NC}"
echo -e "${GREEN}═══════════════════════════════════════════${NC}"
echo -e "  Backend  (API):  ${YELLOW}http://localhost:$BACKEND_PORT${NC}"
echo -e "  Frontend (Web):  ${YELLOW}http://localhost:$FRONTEND_PORT${NC}"
echo -e "  API docs:        ${YELLOW}http://localhost:$BACKEND_PORT/docs${NC}"
echo -e "${GREEN}═══════════════════════════════════════════${NC}"
echo -e "  Presioná ${RED}Ctrl+C${NC} para detener todo"
echo ""

# Esperar a que terminen (o Ctrl+C)
wait
