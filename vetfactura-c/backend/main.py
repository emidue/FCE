# ─────────────────────────────────────────────────────────
#  VetFactura C — Backend FastAPI
#  Integración ARCA: WSAA + wsfe (Factura C = tipo 11)
#  Base de datos: SQLite (archivo local vetfactura.db)
#  Firma PKCS#7: cryptography (compatible ARCA)
# ─────────────────────────────────────────────────────────

from __future__ import annotations
import os, time, sqlite3, base64, json, smtplib, ssl, mimetypes
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Optional, List

import httpx
from lxml import etree

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv

from cryptography.hazmat.primitives.serialization import pkcs7, Encoding
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography import x509 as cx509

# ─── Cargar config ───────────────────────────────────────
load_dotenv()

CUIT        = os.getenv("ARCA_CUIT", "20304567891")
CERT_PATH   = Path(os.getenv("ARCA_CERT_PATH", "./certs/cert.pem"))
KEY_PATH    = Path(os.getenv("ARCA_KEY_PATH",  "./certs/key.pem"))
AMBIENTE    = os.getenv("ARCA_AMBIENTE", "homologacion")
PUNTO_VENTA = int(os.getenv("ARCA_PUNTO_VENTA", "1"))
DB_PATH     = os.getenv("DB_PATH", "./vetfactura.db")

WSAA_URL = {
    "homologacion": "https://wsaahomo.afip.gov.ar/ws/services/LoginCms",
    "produccion":   "https://wsaa.afip.gov.ar/ws/services/LoginCms",
}[AMBIENTE]

WSFE_URL = {
    "homologacion": "https://wswhomo.afip.gov.ar/wsfev1/service.asmx",
    "produccion":   "https://servicios1.afip.gov.ar/wsfev1/service.asmx",
}[AMBIENTE]

TIPO_CBTE_C = 11  # Factura C

# ─── Config persistente (logo, datos emisor, SMTP) ───────
CONFIG_DIR  = Path("./data")
CONFIG_FILE = CONFIG_DIR / "config.json"
LOGO_PATH   = CONFIG_DIR / "logo.png"
CONFIG_DIR.mkdir(exist_ok=True)

DEFAULT_CONFIG = {
    "emisor": {
        "razon_social": "Veterinaria San Martín",
        "domicilio":    "",
        "condicion_iva": "Monotributista",
    },
    "smtp": {
        "host": "",
        "port": 587,
        "user": "",
        "password": "",
        "from_email": "",
        "from_name": "VetFactura",
        "use_tls": True,
    },
}

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return json.loads(json.dumps(DEFAULT_CONFIG))
    # merge con defaults para garantizar campos
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    for k, v in data.items():
        if isinstance(v, dict) and k in merged:
            merged[k].update(v)
        else:
            merged[k] = v
    return merged

def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")

# ─── FastAPI app ─────────────────────────────────────────
app = FastAPI(title="VetFactura C", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────
# BASE DE DATOS (SQLite)
# ─────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS clientes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre      TEXT NOT NULL,
            tipo_doc    INTEGER NOT NULL DEFAULT 96,
            nro_doc     TEXT NOT NULL DEFAULT '0',
            email       TEXT,
            cond_iva    INTEGER NOT NULL DEFAULT 5,
            created_at  TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS facturas (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            punto_venta     INTEGER NOT NULL,
            nro_cbte        INTEGER NOT NULL,
            fecha_cbte      TEXT NOT NULL,
            concepto        INTEGER NOT NULL DEFAULT 2,
            tipo_doc        INTEGER NOT NULL DEFAULT 96,
            nro_doc         TEXT,
            receptor_nombre TEXT,
            receptor_email  TEXT,
            receptor_dom    TEXT,
            imp_total       REAL NOT NULL,
            cae             TEXT,
            vto_cae         TEXT,
            resultado       TEXT,
            observaciones   TEXT,
            created_at      TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS factura_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            factura_id  INTEGER REFERENCES facturas(id) ON DELETE CASCADE,
            descripcion TEXT NOT NULL,
            cantidad    REAL NOT NULL,
            precio_unit REAL NOT NULL,
            subtotal    REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tokens_wsaa (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            servicio    TEXT NOT NULL,
            token       TEXT NOT NULL,
            sign        TEXT NOT NULL,
            expira_en   TEXT NOT NULL,
            created_at  TEXT DEFAULT (datetime('now','localtime'))
        );
        """)
        # Migración: agregar cond_iva si no existe
        cols = [r[1] for r in db.execute("PRAGMA table_info(clientes)").fetchall()]
        if "cond_iva" not in cols:
            db.execute("ALTER TABLE clientes ADD COLUMN cond_iva INTEGER NOT NULL DEFAULT 5")
        if "domicilio" not in cols:
            db.execute("ALTER TABLE clientes ADD COLUMN domicilio TEXT DEFAULT ''")
        # Migración: agregar columnas nuevas a facturas
        fcols = [r[1] for r in db.execute("PRAGMA table_info(facturas)").fetchall()]
        for col, ddl in [
            ("fch_serv_desde",    "TEXT"),
            ("fch_serv_hasta",    "TEXT"),
            ("fch_vto_pago",      "TEXT"),
            ("cond_iva_receptor", "INTEGER"),
            ("cond_venta",        "TEXT"),
        ]:
            if col not in fcols:
                db.execute(f"ALTER TABLE facturas ADD COLUMN {col} {ddl}")

init_db()

# ─────────────────────────────────────────────────────────
# WSAA — Autenticación con ARCA
# ─────────────────────────────────────────────────────────
def _crear_tra(servicio: str) -> bytes:
    ahora     = datetime.utcnow()
    gen_time  = (ahora - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    exp_time  = (ahora + timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    unique_id = int(time.time())
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<loginTicketRequest version="1.0">
  <header>
    <uniqueId>{unique_id}</uniqueId>
    <generationTime>{gen_time}</generationTime>
    <expirationTime>{exp_time}</expirationTime>
  </header>
  <service>{servicio}</service>
</loginTicketRequest>""".encode("utf-8")


def _firmar_tra(tra: bytes) -> str:
    """Firma el TRA con PKCS#7 CMS usando cryptography — compatible ARCA."""
    private_key = load_pem_private_key(KEY_PATH.read_bytes(), password=None)
    cert        = cx509.load_pem_x509_certificate(CERT_PATH.read_bytes())

    signed_der = (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(tra)
        .add_signer(cert, private_key, hashes.SHA256())
        .sign(Encoding.DER, [pkcs7.PKCS7Options.NoCapabilities])
    )

    return base64.b64encode(signed_der).decode("ascii")


async def obtener_token(servicio: str = "wsfe") -> dict:
    with get_db() as db:
        row = db.execute(
            """SELECT * FROM tokens_wsaa
               WHERE servicio=? AND expira_en > datetime('now','localtime')
               ORDER BY id DESC LIMIT 1""",
            (servicio,)
        ).fetchone()
        if row:
            return {"token": row["token"], "sign": row["sign"]}

    tra = _crear_tra(servicio)
    cms = _firmar_tra(tra)

    soap = f"""<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:wsaa="http://wsaa.view.sua.dvadac.desein.afip.gov.ar">
  <soapenv:Header/>
  <soapenv:Body>
    <wsaa:loginCms><wsaa:in0>{cms}</wsaa:in0></wsaa:loginCms>
  </soapenv:Body>
</soapenv:Envelope>"""

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            WSAA_URL, content=soap.encode(),
            headers={"Content-Type": "text/xml;charset=UTF-8", "SOAPAction": ""}
        )
        if not resp.is_success:
            raise HTTPException(status_code=502, detail=f"WSAA {resp.status_code}: {resp.text[:1000]}")

    root      = etree.fromstring(resp.content)
    inner_xml = root.find(".//{http://wsaa.view.sua.dvadac.desein.afip.gov.ar}loginCmsReturn").text
    ta        = etree.fromstring(inner_xml.encode())
    token     = ta.findtext(".//token")
    sign      = ta.findtext(".//sign")
    exp_text  = ta.findtext(".//expirationTime")

    expira = datetime.strptime(exp_text[:19], "%Y-%m-%dT%H:%M:%S")
    with get_db() as db:
        db.execute(
            "INSERT INTO tokens_wsaa (servicio,token,sign,expira_en) VALUES (?,?,?,?)",
            (servicio, token, sign, expira.strftime("%Y-%m-%d %H:%M:%S"))
        )
    return {"token": token, "sign": sign, "expira": expira.isoformat()}


# ─────────────────────────────────────────────────────────
# WS FE — Operaciones ARCA
# ─────────────────────────────────────────────────────────
def _auth_header(token: str, sign: str) -> str:
    return f"""<ar:Auth>
      <ar:Token>{token}</ar:Token>
      <ar:Sign>{sign}</ar:Sign>
      <ar:Cuit>{CUIT}</ar:Cuit>
    </ar:Auth>"""


async def _wsfe_call(action: str, body_inner: str) -> etree._Element:
    soap = f"""<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:ar="http://ar.gov.afip.dif.FEV1/">
  <soapenv:Header/>
  <soapenv:Body><ar:{action}>{body_inner}</ar:{action}></soapenv:Body>
</soapenv:Envelope>"""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            WSFE_URL, content=soap.encode(),
            headers={
                "Content-Type": "text/xml;charset=utf-8",
                "SOAPAction": f'"http://ar.gov.afip.dif.FEV1/{action}"',
            },
        )
    ct = resp.headers.get("content-type", "")
    if "xml" not in ct:
        raise HTTPException(
            status_code=502,
            detail=f"WSFE {resp.status_code} devolvió {ct}: {resp.text[:800]}",
        )
    root = etree.fromstring(resp.content)
    fault = root.find(".//{http://schemas.xmlsoap.org/soap/envelope/}Fault")
    if fault is not None:
        faultstring = fault.findtext("faultstring") or etree.tostring(fault, pretty_print=True).decode()
        raise HTTPException(status_code=502, detail=f"WSFE SOAP Fault: {faultstring}")
    return root


async def consultar_ultimo_nro(token: str, sign: str) -> int:
    root = await _wsfe_call("FECompUltimoAutorizado", f"""
      {_auth_header(token, sign)}
      <ar:PtoVta>{PUNTO_VENTA}</ar:PtoVta>
      <ar:CbteTipo>{TIPO_CBTE_C}</ar:CbteTipo>
    """)
    ns = "http://ar.gov.afip.dif.FEV1/"
    errs = root.findall(f".//{{{ns}}}Errors/{{{ns}}}Err")
    if errs:
        msgs = "; ".join(e.findtext(f"{{{ns}}}Msg") or "" for e in errs)
        raise HTTPException(status_code=502, detail=f"AFIP FECompUltimoAutorizado: {msgs}")
    nro = root.findtext(f".//{{{ns}}}CbteNro")
    return int(nro) if nro else 0


async def solicitar_cae(token: str, sign: str, datos: dict) -> dict:
    nro   = datos["nro_cbte"]
    fecha = datos["fecha_cbte"]
    total = f"{datos['imp_total']:.2f}"

    fechas_serv = ""
    if datos["concepto"] in (2, 3):
        fechas_serv = f"""
            <ar:FchServDesde>{datos['fch_serv_desde'] or fecha}</ar:FchServDesde>
            <ar:FchServHasta>{datos['fch_serv_hasta'] or fecha}</ar:FchServHasta>
            <ar:FchVtoPago>{datos['fch_vto_pago'] or fecha}</ar:FchVtoPago>"""

    root = await _wsfe_call("FECAESolicitar", f"""
      {_auth_header(token, sign)}
      <ar:FeCAEReq>
        <ar:FeCabReq>
          <ar:CantReg>1</ar:CantReg>
          <ar:PtoVta>{PUNTO_VENTA}</ar:PtoVta>
          <ar:CbteTipo>{TIPO_CBTE_C}</ar:CbteTipo>
        </ar:FeCabReq>
        <ar:FeDetReq>
          <ar:FECAEDetRequest>
            <ar:Concepto>{datos['concepto']}</ar:Concepto>
            <ar:DocTipo>{datos['tipo_doc']}</ar:DocTipo>
            <ar:DocNro>{datos['nro_doc']}</ar:DocNro>
            <ar:CbteDesde>{nro}</ar:CbteDesde>
            <ar:CbteHasta>{nro}</ar:CbteHasta>
            <ar:CbteFch>{fecha}</ar:CbteFch>
            <ar:ImpTotal>{total}</ar:ImpTotal>
            <ar:ImpTotConc>0</ar:ImpTotConc>
            <ar:ImpNeto>{total}</ar:ImpNeto>
            <ar:ImpOpEx>0</ar:ImpOpEx>
            <ar:ImpIVA>0</ar:ImpIVA>
            <ar:ImpTrib>0</ar:ImpTrib>{fechas_serv}
            <ar:MonId>PES</ar:MonId>
            <ar:MonCotiz>1</ar:MonCotiz>
            <ar:CondicionIVAReceptorId>{datos['cond_iva_receptor']}</ar:CondicionIVAReceptorId>
          </ar:FECAEDetRequest>
        </ar:FeDetReq>
      </ar:FeCAEReq>
    """)

    ns        = "http://ar.gov.afip.dif.FEV1/"
    resultado = root.findtext(f".//{{{ns}}}Resultado")
    if resultado != "A":
        obs = root.findtext(f".//{{{ns}}}Msg") or "Rechazado por ARCA"
        raise HTTPException(status_code=400, detail=f"ARCA rechazó: {obs}")

    return {
        "cae":     root.findtext(f".//{{{ns}}}CAE"),
        "vto_cae": root.findtext(f".//{{{ns}}}CAEFchVto"),
    }


# ─────────────────────────────────────────────────────────
# MODELOS Pydantic
# ─────────────────────────────────────────────────────────
class ItemFactura(BaseModel):
    descripcion: str
    cantidad:    float = 1
    precio_unit: float

class FacturaRequest(BaseModel):
    punto_venta:     int = 1
    fecha_cbte:      str
    concepto:        int = 2
    tipo_doc:        int = 96
    nro_doc:         str = "0"
    cond_iva_receptor: int = 5
    fch_serv_desde:  Optional[str] = None
    fch_serv_hasta:  Optional[str] = None
    fch_vto_pago:    Optional[str] = None
    receptor_nombre: Optional[str] = None
    receptor_email:  Optional[str] = None
    receptor_dom:    Optional[str] = None
    cond_venta:      Optional[str] = None
    observaciones:   Optional[str] = None
    items:           List[ItemFactura]
    imp_total:       float

class ClienteIn(BaseModel):
    nombre:    str
    tipo_doc:  int = 96
    nro_doc:   str = "0"
    email:     Optional[str] = None
    cond_iva:  int = 5
    domicilio: Optional[str] = ""

class EmisorIn(BaseModel):
    razon_social:       str
    domicilio:          Optional[str] = ""
    condicion_iva:      Optional[str] = ""
    cuit:               Optional[str] = ""
    ingresos_brutos:    Optional[str] = ""
    inicio_actividades: Optional[str] = ""
    domicilio_comercial: Optional[str] = ""

class SmtpIn(BaseModel):
    host:       str
    port:       int = 587
    user:       str
    password:   str = ""
    from_email: str
    from_name:  Optional[str] = "VetFactura"
    use_tls:    bool = True

class ConfigIn(BaseModel):
    emisor:         EmisorIn
    smtp:           SmtpIn
    nombre_sistema: Optional[str] = "VetFactura"

class EnviarEmailIn(BaseModel):
    to:      str
    subject: Optional[str] = None
    body:    Optional[str] = None


# ─────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "app": "VetFactura C", "ambiente": AMBIENTE}

@app.get("/wsaa/estado")
async def wsaa_estado():
    try:
        data = await obtener_token("wsfe")
        return {"ok": True, "ambiente": AMBIENTE, "expira": data.get("expira")}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/wsaa/renovar")
async def wsaa_renovar():
    with get_db() as db:
        db.execute("DELETE FROM tokens_wsaa WHERE servicio='wsfe'")
    data = await obtener_token("wsfe")
    return {"ok": True, "expira": data.get("expira")}

@app.post("/facturas/emitir")
async def emitir_factura(req: FacturaRequest):
    auth     = await obtener_token("wsfe")
    ultimo   = await consultar_ultimo_nro(auth["token"], auth["sign"])
    nro_cbte = ultimo + 1

    datos_cae = await solicitar_cae(auth["token"], auth["sign"], {
        "nro_cbte":   nro_cbte,
        "fecha_cbte": req.fecha_cbte,
        "concepto":   req.concepto,
        "tipo_doc":   req.tipo_doc,
        "nro_doc":    req.nro_doc,
        "imp_total":  req.imp_total,
        "cond_iva_receptor": req.cond_iva_receptor,
        "fch_serv_desde": req.fch_serv_desde,
        "fch_serv_hasta": req.fch_serv_hasta,
        "fch_vto_pago":   req.fch_vto_pago,
    })

    with get_db() as db:
        cur = db.execute(
            """INSERT INTO facturas
               (punto_venta, nro_cbte, fecha_cbte, concepto, tipo_doc, nro_doc,
                receptor_nombre, receptor_email, receptor_dom,
                imp_total, cae, vto_cae, resultado, observaciones,
                fch_serv_desde, fch_serv_hasta, fch_vto_pago,
                cond_iva_receptor, cond_venta)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'A',?,?,?,?,?,?)""",
            (PUNTO_VENTA, nro_cbte, req.fecha_cbte, req.concepto,
             req.tipo_doc, req.nro_doc, req.receptor_nombre,
             req.receptor_email, req.receptor_dom, req.imp_total,
             datos_cae["cae"], datos_cae["vto_cae"], req.observaciones,
             req.fch_serv_desde, req.fch_serv_hasta, req.fch_vto_pago,
             req.cond_iva_receptor, req.cond_venta)
        )
        factura_id = cur.lastrowid
        for item in req.items:
            db.execute(
                "INSERT INTO factura_items (factura_id,descripcion,cantidad,precio_unit,subtotal) VALUES (?,?,?,?,?)",
                (factura_id, item.descripcion, item.cantidad,
                 item.precio_unit, item.cantidad * item.precio_unit)
            )

    return {
        "id": factura_id, "punto_venta": PUNTO_VENTA,
        "nro_cbte": nro_cbte, "receptor_nombre": req.receptor_nombre,
        "imp_total": req.imp_total, **datos_cae,
    }

@app.get("/facturas")
def listar_facturas(limit: int = 100):
    with get_db() as db:
        rows = db.execute("SELECT * FROM facturas ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]

@app.get("/facturas/{id}")
def obtener_factura(id: int):
    with get_db() as db:
        row   = db.execute("SELECT * FROM facturas WHERE id=?", (id,)).fetchone()
        if not row: raise HTTPException(404, "Factura no encontrada")
        items = db.execute("SELECT * FROM factura_items WHERE factura_id=?", (id,)).fetchall()
    return {**dict(row), "items": [dict(i) for i in items]}

COND_IVA_RECEPTOR = {
    1:  "IVA Responsable Inscripto",
    4:  "IVA Sujeto Exento",
    5:  "Consumidor Final",
    6:  "Responsable Monotributo",
    7:  "Sujeto No Categorizado",
    8:  "Proveedor del Exterior",
    9:  "Cliente del Exterior",
    10: "IVA Liberado – Ley N° 19.640",
    13: "Monotributista Social",
    15: "IVA No Alcanzado",
    16: "Monotributo Trabajador Independiente Promovido",
}

def _fmt_date(s: Optional[str]) -> str:
    if not s: return ""
    s = str(s)
    if len(s) == 8 and s.isdigit():
        return f"{s[6:]}/{s[4:6]}/{s[:4]}"
    return s

def _fmt_money(x: float) -> str:
    s = f"{x:,.2f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")

def _qr_arca_image(f: dict, cuit_emisor: str):
    try:
        import qrcode
    except Exception:
        return None
    fecha = f.get("fecha_cbte") or ""
    if len(fecha) == 8:
        fecha = f"{fecha[:4]}-{fecha[4:6]}-{fecha[6:]}"
    payload = {
        "ver": 1,
        "fecha": fecha,
        "cuit": int(cuit_emisor) if cuit_emisor.isdigit() else cuit_emisor,
        "ptoVta": f["punto_venta"],
        "tipoCmp": TIPO_CBTE_C,
        "nroCmp": f["nro_cbte"],
        "importe": float(f["imp_total"]),
        "moneda": "PES",
        "ctz": 1,
        "tipoDocRec": f.get("tipo_doc") or 99,
        "nroDocRec": int(f["nro_doc"]) if (f.get("nro_doc") or "").isdigit() else 0,
        "tipoCodAut": "E",
        "codAut": int(f["cae"]) if (f.get("cae") or "").isdigit() else f.get("cae"),
    }
    data = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    url  = "https://www.afip.gob.ar/fe/qr/?p=" + data
    return qrcode.make(url)


def _draw_page(c, w, h, copia: str, f: dict, items, cfg: dict):
    from reportlab.lib.units import cm
    from reportlab.lib.utils import ImageReader
    import io as _io

    emisor = cfg.get("emisor", {})
    razon  = emisor.get("razon_social") or "Emisor"
    dom    = emisor.get("domicilio") or ""
    dom_c  = emisor.get("domicilio_comercial") or dom
    cond_e = emisor.get("condicion_iva") or "Responsable Monotributo"
    cuit_e = emisor.get("cuit") or CUIT
    iibb   = emisor.get("ingresos_brutos") or ""
    ini_act = _fmt_date(emisor.get("inicio_actividades") or "")

    margin_l, margin_r = 1.2*cm, 1.2*cm
    x0, x1 = margin_l, w - margin_r
    y_top  = h - 1.2*cm

    # Banda superior "ORIGINAL / DUPLICADO / TRIPLICADO"
    c.setStrokeColorRGB(0,0,0); c.setLineWidth(0.8)
    band_h = 0.75*cm
    c.rect(x0, y_top - band_h, x1 - x0, band_h, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString((x0 + x1)/2, y_top - band_h + 0.22*cm, copia)

    # Cabecera (3 columnas): izquierda / centro (C) / derecha
    head_top    = y_top - band_h
    head_h      = 3.3*cm
    center_w    = 2.6*cm
    center_x    = (x0 + x1)/2 - center_w/2
    left_x, left_w   = x0, center_x - x0
    right_x, right_w = center_x + center_w, x1 - (center_x + center_w)

    # Caja izquierda
    c.rect(left_x, head_top - head_h, left_w, head_h, stroke=1, fill=0)
    # Título razón social grande
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(left_x + left_w/2, head_top - 0.65*cm, razon.upper())
    # Logo (opcional, pequeño a la izquierda)
    if LOGO_PATH.exists():
        try:
            img = ImageReader(str(LOGO_PATH))
            iw, ih = img.getSize()
            max_w, max_h = 1.8*cm, 1.6*cm
            scale = min(max_w/iw, max_h/ih)
            c.drawImage(img, left_x + 0.2*cm, head_top - 2.2*cm,
                        width=iw*scale, height=ih*scale,
                        preserveAspectRatio=True, mask='auto')
        except Exception:
            pass
    ly = head_top - 1.15*cm
    c.setFont("Helvetica-Bold", 8); c.drawString(left_x + 0.2*cm, ly, "Razón Social: ")
    c.setFont("Helvetica", 8);      c.drawString(left_x + 2.3*cm, ly, razon)
    ly -= 0.5*cm
    c.setFont("Helvetica-Bold", 8); c.drawString(left_x + 0.2*cm, ly, "Domicilio Comercial: ")
    c.setFont("Helvetica", 8);      c.drawString(left_x + 3.2*cm, ly, dom_c[:70])
    ly -= 0.5*cm
    c.setFont("Helvetica-Bold", 8); c.drawString(left_x + 0.2*cm, ly, "Condición frente al IVA: ")
    c.setFont("Helvetica", 8);      c.drawString(left_x + 3.7*cm, ly, cond_e)

    # Caja centro (letra C)
    c.rect(center_x, head_top - head_h, center_w, head_h, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 48)
    c.drawCentredString(center_x + center_w/2, head_top - 2.1*cm, "C")
    c.setFont("Helvetica-Bold", 8)
    c.drawCentredString(center_x + center_w/2, head_top - 2.7*cm, "COD. 011")

    # Caja derecha
    c.rect(right_x, head_top - head_h, right_w, head_h, stroke=1, fill=0)
    ry = head_top - 0.55*cm
    c.setFont("Helvetica-Bold", 14)
    c.drawString(right_x + 0.2*cm, ry, "FACTURA")
    ry -= 0.6*cm
    c.setFont("Helvetica-Bold", 8); c.drawString(right_x + 0.2*cm, ry, "Punto de Venta: ")
    c.setFont("Helvetica-Bold", 9); c.drawString(right_x + 2.7*cm, ry, str(f["punto_venta"]).zfill(5))
    c.setFont("Helvetica-Bold", 8); c.drawString(right_x + 4.3*cm, ry, "Comp. Nro: ")
    c.setFont("Helvetica-Bold", 9); c.drawString(right_x + 6.1*cm, ry, str(f["nro_cbte"]).zfill(8))
    ry -= 0.45*cm
    c.setFont("Helvetica-Bold", 8); c.drawString(right_x + 0.2*cm, ry, "Fecha de Emisión: ")
    c.setFont("Helvetica", 8);      c.drawString(right_x + 3.0*cm, ry, _fmt_date(f.get("fecha_cbte")))
    ry -= 0.45*cm
    c.setFont("Helvetica-Bold", 8); c.drawString(right_x + 0.2*cm, ry, "CUIT: ")
    c.setFont("Helvetica", 8);      c.drawString(right_x + 1.3*cm, ry, str(cuit_e))
    ry -= 0.45*cm
    c.setFont("Helvetica-Bold", 8); c.drawString(right_x + 0.2*cm, ry, "Ingresos Brutos: ")
    c.setFont("Helvetica", 8);      c.drawString(right_x + 2.9*cm, ry, iibb)
    ry -= 0.45*cm
    c.setFont("Helvetica-Bold", 8); c.drawString(right_x + 0.2*cm, ry, "Fecha de Inicio de Actividades: ")
    c.setFont("Helvetica", 8);      c.drawString(right_x + 4.9*cm, ry, ini_act)

    # Fila "Período Facturado"
    per_top = head_top - head_h
    per_h   = 0.75*cm
    c.rect(x0, per_top - per_h, x1 - x0, per_h, stroke=1, fill=0)
    py = per_top - per_h + 0.22*cm
    desde = _fmt_date(f.get("fch_serv_desde") or f.get("fecha_cbte"))
    hasta = _fmt_date(f.get("fch_serv_hasta") or f.get("fecha_cbte"))
    vto_p = _fmt_date(f.get("fch_vto_pago")   or f.get("fecha_cbte"))
    c.setFont("Helvetica-Bold", 8); c.drawString(x0 + 0.2*cm, py, "Período Facturado Desde:")
    c.setFont("Helvetica", 8);      c.drawString(x0 + 4.1*cm, py, desde)
    c.setFont("Helvetica-Bold", 8); c.drawString(x0 + 6.2*cm, py, "Hasta:")
    c.setFont("Helvetica", 8);      c.drawString(x0 + 7.3*cm, py, hasta)
    c.setFont("Helvetica-Bold", 8); c.drawString(x0 + 10.0*cm, py, "Fecha de Vto. para el pago:")
    c.setFont("Helvetica", 8);      c.drawString(x0 + 14.4*cm, py, vto_p)

    # Datos del receptor (cliente)
    cli_top = per_top - per_h
    cli_h   = 2.1*cm
    c.rect(x0, cli_top - cli_h, x1 - x0, cli_h, stroke=1, fill=0)
    cy = cli_top - 0.45*cm
    cuit_r = f.get("nro_doc") or ""
    nombre_r = f.get("receptor_nombre") or "Consumidor Final"
    dom_r    = f.get("receptor_dom") or ""
    cond_r   = COND_IVA_RECEPTOR.get(f.get("cond_iva_receptor") or 0, "")
    cond_v   = f.get("cond_venta") or "Contado"
    c.setFont("Helvetica-Bold", 8); c.drawString(x0 + 0.2*cm, cy, "CUIT:")
    c.setFont("Helvetica", 8);      c.drawString(x0 + 1.2*cm, cy, str(cuit_r))
    c.setFont("Helvetica-Bold", 8); c.drawString(x0 + 5.5*cm, cy, "Apellido y Nombre / Razón Social:")
    c.setFont("Helvetica", 8);      c.drawString(x0 + 10.4*cm, cy, nombre_r[:55])
    cy -= 0.5*cm
    c.setFont("Helvetica-Bold", 8); c.drawString(x0 + 0.2*cm, cy, "Condición frente al IVA:")
    c.setFont("Helvetica", 8);      c.drawString(x0 + 3.7*cm, cy, cond_r)
    c.setFont("Helvetica-Bold", 8); c.drawString(x0 + 10.0*cm, cy, "Domicilio:")
    c.setFont("Helvetica", 8);      c.drawString(x0 + 11.5*cm, cy, dom_r[:50])
    cy -= 0.5*cm
    c.setFont("Helvetica-Bold", 8); c.drawString(x0 + 0.2*cm, cy, "Condición de venta:")
    c.setFont("Helvetica", 8);      c.drawString(x0 + 3.0*cm, cy, cond_v)

    # Tabla de ítems
    tbl_top = cli_top - cli_h - 0.15*cm
    headers = [
        ("Código",            1.6*cm),
        ("Producto / Servicio", 6.8*cm),
        ("Cantidad",          1.8*cm),
        ("U. Medida",         1.7*cm),
        ("Precio Unit.",      2.0*cm),
        ("% Bonif",           1.3*cm),
        ("Imp. Bonif.",       1.6*cm),
        ("Subtotal",          1.8*cm),
    ]
    total_w = sum(cw for _, cw in headers)
    scale = (x1 - x0) / total_w
    headers = [(t, cw * scale) for t, cw in headers]

    row_h = 0.6*cm
    c.setFillColorRGB(0.92, 0.92, 0.92)
    c.rect(x0, tbl_top - row_h, x1 - x0, row_h, stroke=1, fill=1)
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 8)
    cx = x0
    col_x = []
    for title, cw in headers:
        col_x.append(cx)
        c.drawString(cx + 0.1*cm, tbl_top - row_h + 0.2*cm, title)
        cx += cw
    col_x.append(x1)

    # Filas
    y = tbl_top - row_h
    c.setFont("Helvetica", 8)
    for it in items:
        y -= row_h
        desc  = str(it["descripcion"])
        cant  = it["cantidad"]
        pu    = it["precio_unit"]
        sub   = it["subtotal"]
        # Código vacío, U. Medida "unidades", bonif 0
        c.drawString(col_x[0] + 0.1*cm, y + 0.2*cm, "")
        c.drawString(col_x[1] + 0.1*cm, y + 0.2*cm, desc[:55])
        c.drawRightString(col_x[3] - 0.1*cm, y + 0.2*cm, f"{cant:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
        c.drawString(col_x[3] + 0.1*cm, y + 0.2*cm, "unidades")
        c.drawRightString(col_x[5] - 0.1*cm, y + 0.2*cm, _fmt_money(pu))
        c.drawRightString(col_x[6] - 0.1*cm, y + 0.2*cm, "0,00")
        c.drawRightString(col_x[7] - 0.1*cm, y + 0.2*cm, "0,00")
        c.drawRightString(col_x[8] - 0.1*cm, y + 0.2*cm, _fmt_money(sub))

    # Bloque totales (abajo a la derecha)
    tot_w  = 7.0*cm
    tot_h  = 2.4*cm
    tot_x  = x1 - tot_w
    tot_y  = 4.2*cm
    c.rect(tot_x, tot_y, tot_w, tot_h, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(tot_x + tot_w - 3.0*cm, tot_y + tot_h - 0.7*cm, "Subtotal: $")
    c.setFont("Helvetica", 10)
    c.drawRightString(tot_x + tot_w - 0.2*cm, tot_y + tot_h - 0.7*cm, _fmt_money(f["imp_total"]))
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(tot_x + tot_w - 3.0*cm, tot_y + tot_h - 1.4*cm, "Importe Otros Tributos: $")
    c.setFont("Helvetica", 10)
    c.drawRightString(tot_x + tot_w - 0.2*cm, tot_y + tot_h - 1.4*cm, "0,00")
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(tot_x + tot_w - 3.0*cm, tot_y + tot_h - 2.1*cm, "Importe Total: $")
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(tot_x + tot_w - 0.2*cm, tot_y + tot_h - 2.1*cm, _fmt_money(f["imp_total"]))

    # Pie: QR + ARCA + CAE + Pág
    # QR
    qr_img = _qr_arca_image(f, str(cuit_e))
    qr_size = 2.8*cm
    qr_x, qr_y = x0, 0.6*cm
    if qr_img is not None:
        bio = _io.BytesIO()
        qr_img.save(bio, format="PNG")
        bio.seek(0)
        c.drawImage(ImageReader(bio), qr_x, qr_y, width=qr_size, height=qr_size, mask='auto')
    else:
        c.rect(qr_x, qr_y, qr_size, qr_size, stroke=1, fill=0)
        c.setFont("Helvetica", 6)
        c.drawCentredString(qr_x + qr_size/2, qr_y + qr_size/2, "QR")

    # Bloque ARCA
    arca_x = qr_x + qr_size + 0.3*cm
    c.setFont("Helvetica-Bold", 14)
    c.drawString(arca_x, qr_y + qr_size - 0.7*cm, "ARCA")
    c.setFont("Helvetica", 6)
    c.drawString(arca_x, qr_y + qr_size - 1.1*cm, "AGENCIA DE RECAUDACIÓN")
    c.drawString(arca_x, qr_y + qr_size - 1.35*cm, "Y CONTROL ADUANERO")
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(arca_x, qr_y + 0.5*cm, "Comprobante Autorizado")
    c.setFont("Helvetica-Oblique", 6)
    c.drawString(arca_x, qr_y + 0.1*cm,
                 "Esta Agencia no se responsabiliza por los datos ingresados en el detalle de la operación")

    # CAE y página (derecha)
    c.setFont("Helvetica", 9)
    c.drawCentredString((x0 + x1)/2, qr_y + qr_size - 0.7*cm, "Pág. 1/1")
    c.setFont("Helvetica-Bold", 9)
    c.drawRightString(x1, qr_y + qr_size - 0.7*cm, f"CAE N°:  {f.get('cae','')}")
    c.drawRightString(x1, qr_y + qr_size - 1.15*cm, f"Fecha de Vto. de CAE:  {_fmt_date(f.get('vto_cae'))}")


def _build_factura_pdf(id: int) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    import io

    with get_db() as db:
        f     = db.execute("SELECT * FROM facturas WHERE id=?", (id,)).fetchone()
        items = db.execute("SELECT * FROM factura_items WHERE factura_id=?", (id,)).fetchall()
    if not f: raise HTTPException(404, "Factura no encontrada")
    f = dict(f)
    items = [dict(i) for i in items]

    cfg = load_config()

    buf = io.BytesIO()
    c   = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    for copia in ("ORIGINAL", "DUPLICADO", "TRIPLICADO"):
        _draw_page(c, w, h, copia, f, items, cfg)
        c.showPage()
    c.save()
    return buf.getvalue()


@app.get("/facturas/{id}/pdf")
async def pdf_factura(id: int):
    try:
        pdf_bytes = _build_factura_pdf(id)
    except ImportError:
        raise HTTPException(500, "Instalar reportlab: pip install reportlab")
    import io
    return StreamingResponse(io.BytesIO(pdf_bytes), media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=factura_c_{id}.pdf"})

@app.get("/clientes")
def listar_clientes():
    with get_db() as db:
        rows = db.execute("""
            SELECT c.*, COUNT(f.id) as cant_facturas
            FROM clientes c LEFT JOIN facturas f ON f.receptor_nombre = c.nombre
            GROUP BY c.id ORDER BY c.nombre
        """).fetchall()
    return [dict(r) for r in rows]

@app.post("/clientes")
def crear_cliente(cliente: ClienteIn):
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO clientes (nombre,tipo_doc,nro_doc,email,cond_iva,domicilio) VALUES (?,?,?,?,?,?)",
            (cliente.nombre, cliente.tipo_doc, cliente.nro_doc, cliente.email, cliente.cond_iva, cliente.domicilio)
        )
    return {"id": cur.lastrowid, **cliente.dict()}

@app.put("/clientes/{id}")
def actualizar_cliente(id: int, cliente: ClienteIn):
    with get_db() as db:
        db.execute(
            "UPDATE clientes SET nombre=?, tipo_doc=?, nro_doc=?, email=?, cond_iva=?, domicilio=? WHERE id=?",
            (cliente.nombre, cliente.tipo_doc, cliente.nro_doc, cliente.email, cliente.cond_iva, cliente.domicilio, id)
        )
    return {"id": id, **cliente.dict()}

@app.delete("/clientes/{id}")
def eliminar_cliente(id: int):
    with get_db() as db:
        db.execute("DELETE FROM clientes WHERE id=?", (id,))
    return {"ok": True}


# ─────────────────────────────────────────────────────────
# INICIALIZAR BASE DE DATOS
# ─────────────────────────────────────────────────────────
@app.post("/admin/reset-db")
def reset_database(tablas: str = "all"):
    """Borra facturas y/o clientes. tablas: 'all', 'facturas', 'clientes'"""
    with get_db() as db:
        if tablas in ("all", "facturas"):
            db.execute("DELETE FROM factura_items")
            db.execute("DELETE FROM facturas")
        if tablas in ("all", "clientes"):
            db.execute("DELETE FROM clientes")
    return {"ok": True, "tablas": tablas}


# ─────────────────────────────────────────────────────────
# ESTADO DE SERVICIOS
# ─────────────────────────────────────────────────────────
@app.get("/status")
async def check_status():
    results = {}
    async with httpx.AsyncClient(timeout=10) as client:
        # WSAA
        try:
            r = await client.get(WSAA_URL)
            results["wsaa"] = {"ok": r.status_code < 500, "code": r.status_code}
        except Exception as e:
            results["wsaa"] = {"ok": False, "code": 0, "error": str(e)}

        # WSFE
        try:
            r = await client.get(WSFE_URL)
            results["wsfe"] = {"ok": r.status_code < 500, "code": r.status_code}
        except Exception as e:
            results["wsfe"] = {"ok": False, "code": 0, "error": str(e)}

    # WSAA auth real (intenta obtener/reusar token)
    try:
        tok = await obtener_token("wsfe")
        results["wsaa_auth"] = {"ok": True, "expira": tok.get("expira", "")}
    except Exception as e:
        detail = str(e.detail) if hasattr(e, 'detail') else str(e)
        results["wsaa_auth"] = {"ok": False, "error": detail[:200]}

    # Certificados
    results["cert"] = CERT_PATH.exists()
    results["key"] = KEY_PATH.exists()
    results["ambiente"] = AMBIENTE
    results["cuit"] = CUIT

    return results


# ─────────────────────────────────────────────────────────
# CONFIGURACIÓN (logo, datos emisor, SMTP)
# ─────────────────────────────────────────────────────────
@app.get("/config")
def get_config():
    cfg = load_config()
    # No exponer la password en GET
    cfg_safe = json.loads(json.dumps(cfg))
    if cfg_safe.get("smtp", {}).get("password"):
        cfg_safe["smtp"]["password"] = "********"
    cfg_safe["has_logo"] = LOGO_PATH.exists()
    cfg_safe["cuit"]     = CUIT
    cfg_safe["ambiente"] = AMBIENTE
    return cfg_safe

@app.post("/config")
def set_config(new: ConfigIn):
    global CUIT
    cfg = load_config()
    cfg["emisor"] = new.emisor.dict()
    new_smtp = new.smtp.dict()
    # Si el cliente manda password en blanco o el placeholder, conservar la anterior
    if not new_smtp["password"] or new_smtp["password"] == "********":
        new_smtp["password"] = cfg.get("smtp", {}).get("password", "")
    cfg["smtp"] = new_smtp
    cfg["nombre_sistema"] = new.nombre_sistema or "VetFactura"
    save_config(cfg)
    # Actualizar CUIT en memoria si cambió
    new_cuit = (new.emisor.cuit or "").strip()
    if new_cuit and new_cuit != CUIT:
        CUIT = new_cuit
        # Persistir en .env
        env_path = Path(__file__).parent / ".env"
        if env_path.exists():
            lines = env_path.read_text().splitlines()
            updated = False
            for i, line in enumerate(lines):
                if line.startswith("ARCA_CUIT="):
                    lines[i] = f"ARCA_CUIT={new_cuit}"
                    updated = True
                    break
            if not updated:
                lines.append(f"ARCA_CUIT={new_cuit}")
            env_path.write_text("\n".join(lines) + "\n")
    return {"ok": True}

@app.post("/config/logo")
async def upload_logo(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > 2 * 1024 * 1024:
        raise HTTPException(400, "Logo demasiado grande (máx 2 MB)")
    LOGO_PATH.write_bytes(data)
    return {"ok": True, "size": len(data)}

@app.delete("/config/logo")
def delete_logo():
    if LOGO_PATH.exists():
        LOGO_PATH.unlink()
    return {"ok": True}

@app.get("/config/logo")
def get_logo():
    if not LOGO_PATH.exists():
        raise HTTPException(404, "No hay logo cargado")
    mt, _ = mimetypes.guess_type(str(LOGO_PATH))
    return FileResponse(LOGO_PATH, media_type=mt or "image/png")


# ─────────────────────────────────────────────────────────
# ENVÍO DE FACTURA POR EMAIL
# ─────────────────────────────────────────────────────────
def _send_email_with_pdf(to_addr: str, subject: str, body: str, pdf_bytes: bytes, filename: str):
    cfg  = load_config()
    smtp = cfg.get("smtp", {})
    if not (smtp.get("host") and smtp.get("from_email")):
        raise HTTPException(400, "SMTP no configurado. Ir a Configuración.")

    msg = EmailMessage()
    from_name = smtp.get("from_name") or smtp["from_email"]
    msg["From"]    = f"{from_name} <{smtp['from_email']}>"
    msg["To"]      = to_addr
    msg["Subject"] = subject
    msg.set_content(body or "Adjuntamos su comprobante.")
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=filename)

    host = smtp["host"]
    port = int(smtp.get("port") or 587)
    user = smtp.get("user") or smtp["from_email"]
    pwd  = smtp.get("password") or ""
    use_tls = bool(smtp.get("use_tls", True))

    try:
        if port == 465:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
                if user and pwd:
                    s.login(user, pwd)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.ehlo()
                if use_tls:
                    ctx = ssl.create_default_context()
                    s.starttls(context=ctx)
                    s.ehlo()
                if user and pwd:
                    s.login(user, pwd)
                s.send_message(msg)
    except Exception as e:
        raise HTTPException(502, f"Error SMTP: {e}")

@app.post("/facturas/{id}/email")
def enviar_factura_email(id: int, req: EnviarEmailIn):
    with get_db() as db:
        f = db.execute("SELECT * FROM facturas WHERE id=?", (id,)).fetchone()
    if not f: raise HTTPException(404, "Factura no encontrada")
    f = dict(f)

    to_addr = (req.to or f.get("receptor_email") or "").strip()
    if not to_addr:
        raise HTTPException(400, "Falta dirección de email destinataria")

    try:
        pdf_bytes = _build_factura_pdf(id)
    except ImportError:
        raise HTTPException(500, "Instalar reportlab: pip install reportlab")

    nro = f"{str(f['punto_venta']).zfill(4)}-{str(f['nro_cbte']).zfill(8)}"
    subject = req.subject or f"Factura C {nro}"
    body    = req.body or (
        f"Estimado/a {f.get('receptor_nombre') or 'cliente'},\n\n"
        f"Adjuntamos la Factura C {nro} por $ {f['imp_total']:.2f}.\n"
        f"CAE: {f.get('cae','—')}\n\n"
        f"Saludos."
    )
    _send_email_with_pdf(to_addr, subject, body, pdf_bytes, f"factura_c_{nro}.pdf")
    return {"ok": True, "to": to_addr}