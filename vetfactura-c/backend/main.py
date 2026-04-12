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
    observaciones:   Optional[str] = None
    items:           List[ItemFactura]
    imp_total:       float

class ClienteIn(BaseModel):
    nombre:   str
    tipo_doc: int = 96
    nro_doc:  str = "0"
    email:    Optional[str] = None
    cond_iva: int = 5

class EmisorIn(BaseModel):
    razon_social:  str
    domicilio:     Optional[str] = ""
    condicion_iva: Optional[str] = ""

class SmtpIn(BaseModel):
    host:       str
    port:       int = 587
    user:       str
    password:   str = ""
    from_email: str
    from_name:  Optional[str] = "VetFactura"
    use_tls:    bool = True

class ConfigIn(BaseModel):
    emisor: EmisorIn
    smtp:   SmtpIn

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
                imp_total, cae, vto_cae, resultado, observaciones)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'A',?)""",
            (PUNTO_VENTA, nro_cbte, req.fecha_cbte, req.concepto,
             req.tipo_doc, req.nro_doc, req.receptor_nombre,
             req.receptor_email, req.receptor_dom, req.imp_total,
             datos_cae["cae"], datos_cae["vto_cae"], req.observaciones)
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

def _build_factura_pdf(id: int) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import cm
    from reportlab.lib.utils import ImageReader
    import io

    with get_db() as db:
        f     = db.execute("SELECT * FROM facturas WHERE id=?", (id,)).fetchone()
        items = db.execute("SELECT * FROM factura_items WHERE factura_id=?", (id,)).fetchall()
    if not f: raise HTTPException(404, "Factura no encontrada")
    f = dict(f)

    cfg    = load_config()
    emisor = cfg.get("emisor", {})
    razon  = emisor.get("razon_social") or "Emisor"
    dom    = emisor.get("domicilio") or ""
    cond   = emisor.get("condicion_iva") or ""

    buf = io.BytesIO()
    c   = canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    # Logo (si hay)
    if LOGO_PATH.exists():
        try:
            img = ImageReader(str(LOGO_PATH))
            iw, ih = img.getSize()
            max_w, max_h = 3.5*cm, 3*cm
            scale = min(max_w/iw, max_h/ih)
            c.drawImage(img, 2*cm, h-2*cm-ih*scale, width=iw*scale, height=ih*scale,
                        preserveAspectRatio=True, mask='auto')
            text_x = 2*cm + iw*scale + 0.5*cm
        except Exception:
            text_x = 2*cm
    else:
        text_x = 2*cm

    c.setFillColorRGB(0,0,0)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(text_x, h-2*cm, razon)
    c.setFont("Helvetica", 9)
    linea2 = f"CUIT: {CUIT}"
    if dom:  linea2 += f" | {dom}"
    c.drawString(text_x, h-2.6*cm, linea2)
    if cond:
        c.drawString(text_x, h-3.1*cm, f"Condición IVA: {cond}")

    c.setStrokeColorRGB(.18,.42,.31); c.setLineWidth(2)
    c.rect(w/2-2*cm, h-3.2*cm, 4*cm, 2.2*cm)
    c.setFont("Helvetica-Bold", 28); c.setFillColorRGB(.18,.42,.31)
    c.drawCentredString(w/2, h-2.5*cm, "C")
    c.setFont("Helvetica", 7); c.setFillColorRGB(.4,.4,.4)
    c.drawCentredString(w/2, h-2.9*cm, "FACTURA")
    c.drawCentredString(w/2, h-3.1*cm,
        f"{str(f['punto_venta']).zfill(4)}-{str(f['nro_cbte']).zfill(8)}")

    c.setFillColorRGB(0,0,0); c.setFont("Helvetica", 9)
    fecha = f['fecha_cbte']
    if len(fecha) == 8: fecha = f"{fecha[6:]}/{fecha[4:6]}/{fecha[:4]}"
    c.drawString(2*cm, h-4.2*cm, f"Fecha: {fecha}")
    c.drawString(2*cm, h-4.7*cm, f"Cliente: {f.get('receptor_nombre') or 'Consumidor Final'}")
    if f.get('nro_doc') and f['nro_doc'] != '0':
        c.drawString(2*cm, h-5.2*cm, f"Documento: {f['nro_doc']}")

    c.setLineWidth(0.5); c.setStrokeColorRGB(.8,.8,.8)
    c.line(2*cm, h-5.8*cm, w-2*cm, h-5.8*cm)

    y = h - 6.5*cm
    c.setFont("Helvetica-Bold", 8)
    c.drawString(2*cm, y, "Descripción"); c.drawString(12*cm, y, "Cant.")
    c.drawString(14*cm, y, "Precio"); c.drawString(17*cm, y, "Subtotal")
    y -= 0.6*cm; c.setFont("Helvetica", 8)
    for item in items:
        c.drawString(2*cm, y, str(item['descripcion'])[:60])
        c.drawString(12*cm, y, str(item['cantidad']))
        c.drawString(14*cm, y, f"$ {item['precio_unit']:,.2f}")
        c.drawString(17*cm, y, f"$ {item['subtotal']:,.2f}")
        y -= 0.55*cm

    c.line(2*cm, y, w-2*cm, y); y -= 0.7*cm
    c.setFont("Helvetica-Bold", 11); c.setFillColorRGB(.18,.42,.31)
    c.drawString(14*cm, y, "TOTAL:"); c.drawString(17*cm, y, f"$ {f['imp_total']:,.2f}")

    c.setFillColorRGB(.4,.4,.4); c.setFont("Helvetica", 7)
    c.drawString(2*cm, 2*cm,   f"CAE: {f.get('cae','—')}   Vto: {f.get('vto_cae','—')}")
    c.drawString(2*cm, 1.5*cm, "Comprobante emitido mediante ARCA (ex AFIP) — WS FE")

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
            "INSERT INTO clientes (nombre,tipo_doc,nro_doc,email,cond_iva) VALUES (?,?,?,?,?)",
            (cliente.nombre, cliente.tipo_doc, cliente.nro_doc, cliente.email, cliente.cond_iva)
        )
    return {"id": cur.lastrowid, **cliente.dict()}

@app.put("/clientes/{id}")
def actualizar_cliente(id: int, cliente: ClienteIn):
    with get_db() as db:
        db.execute(
            "UPDATE clientes SET nombre=?, tipo_doc=?, nro_doc=?, email=?, cond_iva=? WHERE id=?",
            (cliente.nombre, cliente.tipo_doc, cliente.nro_doc, cliente.email, cliente.cond_iva, id)
        )
    return {"id": id, **cliente.dict()}

@app.delete("/clientes/{id}")
def eliminar_cliente(id: int):
    with get_db() as db:
        db.execute("DELETE FROM clientes WHERE id=?", (id,))
    return {"ok": True}


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
    cfg = load_config()
    cfg["emisor"] = new.emisor.dict()
    new_smtp = new.smtp.dict()
    # Si el cliente manda password en blanco o el placeholder, conservar la anterior
    if not new_smtp["password"] or new_smtp["password"] == "********":
        new_smtp["password"] = cfg.get("smtp", {}).get("password", "")
    cfg["smtp"] = new_smtp
    save_config(cfg)
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