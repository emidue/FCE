# VetFactura C — Guía de Instalación y Uso

## Stack elegido

| Capa | Tecnología |
|---|---|
| Frontend | HTML + CSS + JS puro (sin dependencias) |
| Backend | **Python 3.11+ + FastAPI** |
| Base de datos | **SQLite** (archivo `vetfactura.db`) |
| ARCA WS | WSAA + wsfe (FECAESolicitar) |
| PDF | ReportLab |

---

## Estructura del proyecto

```
vetfactura-c/
├── frontend/
│   ├── index.html      ← Interfaz web completa
│   ├── styles.css
│   └── app.js
└── backend/
    ├── main.py         ← FastAPI: WSAA + FECAESolicitar + SQLite
    ├── requirements.txt
    ├── .env.example    ← Copiar a .env y completar
    └── certs/          ← ⚠️ Crear esta carpeta, NO subir a Git
        ├── cert.pem
        └── key.pem
```

---

## Instalación rápida

### 1. Preparar el entorno Python

```bash
cd backend
python -m venv venv
source venv/bin/activate       # Linux/Mac
# venv\Scripts\activate        # Windows

pip install -r requirements.txt
```

### 2. Configurar variables de entorno

```bash
cp .env.example .env
# Editar .env con tu CUIT y rutas de certificados
```

### 3. Generar certificados (si no los tenés)

```bash
mkdir certs
# Generar clave privada
openssl genrsa -out certs/key.pem 2048

# Generar certificado autofirmado
openssl req -new -x509 -key certs/key.pem \
  -subj "/C=AR/O=Veronica Gadea/CN=CUIT 27348095655/serialNumber=CUIT 27348095655" \
  -days 1825 -out certs/cert.pem
```

Luego subir `cert.pem` al portal ARCA:
- Ingresar a https://auth.afip.gob.ar con clave fiscal
- **Servicios → Administración de Certificados Digitales**
- Agregar nuevo → pegar contenido de `cert.pem`
- Asociar el servicio **`wsfe`** al certificado

### 4. Dar de alta el punto de venta en ARCA

- En ARCA/AFIP: **Servicios → Administración de puntos de venta y domicilios**
- Agregar punto de venta → tipo **"Web Services"** → número `0001`

### 5. Iniciar el backend

```bash
uvicorn main:app --reload --port 8000
```

La API quedará disponible en: http://localhost:8000  
Documentación automática en: http://localhost:8000/docs

### 6. Abrir el frontend

```bash
# Simplemente abrir en el navegador:
open frontend/index.html
# O servir con Python:
cd frontend && python -m http.server 3000
```

---

## Endpoints de la API

| Método | URL | Descripción |
|---|---|---|
| GET | `/` | Estado del servicio |
| GET | `/wsaa/estado` | Estado del token WSAA |
| POST | `/wsaa/renovar` | Forzar renovación del token |
| POST | `/facturas/emitir` | **Emitir Factura C via ARCA** |
| GET | `/facturas` | Listar historial |
| GET | `/facturas/{id}` | Detalle de una factura |
| GET | `/facturas/{id}/pdf` | Descargar PDF |
| GET | `/clientes` | Listar clientes |
| POST | `/clientes` | Crear cliente |
| DELETE | `/clientes/{id}` | Eliminar cliente |

### Ejemplo de payload — POST /facturas/emitir

```json
{
  "punto_venta": 1,
  "fecha_cbte": "20260331",
  "concepto": 2,
  "tipo_doc": 96,
  "nro_doc": "33456789",
  "receptor_nombre": "Juan Pérez",
  "receptor_email": "juan@email.com",
  "imp_total": 8500.00,
  "items": [
    { "descripcion": "Consulta clínica",   "cantidad": 1, "precio_unit": 5000 },
    { "descripcion": "Vacuna antirrábica", "cantidad": 1, "precio_unit": 3500 }
  ]
}
```

### Respuesta exitosa

```json
{
  "id": 42,
  "punto_venta": 1,
  "nro_cbte": 43,
  "receptor_nombre": "Juan Pérez",
  "imp_total": 8500.00,
  "cae": "74123456789012",
  "vto_cae": "20260410"
}
```

---

## Factura C — Consideraciones importantes

- **Tipo de comprobante ARCA:** `11` (Factura C)
- **Para emisores:** Monotributistas o Exentos (condición IVA sin discriminación)
- **IVA:** No se discrimina en Factura C. El total incluye todo.
- En el XML de FECAESolicitar: `ImpNeto = ImpTotal`, `ImpIVA = 0`
- **Receptores válidos:** Consumidor Final, Monotributistas, Responsables Inscriptos, Exentos

---

## Checklist producción

- [ ] Certificado cargado en ARCA y asociado al servicio `wsfe`
- [ ] Punto de venta `0001` dado de alta como "Web Services" en ARCA
- [ ] Probar 3-5 facturas en ambiente **Homologación** primero
- [ ] Cambiar `ARCA_AMBIENTE=produccion` en `.env`
- [ ] Servir frontend y backend bajo HTTPS (usar Nginx + Certbot)
- [ ] Hacer backup periódico de `vetfactura.db`
- [ ] Nunca subir `certs/` ni `.env` a Git (agregar al `.gitignore`)

---

## .gitignore recomendado

```
certs/
.env
vetfactura.db
venv/
__pycache__/
*.pyc
```
