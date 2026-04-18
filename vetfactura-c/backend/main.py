# ─────────────────────────────────────────────────────────
#  VetFactura C — Backend FastAPI
#  Integración ARCA: WSAA + wsfe (Factura C = tipo 11)
#  Base de datos: SQLite (archivo local vetfactura.db)
#  Firma PKCS#7: cryptography (compatible ARCA)
# ─────────────────────────────────────────────────────────

from __future__ import annotations
import os, time, sqlite3, base64, json, smtplib, ssl, mimetypes, asyncio

# SSL context laxo para AFIP: sus servidores (especialmente producción,
# servicios1.afip.gov.ar) usan Diffie-Hellman < 2048 bits, que OpenSSL 3
# rechaza por default ("dh key too small"). Bajamos el SECLEVEL a 1 sólo
# para estas conexiones salientes.
_AFIP_SSL_CTX = ssl.create_default_context()
_AFIP_SSL_CTX.set_ciphers("DEFAULT:@SECLEVEL=1")
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
AMBIENTE    = os.getenv("ARCA_AMBIENTE", "homologacion")
PUNTO_VENTA = int(os.getenv("ARCA_PUNTO_VENTA", "1"))
DB_PATH     = os.getenv("DB_PATH", "./vetfactura.db")

# Rutas de certs por ambiente (configurable desde UI, ver DEFAULT_CONFIG)
CERT_PATH: Path = Path("./certs/homo_cert.pem")
KEY_PATH:  Path = Path("./certs/homo_key.pem")

WSAA_URLS = {
    "homologacion": "https://wsaahomo.afip.gov.ar/ws/services/LoginCms",
    "produccion":   "https://wsaa.afip.gov.ar/ws/services/LoginCms",
}
WSFE_URLS = {
    "homologacion": "https://wswhomo.afip.gov.ar/wsfev1/service.asmx",
    "produccion":   "https://servicios1.afip.gov.ar/wsfev1/service.asmx",
}
WSAA_URL = WSAA_URLS[AMBIENTE]
WSFE_URL = WSFE_URLS[AMBIENTE]

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
    "certs": {
        "homologacion": {"cert_path": "./certs/homo_cert.pem", "key_path": "./certs/homo_key.pem"},
        "produccion":   {"cert_path": "./certs/cert.pem",      "key_path": "./certs/key.pem"},
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


def _aplicar_certs_del_ambiente(cfg: dict | None = None) -> None:
    """Recalcula CERT_PATH/KEY_PATH según AMBIENTE y la config persistida."""
    global CERT_PATH, KEY_PATH
    cfg = cfg or load_config()
    certs = (cfg.get("certs") or {}).get(AMBIENTE) or {}
    defaults = DEFAULT_CONFIG["certs"][AMBIENTE]
    CERT_PATH = Path(certs.get("cert_path") or defaults["cert_path"])
    KEY_PATH  = Path(certs.get("key_path")  or defaults["key_path"])


def _migrar_certs_desde_env() -> None:
    """Si config.json no tiene 'certs' y el .env define rutas, migrarlas al ambiente actual."""
    legacy_cert = os.getenv("ARCA_CERT_PATH")
    legacy_key  = os.getenv("ARCA_KEY_PATH")
    if not (legacy_cert or legacy_key):
        return
    cfg = load_config()
    if cfg.get("certs"):
        return  # ya migrado
    cfg["certs"] = json.loads(json.dumps(DEFAULT_CONFIG["certs"]))
    if legacy_cert:
        cfg["certs"][AMBIENTE]["cert_path"] = legacy_cert
    if legacy_key:
        cfg["certs"][AMBIENTE]["key_path"]  = legacy_key
    save_config(cfg)


_migrar_certs_desde_env()
_aplicar_certs_del_ambiente()

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
        if "telefono" not in cols:
            db.execute("ALTER TABLE clientes ADD COLUMN telefono TEXT DEFAULT ''")
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

    # Reintentos con backoff: ARCA homologación a veces responde
    # "Zero length BigInteger" de forma transitoria (incidente del servidor).
    resp = None
    last_error = None
    for intento in range(3):
        tra = _crear_tra(servicio)
        cms = _firmar_tra(tra)
        soap = f"""<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:wsaa="http://wsaa.view.sua.dvadac.desein.afip.gov.ar">
  <soapenv:Header/>
  <soapenv:Body>
    <wsaa:loginCms><wsaa:in0>{cms}</wsaa:in0></wsaa:loginCms>
  </soapenv:Body>
</soapenv:Envelope>"""
        async with httpx.AsyncClient(timeout=30, verify=_AFIP_SSL_CTX) as client:
            resp = await client.post(
                WSAA_URL, content=soap.encode(),
                headers={"Content-Type": "text/xml;charset=UTF-8", "SOAPAction": ""}
            )
        if resp.is_success:
            break
        last_error = resp.text[:1000]
        if "Zero length BigInteger" not in last_error:
            break
        await asyncio.sleep(2 ** intento)  # 1s, 2s, 4s

    if resp is None or not resp.is_success:
        msg = last_error or ""
        if "Zero length BigInteger" in msg:
            detalle = (
                "ARCA homologación está respondiendo con un error interno "
                "('Zero length BigInteger'). No es un problema del certificado "
                "ni del código: el servidor de AFIP/ARCA falla al procesar el "
                "CMS. Reintentá en unos minutos; si persiste, verificá el estado "
                "de homologación en los foros de DevAFIP."
            )
        else:
            detalle = f"WSAA {resp.status_code if resp else 'sin respuesta'}: {msg}"
        raise HTTPException(status_code=502, detail=detalle)

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
    async with httpx.AsyncClient(timeout=30, verify=_AFIP_SSL_CTX) as client:
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
    telefono:  Optional[str] = ""

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

class CertPathsIn(BaseModel):
    cert_path: Optional[str] = None
    key_path:  Optional[str] = None

class CertsByEnvIn(BaseModel):
    homologacion: Optional[CertPathsIn] = None
    produccion:   Optional[CertPathsIn] = None

class ConfigIn(BaseModel):
    emisor:         EmisorIn
    smtp:           SmtpIn
    nombre_sistema: Optional[str] = "VetFactura"
    punto_venta:    Optional[int] = None
    ambiente:       Optional[str] = None  # "homologacion" | "produccion"
    certs:          Optional[CertsByEnvIn] = None

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

TIPO_DOC_RECEPTOR = {
    80: "CUIT",
    86: "CUIL",
    96: "DNI",
    99: "Consumidor Final",
}

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
    dom_c  = emisor.get("domicilio_comercial") or emisor.get("domicilio") or ""
    cond_e = emisor.get("condicion_iva") or "Responsable Monotributo"
    cuit_e = emisor.get("cuit") or CUIT
    iibb   = emisor.get("ingresos_brutos") or ""
    ini_act = _fmt_date(emisor.get("inicio_actividades") or "")

    PRIMARY = (0.055, 0.463, 0.431)
    TEXT    = (0.118, 0.161, 0.224)
    MUTED   = (0.392, 0.455, 0.545)
    SOFT_BG = (0.949, 0.965, 0.980)
    BORDER  = (0.851, 0.878, 0.914)
    WHITE   = (1, 1, 1)

    def sf(rgb): c.setFillColorRGB(*rgb)
    def ss(rgb): c.setStrokeColorRGB(*rgb)

    margin = 1.4*cm
    x0, x1 = margin, w - margin
    y_top  = h - 1.2*cm

    # Copia: pill arriba a la derecha
    pill_w, pill_h = 3.4*cm, 0.6*cm
    pill_x = x1 - pill_w
    pill_y = y_top - pill_h
    sf(PRIMARY); ss(PRIMARY)
    c.roundRect(pill_x, pill_y, pill_w, pill_h, 0.3*cm, stroke=0, fill=1)
    sf(WHITE); c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(pill_x + pill_w/2, pill_y + 0.2*cm, copia)

    # Encabezado: C central + columnas laterales sin marco
    head_top = pill_y - 0.35*cm
    c_box_w, c_box_h = 2.2*cm, 2.4*cm
    c_box_x = (x0 + x1)/2 - c_box_w/2
    c_box_y = head_top - c_box_h
    sf(WHITE); ss(TEXT); c.setLineWidth(1.2)
    c.rect(c_box_x, c_box_y, c_box_w, c_box_h, stroke=1, fill=1)
    sf(TEXT); c.setFont("Helvetica-Bold", 52)
    c.drawCentredString(c_box_x + c_box_w/2, c_box_y + 0.6*cm, "C")
    sf(MUTED); c.setFont("Helvetica-Bold", 7)
    c.drawCentredString(c_box_x + c_box_w/2, c_box_y + 0.2*cm, "COD. 011")

    # Izquierda: logo + razón social + campos emisor
    left_x = x0
    logo_offset = 0
    if LOGO_PATH.exists():
        try:
            img = ImageReader(str(LOGO_PATH))
            iw, ih = img.getSize()
            max_w, max_h = 1.6*cm, 1.6*cm
            scale = min(max_w/iw, max_h/ih)
            c.drawImage(img, left_x, head_top - 1.7*cm,
                        width=iw*scale, height=ih*scale,
                        preserveAspectRatio=True, mask='auto')
            logo_offset = iw*scale + 0.25*cm
        except Exception:
            logo_offset = 0

    sf(TEXT); c.setFont("Helvetica-Bold", 15)
    c.drawString(left_x + logo_offset, head_top - 0.45*cm, razon.upper()[:40])

    ly = head_top - 0.95*cm
    for lbl, val in (("RAZÓN SOCIAL", razon),
                     ("DOMICILIO", dom_c[:60] or "—"),
                     ("CONDICIÓN FRENTE AL IVA", cond_e)):
        sf(MUTED); c.setFont("Helvetica", 6.2)
        c.drawString(left_x + logo_offset, ly, lbl)
        sf(TEXT); c.setFont("Helvetica", 8.5)
        c.drawString(left_x + logo_offset, ly - 0.28*cm, str(val))
        ly -= 0.48*cm

    # Derecha: FACTURA + nros + fecha
    right_x = c_box_x + c_box_w + 0.5*cm
    sf(PRIMARY); c.setFont("Helvetica-Bold", 20)
    c.drawString(right_x, head_top - 0.55*cm, "FACTURA")

    sf(MUTED); c.setFont("Helvetica", 6.2)
    c.drawString(right_x, head_top - 1.2*cm, "PUNTO DE VENTA")
    c.drawString(right_x + 3.6*cm, head_top - 1.2*cm, "COMP. N°")
    sf(TEXT); c.setFont("Helvetica-Bold", 13)
    c.drawString(right_x, head_top - 1.6*cm, str(f["punto_venta"]).zfill(5))
    c.drawString(right_x + 3.6*cm, head_top - 1.6*cm, str(f["nro_cbte"]).zfill(8))

    sf(MUTED); c.setFont("Helvetica", 6.2)
    c.drawString(right_x, head_top - 2.1*cm, "FECHA DE EMISIÓN")
    sf(TEXT); c.setFont("Helvetica", 9)
    c.drawString(right_x, head_top - 2.45*cm, _fmt_date(f.get("fecha_cbte")))

    # Barra de datos fiscales del emisor
    bar_y = head_top - c_box_h - 0.5*cm
    bar_h = 0.95*cm
    sf(SOFT_BG); ss(BORDER); c.setLineWidth(0.4)
    c.roundRect(x0, bar_y - bar_h, x1 - x0, bar_h, 0.2*cm, stroke=1, fill=1)
    cells = (
        ("CUIT EMISOR", str(cuit_e)),
        ("INGRESOS BRUTOS", iibb or "—"),
        ("INICIO DE ACTIVIDADES", ini_act or "—"),
    )
    cell_w = (x1 - x0) / 3
    for i, (lbl, val) in enumerate(cells):
        cx = x0 + i*cell_w + 0.3*cm
        sf(MUTED); c.setFont("Helvetica", 6.2)
        c.drawString(cx, bar_y - 0.3*cm, lbl)
        sf(TEXT); c.setFont("Helvetica-Bold", 10)
        c.drawString(cx, bar_y - 0.7*cm, str(val))
        if i > 0:
            ss(BORDER); c.setLineWidth(0.4)
            c.line(x0 + i*cell_w, bar_y - 0.2*cm,
                   x0 + i*cell_w, bar_y - bar_h + 0.2*cm)

    # Período
    per_y = bar_y - bar_h - 0.55*cm
    desde = _fmt_date(f.get("fch_serv_desde") or f.get("fecha_cbte"))
    hasta = _fmt_date(f.get("fch_serv_hasta") or f.get("fecha_cbte"))
    vto_p = _fmt_date(f.get("fch_vto_pago")   or f.get("fecha_cbte"))
    col_w = (x1 - x0) / 3
    for i, (lbl, val) in enumerate((("PERÍODO DESDE", desde),
                                     ("HASTA", hasta),
                                     ("VTO. PARA EL PAGO", vto_p))):
        cx = x0 + i*col_w
        sf(MUTED); c.setFont("Helvetica", 6.2)
        c.drawString(cx, per_y, lbl)
        sf(TEXT); c.setFont("Helvetica", 9)
        c.drawString(cx, per_y - 0.35*cm, val)

    # Línea acento
    line_y = per_y - 0.7*cm
    ss(PRIMARY); c.setLineWidth(1.0)
    c.line(x0, line_y, x1, line_y)

    # Receptor
    sf(PRIMARY); c.setFont("Helvetica-Bold", 7.5)
    c.drawString(x0, line_y - 0.35*cm, "CLIENTE")

    tipo_doc_r = f.get("tipo_doc") or 99
    doc_label = TIPO_DOC_RECEPTOR.get(tipo_doc_r, "Doc")
    nro_doc_r = f.get("nro_doc") or ""
    nombre_r = f.get("receptor_nombre") or "Consumidor Final"
    dom_r    = f.get("receptor_dom") or ""
    cond_r   = COND_IVA_RECEPTOR.get(f.get("cond_iva_receptor") or 0, "")
    cond_v   = f.get("cond_venta") or "Contado"

    rec_y = line_y - 0.9*cm
    rec_fields = (
        (doc_label.upper(), str(nro_doc_r) or "—"),
        ("APELLIDO Y NOMBRE / RAZÓN SOCIAL", nombre_r[:55]),
        ("CONDICIÓN FRENTE AL IVA", cond_r or "—"),
        ("DOMICILIO", (dom_r[:50] or "—")),
        ("CONDICIÓN DE VENTA", cond_v),
    )
    col_w2 = (x1 - x0) / 2
    for i, (lbl, val) in enumerate(rec_fields):
        row = i // 2
        col = i % 2
        fx = x0 + col * col_w2
        fy = rec_y - row * 0.85*cm
        sf(MUTED); c.setFont("Helvetica", 6.2)
        c.drawString(fx, fy, lbl)
        sf(TEXT); c.setFont("Helvetica", 9)
        c.drawString(fx, fy - 0.33*cm, str(val))

    rec_rows = (len(rec_fields) + 1) // 2
    rec_end_y = rec_y - rec_rows * 0.85*cm + 0.15*cm

    # Tabla ítems
    tbl_top = rec_end_y - 0.25*cm
    headers = (
        ("Código",              1.4*cm, "left"),
        ("Producto / Servicio", 6.2*cm, "left"),
        ("Cantidad",            1.6*cm, "right"),
        ("U. Medida",           1.6*cm, "left"),
        ("Precio Unit.",        2.0*cm, "right"),
        ("% Bonif.",            1.3*cm, "right"),
        ("Imp. Bonif.",         1.6*cm, "right"),
        ("Subtotal",            2.2*cm, "right"),
    )
    total_w = sum(cw for _, cw, _ in headers)
    scale_h = (x1 - x0) / total_w
    headers = [(t, cw*scale_h, a) for t, cw, a in headers]

    row_h = 0.7*cm
    sf(PRIMARY); ss(PRIMARY)
    c.rect(x0, tbl_top - row_h, x1 - x0, row_h, stroke=0, fill=1)
    sf(WHITE); c.setFont("Helvetica-Bold", 8)
    cx = x0
    col_x = [cx]
    for title, cw, align in headers:
        if align == "right":
            c.drawRightString(cx + cw - 0.15*cm, tbl_top - row_h + 0.25*cm, title)
        else:
            c.drawString(cx + 0.15*cm, tbl_top - row_h + 0.25*cm, title)
        cx += cw
        col_x.append(cx)

    y = tbl_top - row_h
    for idx, it in enumerate(items):
        y -= row_h
        if idx % 2 == 0:
            sf(SOFT_BG); ss(SOFT_BG)
            c.rect(x0, y, x1 - x0, row_h, stroke=0, fill=1)
        desc = str(it["descripcion"])
        cant = it["cantidad"]
        pu   = it["precio_unit"]
        sub  = it["subtotal"]
        cant_s = f"{cant:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        vals = (
            ("",                 "left"),
            (desc[:55],          "left"),
            (cant_s,             "right"),
            ("unidades",         "left"),
            (_fmt_money(pu),     "right"),
            ("0,00",             "right"),
            ("0,00",             "right"),
            (_fmt_money(sub),    "right"),
        )
        sf(TEXT); c.setFont("Helvetica", 8.5)
        for i, (text, align) in enumerate(vals):
            if align == "right":
                c.drawRightString(col_x[i+1] - 0.15*cm, y + 0.25*cm, text)
            else:
                c.drawString(col_x[i] + 0.15*cm, y + 0.25*cm, text)

    # Totales
    tot_w = 7.4*cm
    tot_x = x1 - tot_w
    tot_y_top = 5.4*cm
    sf(TEXT); c.setFont("Helvetica", 9)
    c.drawRightString(tot_x + tot_w - 3.2*cm, tot_y_top, "Subtotal")
    c.setFont("Helvetica", 10)
    c.drawRightString(tot_x + tot_w - 0.25*cm, tot_y_top, f"$ {_fmt_money(f['imp_total'])}")

    c.setFont("Helvetica", 9)
    c.drawRightString(tot_x + tot_w - 3.2*cm, tot_y_top - 0.5*cm, "Importe Otros Tributos")
    c.setFont("Helvetica", 10)
    c.drawRightString(tot_x + tot_w - 0.25*cm, tot_y_top - 0.5*cm, "$ 0,00")

    ss(BORDER); c.setLineWidth(0.4)
    c.line(tot_x, tot_y_top - 0.9*cm, tot_x + tot_w, tot_y_top - 0.9*cm)

    tb_y = tot_y_top - 1.8*cm
    sf(PRIMARY); ss(PRIMARY)
    c.roundRect(tot_x, tb_y, tot_w, 0.9*cm, 0.15*cm, stroke=0, fill=1)
    sf(WHITE); c.setFont("Helvetica-Bold", 11)
    c.drawString(tot_x + 0.3*cm, tb_y + 0.3*cm, "IMPORTE TOTAL")
    c.setFont("Helvetica-Bold", 13)
    c.drawRightString(tot_x + tot_w - 0.3*cm, tb_y + 0.28*cm, f"$ {_fmt_money(f['imp_total'])}")

    # Pie: QR + ARCA + CAE
    qr_img = _qr_arca_image(f, str(cuit_e))
    qr_size = 2.6*cm
    qr_x, qr_y = x0, 0.9*cm
    if qr_img is not None:
        bio = _io.BytesIO()
        qr_img.save(bio, format="PNG")
        bio.seek(0)
        c.drawImage(ImageReader(bio), qr_x, qr_y, width=qr_size, height=qr_size, mask='auto')
    else:
        ss(BORDER); sf(WHITE)
        c.rect(qr_x, qr_y, qr_size, qr_size, stroke=1, fill=0)
        sf(MUTED); c.setFont("Helvetica", 6)
        c.drawCentredString(qr_x + qr_size/2, qr_y + qr_size/2, "QR")

    arca_x = qr_x + qr_size + 0.4*cm
    sf(TEXT); c.setFont("Helvetica-Bold", 13)
    c.drawString(arca_x, qr_y + qr_size - 0.5*cm, "ARCA")
    sf(MUTED); c.setFont("Helvetica", 6)
    c.drawString(arca_x, qr_y + qr_size - 0.85*cm, "Agencia de Recaudación")
    c.drawString(arca_x, qr_y + qr_size - 1.05*cm, "y Control Aduanero")
    sf(TEXT); c.setFont("Helvetica-Oblique", 7.5)
    c.drawString(arca_x, qr_y + 0.4*cm, "Comprobante Autorizado")
    sf(MUTED); c.setFont("Helvetica-Oblique", 5.5)
    c.drawString(arca_x, qr_y + 0.1*cm,
                 "Esta Agencia no se responsabiliza por los datos ingresados en el detalle de la operación")

    cae_x = x1 - 6.2*cm
    sf(MUTED); c.setFont("Helvetica", 6.2)
    c.drawString(cae_x, qr_y + qr_size - 0.5*cm, "CAE N°")
    sf(TEXT); c.setFont("Helvetica-Bold", 12)
    c.drawString(cae_x, qr_y + qr_size - 0.95*cm, str(f.get('cae','') or "—"))
    sf(MUTED); c.setFont("Helvetica", 6.2)
    c.drawString(cae_x, qr_y + qr_size - 1.5*cm, "FECHA DE VTO. DE CAE")
    sf(TEXT); c.setFont("Helvetica", 9)
    c.drawString(cae_x, qr_y + qr_size - 1.85*cm, _fmt_date(f.get("vto_cae")))

    sf(MUTED); c.setFont("Helvetica", 7)
    c.drawRightString(x1, qr_y + 0.15*cm, "Pág. 1/1")


def _build_factura_pdf(id: int, copias: tuple = ("ORIGINAL", "DUPLICADO", "TRIPLICADO")) -> bytes:
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
    for copia in copias:
        _draw_page(c, w, h, copia, f, items, cfg)
        c.showPage()
    c.save()
    return buf.getvalue()


@app.get("/facturas/{id}/pdf")
async def pdf_factura(id: int, copias: str = "todas"):
    modo = (copias or "todas").lower()
    if modo in ("original", "solo", "1"):
        pages = ("ORIGINAL",)
        suffix = "_original"
    else:
        pages = ("ORIGINAL", "DUPLICADO", "TRIPLICADO")
        suffix = ""
    try:
        pdf_bytes = _build_factura_pdf(id, pages)
    except ImportError:
        raise HTTPException(500, "Instalar reportlab: pip install reportlab")
    import io
    return StreamingResponse(io.BytesIO(pdf_bytes), media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=factura_c_{id}{suffix}.pdf"})

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
            "INSERT INTO clientes (nombre,tipo_doc,nro_doc,email,cond_iva,domicilio,telefono) VALUES (?,?,?,?,?,?,?)",
            (cliente.nombre, cliente.tipo_doc, cliente.nro_doc, cliente.email,
             cliente.cond_iva, cliente.domicilio, cliente.telefono)
        )
    return {"id": cur.lastrowid, **cliente.dict()}

@app.put("/clientes/{id}")
def actualizar_cliente(id: int, cliente: ClienteIn):
    with get_db() as db:
        db.execute(
            "UPDATE clientes SET nombre=?, tipo_doc=?, nro_doc=?, email=?, cond_iva=?, domicilio=?, telefono=? WHERE id=?",
            (cliente.nombre, cliente.tipo_doc, cliente.nro_doc, cliente.email,
             cliente.cond_iva, cliente.domicilio, cliente.telefono, id)
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
    async with httpx.AsyncClient(timeout=10, verify=_AFIP_SSL_CTX) as client:
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
    cfg_safe["has_logo"]    = LOGO_PATH.exists()
    cfg_safe["cuit"]        = CUIT
    cfg_safe["ambiente"]    = AMBIENTE
    cfg_safe["punto_venta"] = PUNTO_VENTA
    # Indicador de existencia por cada ruta configurada
    cert_status = {}
    for amb, paths in cfg_safe.get("certs", {}).items():
        cert_status[amb] = {
            "cert_exists": Path(paths.get("cert_path", "")).exists(),
            "key_exists":  Path(paths.get("key_path",  "")).exists(),
        }
    cfg_safe["certs_status"] = cert_status
    return cfg_safe

def _actualizar_env(pares: dict) -> None:
    """Reescribe el .env preservando líneas y actualizando/insertando las claves dadas."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    lines = env_path.read_text().splitlines()
    pendientes = dict(pares)
    for i, line in enumerate(lines):
        for clave in list(pendientes):
            if line.startswith(f"{clave}="):
                lines[i] = f"{clave}={pendientes.pop(clave)}"
                break
    for clave, valor in pendientes.items():
        lines.append(f"{clave}={valor}")
    env_path.write_text("\n".join(lines) + "\n")


@app.post("/config")
def set_config(new: ConfigIn):
    global CUIT, PUNTO_VENTA, AMBIENTE, WSAA_URL, WSFE_URL
    cfg = load_config()
    cfg["emisor"] = new.emisor.dict()
    new_smtp = new.smtp.dict()
    # Si el cliente manda password en blanco o el placeholder, conservar la anterior
    if not new_smtp["password"] or new_smtp["password"] == "********":
        new_smtp["password"] = cfg.get("smtp", {}).get("password", "")
    cfg["smtp"] = new_smtp
    cfg["nombre_sistema"] = new.nombre_sistema or "VetFactura"

    # Rutas de certs por ambiente (si vinieron, mergear por campo para no perder claves)
    if new.certs:
        cert_cfg = cfg.setdefault("certs", json.loads(json.dumps(DEFAULT_CONFIG["certs"])))
        for amb in ("homologacion", "produccion"):
            entry = getattr(new.certs, amb, None)
            if not entry:
                continue
            cert_cfg.setdefault(amb, {})
            if entry.cert_path is not None and entry.cert_path.strip():
                cert_cfg[amb]["cert_path"] = entry.cert_path.strip()
            if entry.key_path is not None and entry.key_path.strip():
                cert_cfg[amb]["key_path"]  = entry.key_path.strip()

    save_config(cfg)

    env_updates = {}
    # Actualizar CUIT en memoria si cambió
    new_cuit = (new.emisor.cuit or "").strip()
    if new_cuit and new_cuit != CUIT:
        CUIT = new_cuit
        env_updates["ARCA_CUIT"] = new_cuit
    # Actualizar PUNTO_VENTA en memoria si cambió
    if new.punto_venta is not None and new.punto_venta > 0 and new.punto_venta != PUNTO_VENTA:
        PUNTO_VENTA = int(new.punto_venta)
        env_updates["ARCA_PUNTO_VENTA"] = str(PUNTO_VENTA)
    # Actualizar AMBIENTE (y URLs) en memoria si cambió
    new_amb = (new.ambiente or "").strip().lower()
    if new_amb in WSAA_URLS and new_amb != AMBIENTE:
        AMBIENTE  = new_amb
        WSAA_URL  = WSAA_URLS[new_amb]
        WSFE_URL  = WSFE_URLS[new_amb]
        env_updates["ARCA_AMBIENTE"] = new_amb
        # Los tokens WSAA son específicos del ambiente: invalidarlos
        with get_db() as db:
            db.execute("DELETE FROM tokens_wsaa")

    # Recalcular siempre las rutas activas (ambiente o certs pudieron cambiar)
    _aplicar_certs_del_ambiente(cfg)

    if env_updates:
        _actualizar_env(env_updates)
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
        pdf_bytes = _build_factura_pdf(id, ("ORIGINAL",))
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