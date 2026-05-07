from fastapi import FastAPI, HTTPException, UploadFile, File, Query, Depends, Form, status
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import StreamingResponse, HTMLResponse, RedirectResponse
from fastapi.requests import Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse
from pydantic import BaseModel
from typing import Optional
import sqlite3, csv, io, httpx, os, hashlib, hmac
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, HRFlowable

app = FastAPI(title="GasControl")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(BASE_DIR, "data", "gascontrol.db")

# ── AUTH CONFIG ─────────────────────────────────────────────────────────────
SECRET_KEY   = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
APP_USER     = os.environ.get("APP_USER", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "admin")

# IMPORTANT: AuthMiddleware must be added AFTER SessionMiddleware.
# Starlette wraps middlewares in reverse order, so the last added
# is the outermost (first to run). We need SessionMiddleware to run
# first so request.session is available inside AuthMiddleware.
class AuthMiddleware(BaseHTTPMiddleware):
    OPEN_PATHS = {"/login", "/static", "/.well-known"}
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path.startswith(p) for p in self.OPEN_PATHS):
            return await call_next(request)
        if path == "/login" and request.method == "POST":
            return await call_next(request)
        try:
            authenticated = request.session.get("authenticated")
        except AssertionError:
            authenticated = False
        if not authenticated:
            if path.startswith("/api/"):
                return StarletteResponse('{"detail":"No autenticado"}', status_code=401,
                                         media_type="application/json")
            return RedirectResponse("/login", status_code=302)
        return await call_next(request)

# Order matters: SessionMiddleware first, then AuthMiddleware on top
app.add_middleware(AuthMiddleware)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=86400 * 30)

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# ── AUTH HELPERS ────────────────────────────────────────────────────────────
def verify_password(plain: str, stored: str) -> bool:
    return hmac.compare_digest(plain, stored)

def require_auth(request: Request):
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No autenticado")

def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("authenticated"))

# ── DB ─────────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db(); c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS lecturas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fecha TEXT NOT NULL, valor_contador REAL NOT NULL,
        nota TEXT, precio_kwh REAL, factor_conversion REAL, cuota_fija REAL,
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(fecha, valor_contador))""")
    # migration: add UNIQUE if missing
    indexes = [r["name"] for r in c.execute("PRAGMA index_list(lecturas)").fetchall()]
    if not any("sqlite_autoindex" in i.lower() for i in indexes):
        c.execute("ALTER TABLE lecturas RENAME TO lecturas_old")
        c.execute("""CREATE TABLE lecturas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL, valor_contador REAL NOT NULL,
            nota TEXT, precio_kwh REAL, factor_conversion REAL, cuota_fija REAL,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(fecha, valor_contador))""")
        c.execute("""INSERT OR IGNORE INTO lecturas
            (id,fecha,valor_contador,nota,precio_kwh,factor_conversion,cuota_fija,created_at)
            SELECT id,fecha,valor_contador,nota,precio_kwh,factor_conversion,cuota_fija,created_at
            FROM lecturas_old""")
        c.execute("DROP TABLE lecturas_old")
    c.execute("""CREATE TABLE IF NOT EXISTS config (clave TEXT PRIMARY KEY, valor TEXT NOT NULL)""")
    for k,v in [("factor_conversion","10.23"),("cuota_fija","10.50"),
                ("tarifa_tur","TUR2"),("precio_kwh_manual","0.0650")]:
        c.execute("INSERT OR IGNORE INTO config (clave,valor) VALUES (?,?)", (k,v))
    conn.commit(); conn.close()

init_db()

# ── MODELS ──────────────────────────────────────────────────────────────────
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

# ── HELPERS ─────────────────────────────────────────────────────────────────
def get_config(conn):
    return {r["clave"]: r["valor"] for r in conn.execute("SELECT clave,valor FROM config").fetchall()}

def parse_fecha(f: str) -> str:
    for fmt in ("%d/%m/%Y %H:%M","%d/%m/%Y","%Y-%m-%d %H:%M:%S","%Y-%m-%dT%H:%M","%Y-%m-%d %H:%M","%Y-%m-%d"):
        try: return datetime.strptime(f.strip(), fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError: continue
    raise ValueError(f"Formato de fecha no reconocido: '{f}'")

def dias_entre(a: str, b: str) -> float:
    fa = datetime.strptime(a, "%Y-%m-%d %H:%M:%S")
    fb = datetime.strptime(b, "%Y-%m-%d %H:%M:%S")
    return abs((fb - fa).total_seconds() / 86400)

def calcular_kwh(m3, factor): return round(m3 * factor, 4)

def calcular_coste(kwh, precio, cuota_mensual, dias):
    """Gas cost + prorated monthly fixed fee (30.44 avg days/month)."""
    return round(kwh * precio + cuota_mensual * (dias / 30.44), 4)

def get_all_asc(conn):
    return conn.execute("SELECT * FROM lecturas ORDER BY fecha ASC").fetchall()

def validate_reading(conn, fecha_iso: str, valor: float):
    """
    Returns ("ok"|"duplicate"|"error", message_or_None).
    Rules: counter is monotonically non-decreasing.
    - exact (fecha,valor) exists → duplicate
    - valor < reading_before    → error (counter reversed)
    - valor > reading_after     → error (breaks future readings)
    """
    if conn.execute("SELECT id FROM lecturas WHERE fecha=? AND valor_contador=?",
                    (fecha_iso, valor)).fetchone():
        return ("duplicate", None)
    before = conn.execute(
        "SELECT fecha,valor_contador FROM lecturas WHERE fecha<? ORDER BY fecha DESC LIMIT 1",
        (fecha_iso,)).fetchone()
    after = conn.execute(
        "SELECT fecha,valor_contador FROM lecturas WHERE fecha>? ORDER BY fecha ASC LIMIT 1",
        (fecha_iso,)).fetchone()
    if before and round(valor,3) < round(before["valor_contador"],3):
        return ("error",
            f"El valor {valor:.3f} m³ es inferior a la lectura anterior "
            f"({before['valor_contador']:.3f} m³ del {before['fecha'][:10]}). "
            "El contador no puede retroceder.")
    if after and round(valor,3) > round(after["valor_contador"],3):
        return ("error",
            f"El valor {valor:.3f} m³ es superior a la lectura siguiente "
            f"({after['valor_contador']:.3f} m³ del {after['fecha'][:10]}). "
            "Esto rompería la coherencia del historial.")
    return ("ok", None)

def build_periodos(rows, factor_d, precio_d, cuota_d):
    """Compute per-period stats. First reading has no consumo."""
    result = []
    for i, r in enumerate(rows):
        if i == 0:
            result.append({"id":r["id"],"fecha":r["fecha"],"valor_contador":r["valor_contador"],
                           "consumo_m3":None,"consumo_kwh":None,"coste":None,"dias":None,
                           "nota":r["nota"],"precio_kwh":r["precio_kwh"]})
        else:
            prev = rows[i-1]
            m3   = round(r["valor_contador"] - prev["valor_contador"], 4)
            f    = r["factor_conversion"] or factor_d
            p    = r["precio_kwh"]        or precio_d
            c    = r["cuota_fija"]        or cuota_d
            dias = dias_entre(prev["fecha"], r["fecha"])
            kwh  = calcular_kwh(m3, f)
            cost = calcular_coste(kwh, p, c, dias)
            result.append({"id":r["id"],"fecha":r["fecha"],"valor_contador":r["valor_contador"],
                           "consumo_m3":m3,"consumo_kwh":kwh,"coste":cost,"dias":round(dias,1),
                           "nota":r["nota"],"precio_kwh":r["precio_kwh"]})
    return result

# ── ROUTES ───────────────────────────────────────────────────────────────────
# ── AUTH ROUTES ─────────────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse("/", status_code=302)
    error = request.session.pop("login_error", None)
    return templates.TemplateResponse("login.html", {"request": request, "error": error})

@app.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == APP_USER and verify_password(password, APP_PASSWORD):
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=302)
    request.session["login_error"] = "Usuario o contraseña incorrectos"
    return RedirectResponse("/login", status_code=302)

@app.get("/logout")
@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/config")
async def api_get_config():
    conn = get_db(); cfg = get_config(conn); conn.close(); return cfg

@app.post("/api/config")
async def api_update_config(data: ConfigUpdate):
    conn = get_db()
    for k,v in data.dict(exclude_none=True).items():
        conn.execute("INSERT OR REPLACE INTO config (clave,valor) VALUES (?,?)", (k,str(v)))
    conn.commit(); conn.close(); return {"ok": True}

@app.get("/api/precio-tur")
async def api_precio_tur():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://precios-tur.petru.tech/api/precio-actual")
            if r.status_code == 200: return {"ok":True,"data":r.json()}
            r2 = await client.get("https://precios-tur.petru.tech/api/precios")
            if r2.status_code == 200: return {"ok":True,"data":r2.json()}
    except Exception: pass
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://precios-tur.petru.tech/")
            return {"ok":True,"raw":r.text[:500]}
    except Exception as e:
        return {"ok":False,"error":str(e)}

@app.post("/api/lecturas")
async def api_crear_lectura(data: LecturaCreate):
    conn = get_db(); cfg = get_config(conn)
    factor = float(cfg["factor_conversion"])
    cuota  = float(cfg["cuota_fija"])
    precio = data.precio_kwh if data.precio_kwh else float(cfg["precio_kwh_manual"])
    try:
        fecha_iso = parse_fecha(data.fecha)
    except ValueError as e:
        conn.close(); raise HTTPException(400, str(e))
    valor = round(data.valor_contador, 4)
    status, msg = validate_reading(conn, fecha_iso, valor)
    conn.close()
    if status == "duplicate":
        raise HTTPException(409, "Lectura duplicada: ya existe una lectura con esa fecha y valor.")
    if status == "error":
        raise HTTPException(422, msg)
    conn2 = get_db(); cfg2 = get_config(conn2)
    conn2.execute(
        "INSERT INTO lecturas (fecha,valor_contador,nota,precio_kwh,factor_conversion,cuota_fija) VALUES (?,?,?,?,?,?)",
        (fecha_iso, valor, data.nota, precio, float(cfg2["factor_conversion"]), float(cfg2["cuota_fija"])))
    conn2.commit(); conn2.close()
    return {"ok": True}

@app.get("/api/lecturas")
async def api_get_lecturas(
    desde: Optional[str]=None, hasta: Optional[str]=None,
    limit: int=Query(50,ge=1,le=500), offset: int=0):
    conn = get_db(); cfg = get_config(conn)
    factor = float(cfg["factor_conversion"]); cuota = float(cfg["cuota_fija"])
    precio_d = float(cfg["precio_kwh_manual"])
    all_rows = get_all_asc(conn); conn.close()
    periodos = build_periodos(all_rows, factor, precio_d, cuota)
    filtered = []
    for p in periodos:
        fd = p["fecha"]
        ok = True
        if desde and fd < (desde+" 00:00:00" if len(desde)==10 else desde): ok=False
        if hasta and fd > (hasta+" 23:59:59" if len(hasta)==10 else hasta): ok=False
        if ok: filtered.append(p)
    total = len(filtered)
    paginated = list(reversed(filtered))[offset:offset+limit]
    # Resumen del período filtrado (only periods with actual consumption)
    periodos_con_consumo = [p for p in filtered if p["consumo_m3"] is not None]
    resumen_periodo = {
        "consumo_m3":  round(sum(p["consumo_m3"]  for p in periodos_con_consumo), 3),
        "consumo_kwh": round(sum(p["consumo_kwh"] for p in periodos_con_consumo), 2),
        "coste":       round(sum(p["coste"]        for p in periodos_con_consumo), 2),
        "num_periodos": len(periodos_con_consumo),
    } if periodos_con_consumo else None
    return {"total": total, "lecturas": paginated, "resumen_periodo": resumen_periodo}

@app.get("/api/resumen")
async def api_resumen():
    conn = get_db(); cfg = get_config(conn)
    factor = float(cfg["factor_conversion"]); cuota = float(cfg["cuota_fija"])
    precio_d = float(cfg["precio_kwh_manual"])
    all_rows = get_all_asc(conn); conn.close()
    if not all_rows:
        return {"ultima_lectura":None,"consumo_total_m3":0,"consumo_total_kwh":0,
                "coste_total":0,"precio_actual":precio_d,"num_lecturas":0}
    periodos = build_periodos(all_rows, factor, precio_d, cuota)
    return {
        "ultima_lectura": {"fecha":all_rows[-1]["fecha"],"valor":all_rows[-1]["valor_contador"]},
        "consumo_total_m3":  round(all_rows[-1]["valor_contador"]-all_rows[0]["valor_contador"],3),
        "consumo_total_kwh": round(sum(p["consumo_kwh"] for p in periodos if p["consumo_kwh"] is not None),2),
        "coste_total":       round(sum(p["coste"]       for p in periodos if p["coste"]       is not None),2),
        "precio_actual":     precio_d,
        "num_lecturas":      len(all_rows),
    }

def _filter_periodos(periodos, desde, hasta):
    """Apply date filter to a list of periodos. Default: last 2 years."""
    from dateutil.relativedelta import relativedelta
    if not desde and not hasta:
        cutoff = (datetime.now()-relativedelta(years=2)).strftime("%Y-%m-%d 00:00:00")
        return [p for p in periodos if p["fecha"] >= cutoff]
    result = periodos[:]
    if desde:
        d = desde+" 00:00:00" if len(desde)==10 else desde
        result = [p for p in result if p["fecha"] >= d]
    if hasta:
        h = hasta+" 23:59:59" if len(hasta)==10 else hasta
        result = [p for p in result if p["fecha"] <= h]
    return result

@app.get("/api/grafica")
async def api_grafica(desde: Optional[str]=None, hasta: Optional[str]=None):
    """Raw period consumo (m3 bruto entre lecturas) — original endpoint kept for PDF."""
    conn = get_db(); cfg = get_config(conn)
    factor = float(cfg["factor_conversion"]); cuota = float(cfg["cuota_fija"])
    precio_d = float(cfg["precio_kwh_manual"])
    all_rows = get_all_asc(conn); conn.close()
    if not all_rows: return {"labels":[],"consumo_m3":[],"consumo_kwh":[],"costes":[]}
    periodos = _filter_periodos(build_periodos(all_rows, factor, precio_d, cuota), desde, hasta)
    labels,cm3,ckwh,costes = [],[],[],[]
    for p in periodos:
        if p["consumo_m3"] is not None:
            labels.append(p["fecha"][:10]); cm3.append(p["consumo_m3"])
            ckwh.append(p["consumo_kwh"]); costes.append(p["coste"])
    return {"labels":labels,"consumo_m3":cm3,"consumo_kwh":ckwh,"costes":costes}

@app.get("/api/grafica/diario")
async def api_grafica_diario(desde: Optional[str]=None, hasta: Optional[str]=None):
    """
    Consumo normalizado a m³/día y €/día para cada período.
    Makes periods with very different durations directly comparable.
    """
    conn = get_db(); cfg = get_config(conn)
    factor = float(cfg["factor_conversion"]); cuota = float(cfg["cuota_fija"])
    precio_d = float(cfg["precio_kwh_manual"])
    all_rows = get_all_asc(conn); conn.close()
    if not all_rows: return {"labels":[],"m3_dia":[],"kwh_dia":[],"coste_dia":[]}
    periodos = _filter_periodos(build_periodos(all_rows, factor, precio_d, cuota), desde, hasta)
    labels,m3d,kwhd,costd = [],[],[],[]
    for p in periodos:
        if p["consumo_m3"] is not None and p["dias"] and p["dias"] > 0:
            labels.append(p["fecha"][:10])
            m3d.append(round(p["consumo_m3"] / p["dias"], 4))
            kwhd.append(round(p["consumo_kwh"] / p["dias"], 4))
            costd.append(round(p["coste"] / p["dias"], 4))
    return {"labels":labels,"m3_dia":m3d,"kwh_dia":kwhd,"coste_dia":costd}

@app.get("/api/grafica/mensual")
async def api_grafica_mensual(desde: Optional[str]=None, hasta: Optional[str]=None):
    """
    Consumo aggregated by calendar month.
    For each period that spans multiple months, consumption is distributed
    proportionally by days within each month.
    """
    conn = get_db(); cfg = get_config(conn)
    factor = float(cfg["factor_conversion"]); cuota = float(cfg["cuota_fija"])
    precio_d = float(cfg["precio_kwh_manual"])
    all_rows = get_all_asc(conn); conn.close()
    if not all_rows: return {"labels":[],"consumo_m3":[],"consumo_kwh":[],"costes":[]}

    # Use ALL periodos (no date filter yet) to distribute consumption correctly
    all_periodos = build_periodos(all_rows, factor, precio_d, cuota)

    from collections import defaultdict
    import calendar

    monthly_m3    = defaultdict(float)
    monthly_kwh   = defaultdict(float)
    monthly_coste = defaultdict(float)

    for p in all_periodos:
        if p["consumo_m3"] is None or p["dias"] is None or p["dias"] == 0:
            continue
        # Period goes from previous reading date to current reading date
        # Find previous reading date from all_rows
        idx = next((i for i,r in enumerate(all_rows) if r["id"]==p["id"]), None)
        if idx is None or idx == 0:
            continue
        date_start = datetime.strptime(all_rows[idx-1]["fecha"], "%Y-%m-%d %H:%M:%S")
        date_end   = datetime.strptime(p["fecha"], "%Y-%m-%d %H:%M:%S")
        total_days = (date_end - date_start).total_seconds() / 86400
        if total_days == 0:
            continue

        # Walk through each calendar month this period touches
        cur = date_start
        while cur < date_end:
            # Last day of current month
            last_day = calendar.monthrange(cur.year, cur.month)[1]
            month_end = cur.replace(day=last_day, hour=23, minute=59, second=59)
            segment_end = min(month_end, date_end)
            seg_days = (segment_end - cur).total_seconds() / 86400
            fraction = seg_days / total_days
            key = f"{cur.year}-{cur.month:02d}"
            monthly_m3[key]    += p["consumo_m3"]  * fraction
            monthly_kwh[key]   += p["consumo_kwh"] * fraction
            monthly_coste[key] += p["coste"]        * fraction
            # Move to first day of next month
            if cur.month == 12:
                cur = cur.replace(year=cur.year+1, month=1, day=1, hour=0, minute=0, second=0)
            else:
                cur = cur.replace(month=cur.month+1, day=1, hour=0, minute=0, second=0)

    # Now apply date filter to monthly aggregates
    sorted_keys = sorted(monthly_m3.keys())
    if not desde and not hasta:
        from dateutil.relativedelta import relativedelta
        cutoff = (datetime.now()-relativedelta(years=2)).strftime("%Y-%m")
        sorted_keys = [k for k in sorted_keys if k >= cutoff]
    else:
        if desde: sorted_keys = [k for k in sorted_keys if k >= desde[:7]]
        if hasta: sorted_keys = [k for k in sorted_keys if k <= hasta[:7]]

    labels = [f"{k[5:]}/{k[:4]}" for k in sorted_keys]  # MM/YYYY
    return {
        "labels":      labels,
        "consumo_m3":  [round(monthly_m3[k],   3) for k in sorted_keys],
        "consumo_kwh": [round(monthly_kwh[k],  2) for k in sorted_keys],
        "costes":      [round(monthly_coste[k],2) for k in sorted_keys],
    }

@app.delete("/api/lecturas/{lid}")
async def api_delete(lid: int):
    conn = get_db()
    conn.execute("DELETE FROM lecturas WHERE id=?", (lid,))
    conn.commit(); conn.close(); return {"ok": True}

# ── EXPORT / IMPORT ──────────────────────────────────────────────────────────
@app.get("/api/export/csv")
async def export_csv():
    conn = get_db()
    rows = conn.execute("SELECT fecha,valor_contador,nota,precio_kwh,factor_conversion,cuota_fija FROM lecturas ORDER BY fecha ASC").fetchall()
    conn.close()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["fecha","valor_contador","nota","precio_kwh","factor_conversion","cuota_fija"])
    for r in rows:
        w.writerow([r["fecha"],r["valor_contador"],r["nota"] or "",
                    r["precio_kwh"] or "",r["factor_conversion"] or "",r["cuota_fija"] or ""])
    out.seek(0)
    return StreamingResponse(io.BytesIO(out.getvalue().encode("utf-8-sig")), media_type="text/csv",
        headers={"Content-Disposition":"attachment; filename=gascontrol_lecturas.csv"})

@app.get("/api/export/template")
async def export_template():
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(["fecha","valor_contador","nota"])
    w.writerow(["20/12/2023 12:00","269.00","Lectura histórica"])
    w.writerow(["18/01/2024 12:00","472.00","Lectura histórica"])
    out.seek(0)
    return StreamingResponse(io.BytesIO(out.getvalue().encode("utf-8-sig")), media_type="text/csv",
        headers={"Content-Disposition":"attachment; filename=plantilla_lecturas.csv"})

@app.post("/api/import/csv")
async def import_csv(file: UploadFile = File(...)):
    content = await file.read()
    try:    text = content.decode("utf-8-sig")
    except: text = content.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    conn = get_db(); cfg = get_config(conn)
    factor = float(cfg["factor_conversion"]); cuota = float(cfg["cuota_fija"])
    precio = float(cfg["precio_kwh_manual"])

    parse_errors, parsed = [], []
    for i, row in enumerate(reader):
        try:
            fecha = parse_fecha(row.get("fecha",""))
            valor = round(float(row.get("valor_contador","").replace(",",".")), 4)
            nota  = row.get("nota","")
            p = float(row["precio_kwh"])      if row.get("precio_kwh")      else precio
            f = float(row["factor_conversion"]) if row.get("factor_conversion") else factor
            c = float(row["cuota_fija"])      if row.get("cuota_fija")      else cuota
            parsed.append((fecha,valor,nota,p,f,c))
        except Exception as e:
            parse_errors.append(f"Fila {i+2}: {e}")

    parsed.sort(key=lambda r: r[0])   # chronological order mandatory

    imported, skipped_dup, val_errors = 0, 0, []
    for fecha,valor,nota,p,f,c in parsed:
        status, msg = validate_reading(conn, fecha, valor)
        if status == "duplicate":
            skipped_dup += 1
        elif status == "error":
            val_errors.append(f"{fecha[:10]} ({valor:.3f} m³): {msg}")
        else:
            conn.execute(
                "INSERT OR IGNORE INTO lecturas (fecha,valor_contador,nota,precio_kwh,factor_conversion,cuota_fija) VALUES (?,?,?,?,?,?)",
                (fecha,valor,nota,p,f,c))
            imported += 1

    conn.commit(); conn.close()
    return {"ok":True,"imported":imported,"skipped_duplicates":skipped_dup,
            "errors": parse_errors + val_errors}

@app.get("/api/export/pdf")
async def export_pdf(desde: Optional[str]=None, hasta: Optional[str]=None):
    conn = get_db(); cfg = get_config(conn)
    factor = float(cfg["factor_conversion"]); cuota = float(cfg["cuota_fija"])
    precio_d = float(cfg["precio_kwh_manual"])
    all_rows = get_all_asc(conn); resumen = await api_resumen(); conn.close()
    periodos = build_periodos(all_rows, factor, precio_d, cuota)
    if desde: periodos = [p for p in periodos if p["fecha"] >= (desde+" 00:00:00" if len(desde)==10 else desde)]
    if hasta: periodos = [p for p in periodos if p["fecha"] <= (hasta+" 23:59:59" if len(hasta)==10 else hasta)]

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=2*cm, leftMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet(); story = []
    ts = ParagraphStyle('t', parent=styles['Title'], fontSize=20, textColor=colors.HexColor('#1a73e8'), spaceAfter=6)
    story.append(Paragraph("GasControl - Informe de Consumo", ts))
    story.append(Paragraph(f"Generado: {datetime.now().strftime('%d/%m/%Y %H:%M')}", styles['Normal']))
    story.append(Spacer(1,.4*cm))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#1a73e8')))
    story.append(Spacer(1,.3*cm))
    story.append(Paragraph("Resumen General", styles['Heading2']))
    rd = [["Métrica","Valor"],
          ["Lecturas",str(resumen["num_lecturas"])],
          ["Consumo total",f"{resumen['consumo_total_m3']} m³ / {resumen['consumo_total_kwh']} kWh"],
          ["Coste total",f"{resumen['coste_total']} €"],
          ["Precio kWh",f"{resumen['precio_actual']} €/kWh"],
          ["Factor conv.",cfg["factor_conversion"]],
          ["Cuota fija mensual",f"{cfg['cuota_fija']} € (prorrateada por días)"]]
    t = Table(rd, colWidths=[8*cm,8*cm])
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#1a73e8')),
        ('TEXTCOLOR',(0,0),(-1,0),colors.white),('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
        ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.HexColor('#f8f9fa'),colors.white]),
        ('GRID',(0,0),(-1,-1),.5,colors.grey),('FONTSIZE',(0,0),(-1,-1),9),('PADDING',(0,0),(-1,-1),6)]))
    story.append(t); story.append(Spacer(1,.4*cm))
    story.append(Paragraph("Historial de Lecturas", styles['Heading2']))
    hd = [["Fecha","Contador (m³)","Días","Consumo (m³)","Consumo (kWh)","Coste (€)","Nota"]]
    for p in periodos:
        hd.append([p["fecha"][:10], f"{p['valor_contador']:.2f}",
            f"{p['dias']:.0f}" if p["dias"] is not None else "—",
            f"{p['consumo_m3']:.3f}" if p["consumo_m3"] is not None else "—",
            f"{p['consumo_kwh']:.2f}" if p["consumo_kwh"] is not None else "—",
            f"{p['coste']:.2f}" if p["coste"] is not None else "—",
            (p["nota"] or "")[:25]])
    if len(hd)==1: hd.append(["Sin lecturas suficientes","","","","","",""])
    ht = Table(hd, colWidths=[2.4*cm,2.6*cm,1.4*cm,2.4*cm,2.6*cm,2.2*cm,3.4*cm])
    ht.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#34a853')),
        ('TEXTCOLOR',(0,0),(-1,0),colors.white),('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
        ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.HexColor('#f0fff4'),colors.white]),
        ('GRID',(0,0),(-1,-1),.3,colors.grey),('FONTSIZE',(0,0),(-1,-1),7.5),('PADDING',(0,0),(-1,-1),3)]))
    story.append(ht); doc.build(story); buf.seek(0)
    return StreamingResponse(buf, media_type="application/pdf",
        headers={"Content-Disposition":"attachment; filename=gascontrol_informe.pdf"})

@app.get("/api/export/sql")
async def export_sql():
    conn = get_db()
    rows = conn.execute("SELECT * FROM lecturas ORDER BY fecha ASC").fetchall()
    cfgr = conn.execute("SELECT * FROM config").fetchall(); conn.close()
    lines = ["-- GasControl Backup SQL", f"-- Generado: {datetime.now().isoformat()}", "",
        "CREATE TABLE IF NOT EXISTS lecturas (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "fecha TEXT, valor_contador REAL, nota TEXT, precio_kwh REAL, "
        "factor_conversion REAL, cuota_fija REAL, created_at TEXT, UNIQUE(fecha,valor_contador));",""]
    for r in rows:
        nota = (r["nota"] or "").replace("'","''")
        lines.append(f"INSERT OR IGNORE INTO lecturas (fecha,valor_contador,nota,precio_kwh,factor_conversion,cuota_fija,created_at) "
            f"VALUES ('{r['fecha']}',{r['valor_contador']},'{nota}',"
            f"{r['precio_kwh'] or 'NULL'},{r['factor_conversion'] or 'NULL'},{r['cuota_fija'] or 'NULL'},'{r['created_at']}');")
    lines.append("")
    for r in cfgr:
        lines.append(f"INSERT OR REPLACE INTO config (clave,valor) VALUES ('{r['clave']}','{r['valor']}');")
    return StreamingResponse(io.BytesIO("\n".join(lines).encode("utf-8")), media_type="text/plain",
        headers={"Content-Disposition":"attachment; filename=gascontrol_backup.sql"})