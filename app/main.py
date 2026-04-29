from fastapi import FastAPI, HTTPException, UploadFile, File, Query
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.requests import Request
from pydantic import BaseModel
from typing import Optional
import sqlite3
import csv
import io
import json
import httpx
from datetime import datetime, date
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, HRFlowable
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
import os

app = FastAPI(title="GasControl", description="Control de consumo de gas doméstico")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "gascontrol.db")

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# ---------------------------------------------------------------------------
# DB INIT
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS lecturas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            valor_contador REAL NOT NULL,
            nota TEXT,
            precio_kwh REAL,
            factor_conversion REAL,
            cuota_fija REAL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS config (
            clave TEXT PRIMARY KEY,
            valor TEXT NOT NULL
        )
    """)
    # Default config
    defaults = [
        ("factor_conversion", "10.23"),
        ("cuota_fija", "10.50"),
        ("tarifa_tur", "TUR2"),
        ("precio_kwh_manual", "0.0650"),
    ]
    for k, v in defaults:
        c.execute("INSERT OR IGNORE INTO config (clave, valor) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()

init_db()

# ---------------------------------------------------------------------------
# MODELS
# ---------------------------------------------------------------------------
class LecturaCreate(BaseModel):
    fecha: str
    valor_contador: float
    nota: Optional[str] = None
    precio_kwh: Optional[float] = None

class ConfigUpdate(BaseModel):
    factor_conversion: Optional[float] = None
    cuota_fija: Optional[float] = None
    tarifa_tur: Optional[str] = None
    precio_kwh_manual: Optional[float] = None

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def get_config(conn) -> dict:
    rows = conn.execute("SELECT clave, valor FROM config").fetchall()
    return {r["clave"]: r["valor"] for r in rows}

def calcular_consumo_kwh(m3: float, factor: float) -> float:
    return round(m3 * factor, 4)

def calcular_coste(kwh: float, precio: float, cuota: float) -> float:
    return round(kwh * precio + cuota, 4)

def parse_fecha(f: str) -> str:
    """Normalize fecha to ISO format."""
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(f.strip(), fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise ValueError(f"Formato de fecha no reconocido: {f}")

# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/config")
async def api_get_config():
    conn = get_db()
    cfg = get_config(conn)
    conn.close()
    return cfg

@app.post("/api/config")
async def api_update_config(data: ConfigUpdate):
    conn = get_db()
    updates = data.dict(exclude_none=True)
    for k, v in updates.items():
        conn.execute("INSERT OR REPLACE INTO config (clave, valor) VALUES (?, ?)", (k, str(v)))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.get("/api/precio-tur")
async def api_precio_tur():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://precios-tur.petru.tech/api/precio-actual")
            if r.status_code == 200:
                data = r.json()
                return {"ok": True, "data": data}
            # Try alternative endpoint
            r2 = await client.get("https://precios-tur.petru.tech/api/precios")
            if r2.status_code == 200:
                return {"ok": True, "data": r2.json()}
    except Exception as e:
        pass
    # Fallback: try root
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://precios-tur.petru.tech/")
            return {"ok": True, "raw": r.text[:500]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/lecturas")
async def api_crear_lectura(data: LecturaCreate):
    conn = get_db()
    cfg = get_config(conn)
    factor = float(cfg["factor_conversion"])
    cuota = float(cfg["cuota_fija"])
    precio = data.precio_kwh if data.precio_kwh else float(cfg["precio_kwh_manual"])
    fecha_iso = parse_fecha(data.fecha)
    conn.execute(
        """INSERT INTO lecturas (fecha, valor_contador, nota, precio_kwh, factor_conversion, cuota_fija)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (fecha_iso, data.valor_contador, data.nota, precio, factor, cuota)
    )
    conn.commit()
    conn.close()
    return {"ok": True}

@app.get("/api/lecturas")
async def api_get_lecturas(
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = 0
):
    conn = get_db()
    cfg = get_config(conn)
    factor = float(cfg["factor_conversion"])
    cuota = float(cfg["cuota_fija"])
    precio_default = float(cfg["precio_kwh_manual"])

    where = []
    params = []
    if desde:
        where.append("fecha >= ?")
        params.append(parse_fecha(desde) if "/" in desde else desde + " 00:00:00")
    if hasta:
        where.append("fecha <= ?")
        params.append(parse_fecha(hasta) if "/" in hasta else hasta + " 23:59:59")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM lecturas {where_sql}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM lecturas {where_sql} ORDER BY fecha DESC LIMIT ? OFFSET ?",
        params + [limit, offset]
    ).fetchall()
    conn.close()

    lecturas = []
    all_rows_asc = list(reversed(rows))  # para calcular consumo vs anterior
    # Obtener todas ordenadas ASC para cálculo de consumo
    conn2 = get_db()
    all_rows_full = conn2.execute(
        f"SELECT * FROM lecturas {where_sql} ORDER BY fecha ASC",
        params
    ).fetchall()
    conn2.close()

    # Índice por id para rápido acceso
    idx = {r["id"]: i for i, r in enumerate(all_rows_full)}

    result = []
    for r in rows:
        pos = idx.get(r["id"], -1)
        consumo_m3 = None
        consumo_kwh = None
        coste = None
        if pos > 0:
            prev = all_rows_full[pos - 1]
            consumo_m3 = round(r["valor_contador"] - prev["valor_contador"], 3)
            f = r["factor_conversion"] or factor
            p = r["precio_kwh"] or precio_default
            c = r["cuota_fija"] or cuota
            consumo_kwh = calcular_consumo_kwh(consumo_m3, f)
            coste = calcular_coste(consumo_kwh, p, c)
        result.append({
            "id": r["id"],
            "fecha": r["fecha"],
            "valor_contador": r["valor_contador"],
            "nota": r["nota"],
            "precio_kwh": r["precio_kwh"],
            "consumo_m3": consumo_m3,
            "consumo_kwh": consumo_kwh,
            "coste": coste,
        })

    return {"total": total, "lecturas": result}

@app.get("/api/resumen")
async def api_resumen():
    conn = get_db()
    cfg = get_config(conn)
    factor = float(cfg["factor_conversion"])
    cuota = float(cfg["cuota_fija"])
    precio_default = float(cfg["precio_kwh_manual"])

    rows = conn.execute("SELECT * FROM lecturas ORDER BY fecha ASC").fetchall()
    conn.close()

    if not rows:
        return {
            "ultima_lectura": None,
            "consumo_total_m3": 0,
            "consumo_total_kwh": 0,
            "coste_total": 0,
            "precio_actual": precio_default,
            "num_lecturas": 0,
        }

    ultima = rows[-1]
    consumo_total_m3 = round(rows[-1]["valor_contador"] - rows[0]["valor_contador"], 3)
    consumo_total_kwh = calcular_consumo_kwh(consumo_total_m3, factor)
    coste_total = 0
    for i in range(1, len(rows)):
        m3 = rows[i]["valor_contador"] - rows[i-1]["valor_contador"]
        f = rows[i]["factor_conversion"] or factor
        p = rows[i]["precio_kwh"] or precio_default
        c = rows[i]["cuota_fija"] or cuota
        kwh = calcular_consumo_kwh(m3, f)
        coste_total += calcular_coste(kwh, p, c)

    return {
        "ultima_lectura": {"fecha": ultima["fecha"], "valor": ultima["valor_contador"]},
        "consumo_total_m3": consumo_total_m3,
        "consumo_total_kwh": consumo_total_kwh,
        "coste_total": round(coste_total, 2),
        "precio_actual": precio_default,
        "num_lecturas": len(rows),
    }

@app.get("/api/grafica")
async def api_grafica(desde: Optional[str] = None, hasta: Optional[str] = None):
    conn = get_db()
    cfg = get_config(conn)
    factor = float(cfg["factor_conversion"])
    cuota = float(cfg["cuota_fija"])
    precio_default = float(cfg["precio_kwh_manual"])

    if not desde and not hasta:
        from dateutil.relativedelta import relativedelta
        dos_anios = (datetime.now() - relativedelta(years=2)).strftime("%Y-%m-%d 00:00:00")
        where = "WHERE fecha >= ?"
        params = [dos_anios]
    else:
        where_parts = []
        params = []
        if desde:
            where_parts.append("fecha >= ?")
            params.append(desde + " 00:00:00" if len(desde) == 10 else desde)
        if hasta:
            where_parts.append("fecha <= ?")
            params.append(hasta + " 23:59:59" if len(hasta) == 10 else hasta)
        where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    rows = conn.execute(f"SELECT * FROM lecturas {where} ORDER BY fecha ASC", params).fetchall()
    conn.close()

    labels = []
    consumo_m3 = []
    consumo_kwh = []
    costes = []

    for i in range(1, len(rows)):
        r = rows[i]
        prev = rows[i-1]
        m3 = round(r["valor_contador"] - prev["valor_contador"], 3)
        f = r["factor_conversion"] or factor
        p = r["precio_kwh"] or precio_default
        c = r["cuota_fija"] or cuota
        kwh = calcular_consumo_kwh(m3, f)
        coste = calcular_coste(kwh, p, c)
        labels.append(r["fecha"][:10])
        consumo_m3.append(m3)
        consumo_kwh.append(kwh)
        costes.append(coste)

    return {"labels": labels, "consumo_m3": consumo_m3, "consumo_kwh": consumo_kwh, "costes": costes}

@app.delete("/api/lecturas/{lectura_id}")
async def api_delete_lectura(lectura_id: int):
    conn = get_db()
    conn.execute("DELETE FROM lecturas WHERE id = ?", (lectura_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

# ---------------------------------------------------------------------------
# EXPORT / IMPORT
# ---------------------------------------------------------------------------
@app.get("/api/export/csv")
async def export_csv():
    conn = get_db()
    rows = conn.execute("SELECT fecha, valor_contador, nota, precio_kwh, factor_conversion, cuota_fija FROM lecturas ORDER BY fecha ASC").fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["fecha", "valor_contador", "nota", "precio_kwh", "factor_conversion", "cuota_fija"])
    for r in rows:
        writer.writerow([r["fecha"], r["valor_contador"], r["nota"] or "", r["precio_kwh"] or "", r["factor_conversion"] or "", r["cuota_fija"] or ""])
    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=gascontrol_lecturas.csv"}
    )

@app.get("/api/export/template")
async def export_template():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["fecha", "valor_contador", "nota"])
    writer.writerow(["20/12/2023 12:00", "269.00", "Lectura histórica"])
    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=plantilla_lecturas.csv"}
    )

@app.post("/api/import/csv")
async def import_csv(file: UploadFile = File(...)):
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except:
        text = content.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    conn = get_db()
    cfg = get_config(conn)
    factor = float(cfg["factor_conversion"])
    cuota = float(cfg["cuota_fija"])
    precio = float(cfg["precio_kwh_manual"])
    imported = 0
    errors = []
    for i, row in enumerate(reader):
        try:
            fecha = parse_fecha(row.get("fecha", ""))
            valor = float(row.get("valor_contador", "").replace(",", "."))
            nota = row.get("nota", "")
            p = float(row["precio_kwh"]) if row.get("precio_kwh") else precio
            f = float(row["factor_conversion"]) if row.get("factor_conversion") else factor
            c = float(row["cuota_fija"]) if row.get("cuota_fija") else cuota
            conn.execute(
                "INSERT OR IGNORE INTO lecturas (fecha, valor_contador, nota, precio_kwh, factor_conversion, cuota_fija) VALUES (?,?,?,?,?,?)",
                (fecha, valor, nota, p, f, c)
            )
            imported += 1
        except Exception as e:
            errors.append(f"Fila {i+2}: {str(e)}")
    conn.commit()
    conn.close()
    return {"ok": True, "imported": imported, "errors": errors}

@app.get("/api/export/pdf")
async def export_pdf(desde: Optional[str] = None, hasta: Optional[str] = None):
    conn = get_db()
    cfg = get_config(conn)
    factor = float(cfg["factor_conversion"])
    cuota = float(cfg["cuota_fija"])
    precio_default = float(cfg["precio_kwh_manual"])

    where_parts = []
    params = []
    if desde:
        where_parts.append("fecha >= ?")
        params.append(desde + " 00:00:00" if len(desde) == 10 else desde)
    if hasta:
        where_parts.append("fecha <= ?")
        params.append(hasta + " 23:59:59" if len(hasta) == 10 else hasta)
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    rows = conn.execute(f"SELECT * FROM lecturas {where} ORDER BY fecha ASC", params).fetchall()
    resumen = await api_resumen()
    conn.close()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            rightMargin=2*cm, leftMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    story = []

    # Title
    title_style = ParagraphStyle('title', parent=styles['Title'], fontSize=20,
                                  textColor=colors.HexColor('#1a73e8'), spaceAfter=6)
    story.append(Paragraph("🔥 GasControl — Informe de Consumo", title_style))
    story.append(Paragraph(f"Generado: {datetime.now().strftime('%d/%m/%Y %H:%M')}", styles['Normal']))
    story.append(Spacer(1, 0.5*cm))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#1a73e8')))
    story.append(Spacer(1, 0.4*cm))

    # Resumen
    story.append(Paragraph("Resumen General", styles['Heading2']))
    resumen_data = [
        ["Métrica", "Valor"],
        ["Lecturas registradas", str(resumen["num_lecturas"])],
        ["Consumo total", f"{resumen['consumo_total_m3']} m³ / {resumen['consumo_total_kwh']} kWh"],
        ["Coste total estimado", f"{resumen['coste_total']} €"],
        ["Precio kWh actual", f"{resumen['precio_actual']} €/kWh"],
        ["Factor conversión", cfg["factor_conversion"]],
        ["Cuota fija", f"{cfg['cuota_fija']} €"],
    ]
    t = Table(resumen_data, colWidths=[8*cm, 8*cm])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1a73e8')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.HexColor('#f8f9fa'), colors.white]),
        ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('PADDING', (0,0), (-1,-1), 6),
    ]))
    story.append(t)
    story.append(Spacer(1, 0.5*cm))

    # Historial
    story.append(Paragraph("Historial de Lecturas", styles['Heading2']))
    hist_data = [["Fecha", "Contador (m³)", "Consumo (m³)", "Consumo (kWh)", "Coste (€)", "Nota"]]
    for i in range(1, len(rows)):
        r = rows[i]
        prev = rows[i-1]
        m3 = round(r["valor_contador"] - prev["valor_contador"], 3)
        f = r["factor_conversion"] or factor
        p = r["precio_kwh"] or precio_default
        c = r["cuota_fija"] or cuota
        kwh = calcular_consumo_kwh(m3, f)
        coste = calcular_coste(kwh, p, c)
        hist_data.append([
            r["fecha"][:10],
            f"{r['valor_contador']:.2f}",
            f"{m3:.3f}",
            f"{kwh:.2f}",
            f"{coste:.2f}",
            (r["nota"] or "")[:30]
        ])
    if len(rows) > 0 and len(hist_data) == 1:
        hist_data.append(["Sin lecturas suficientes", "", "", "", "", ""])

    ht = Table(hist_data, colWidths=[2.8*cm, 2.8*cm, 2.5*cm, 2.8*cm, 2.5*cm, 3.6*cm])
    ht.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#34a853')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.HexColor('#f0fff4'), colors.white]),
        ('GRID', (0,0), (-1,-1), 0.3, colors.grey),
        ('FONTSIZE', (0,0), (-1,-1), 8),
        ('PADDING', (0,0), (-1,-1), 4),
    ]))
    story.append(ht)

    doc.build(story)
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=gascontrol_informe.pdf"}
    )

@app.get("/api/export/sql")
async def export_sql():
    conn = get_db()
    rows = conn.execute("SELECT * FROM lecturas ORDER BY fecha ASC").fetchall()
    cfg_rows = conn.execute("SELECT * FROM config").fetchall()
    conn.close()
    lines = [
        "-- GasControl Backup SQL",
        f"-- Generado: {datetime.now().isoformat()}",
        "",
        "CREATE TABLE IF NOT EXISTS lecturas (id INTEGER PRIMARY KEY AUTOINCREMENT, fecha TEXT, valor_contador REAL, nota TEXT, precio_kwh REAL, factor_conversion REAL, cuota_fija REAL, created_at TEXT);",
        ""
    ]
    for r in rows:
        lines.append(f"INSERT INTO lecturas (fecha, valor_contador, nota, precio_kwh, factor_conversion, cuota_fija, created_at) VALUES ('{r['fecha']}', {r['valor_contador']}, '{r['nota'] or ''}', {r['precio_kwh'] or 'NULL'}, {r['factor_conversion'] or 'NULL'}, {r['cuota_fija'] or 'NULL'}, '{r['created_at']}');")
    lines.append("")
    for r in cfg_rows:
        lines.append(f"INSERT OR REPLACE INTO config (clave, valor) VALUES ('{r['clave']}', '{r['valor']}');")
    sql_content = "\n".join(lines)
    return StreamingResponse(
        io.BytesIO(sql_content.encode("utf-8")),
        media_type="text/plain",
        headers={"Content-Disposition": "attachment; filename=gascontrol_backup.sql"}
    )
