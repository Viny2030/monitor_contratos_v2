from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from datetime import datetime
import pandas as pd
import requests
import io
import os
import json

# ── Configuración GitHub ─────────────────────────────────────────────────────
GITHUB_REPO  = "Viny2030/monitor_contratos_v2"
GITHUB_BRANCH = "feature/base-datos"
GITHUB_API   = f"https://api.github.com/repos/{GITHUB_REPO}/git/trees/{GITHUB_BRANCH}?recursive=1"
GITHUB_RAW   = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/"

# ── Lifespan ─────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("✅ Monitor XAI arrancando — cargando datos desde GitHub...")
    _cargar_cache_desde_github()
    yield
    print("Monitor XAI apagando")

app = FastAPI(
    title="Monitor XAI - Ph.D. Monteverde",
    description="Algoritmos contra la Corrupción",
    version="2.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# DATA_DIR se usa solo como fallback local (Railway Volume si existe)
DATA_DIR = "/app/data" if os.path.exists("/app") else "data"
os.makedirs(DATA_DIR, exist_ok=True)

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "dev")

# ── Cache global ─────────────────────────────────────────────────────────────
_cache = {
    "df": None,           # DataFrame del último reporte
    "archivos": [],       # lista de paths relativos en GitHub
    "ultimo": None,       # nombre del último archivo
    "ts": None,           # timestamp de última carga
}

# ── Helpers GitHub ───────────────────────────────────────────────────────────

def _listar_xlsx_github() -> list[str]:
    """Lista todos los xlsx de data/ en el repo de GitHub vía API."""
    try:
        resp = requests.get(GITHUB_API, timeout=15)
        resp.raise_for_status()
        tree = resp.json().get("tree", [])
        archivos = [
            item["path"] for item in tree
            if item["path"].startswith("data/")
            and item["path"].endswith(".xlsx")
            and "reporte_202" in item["path"]
        ]
        archivos.sort(reverse=True)   # más reciente primero
        return archivos
    except Exception as e:
        print(f"⚠️  Error listando GitHub: {e}")
        return []


def _leer_xlsx_github(path_relativo: str) -> pd.DataFrame:
    """Descarga y lee un xlsx directamente desde GitHub raw."""
    url = GITHUB_RAW + path_relativo
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    xl = pd.ExcelFile(io.BytesIO(resp.content), engine="openpyxl")
    hojas = ["🚨 Flujo Completo", "🔗 Flujo Cruzado", "Sheet1"]
    hoja  = next((h for h in hojas if h in xl.sheet_names), xl.sheet_names[0])
    return xl.parse(hoja)


def _cargar_cache_desde_github():
    """Descarga el xlsx más reciente de GitHub y lo guarda en caché."""
    global _cache
    archivos = _listar_xlsx_github()
    if not archivos:
        # Fallback: intentar leer desde disco local (Railway Volume)
        archivos_local = _buscar_xlsx_local()
        if archivos_local:
            print(f"📂 Usando archivo local: {archivos_local[0]}")
            try:
                xl = pd.ExcelFile(archivos_local[0], engine="openpyxl")
                hojas = ["🚨 Flujo Completo", "🔗 Flujo Cruzado", "Sheet1"]
                hoja  = next((h for h in hojas if h in xl.sheet_names), xl.sheet_names[0])
                df = xl.parse(hoja)
                _cache.update({
                    "df": df,
                    "archivos": archivos_local,
                    "ultimo": os.path.basename(archivos_local[0]),
                    "ts": datetime.now().isoformat(),
                })
                print(f"✅ Cache cargado desde disco: {len(df)} registros")
            except Exception as e:
                print(f"❌ Error leyendo archivo local: {e}")
        else:
            print("⚠️  Sin datos disponibles (ni GitHub ni disco local)")
        return

    try:
        df = _leer_xlsx_github(archivos[0])
        _cache.update({
            "df": df,
            "archivos": archivos,
            "ultimo": archivos[0].split("/")[-1],
            "ts": datetime.now().isoformat(),
        })
        print(f"✅ Cache cargado desde GitHub: {len(df)} registros — {archivos[0]}")
    except Exception as e:
        print(f"❌ Error cargando xlsx desde GitHub: {e}")


# ── Helpers locales (fallback) ───────────────────────────────────────────────

def _buscar_xlsx_local() -> list[str]:
    archivos = []
    for root, _, files in os.walk(DATA_DIR):
        for f in files:
            if f.startswith("reporte_202") and f.endswith(".xlsx"):
                archivos.append(os.path.join(root, f))
    archivos.sort(key=lambda x: os.path.getmtime(x), reverse=True)
    return archivos


def etiqueta_archivo(ruta: str) -> str:
    partes = ruta.replace("\\", "/").split("/")
    return f"{partes[-2]} / {partes[-1]}" if len(partes) >= 3 else partes[-1]

# ── AGREGAR ESTAS RUTAS AL main.py existente ────────────────────────────────
# Pegar después de app.mount("/static", ...) y antes de los endpoints API


@app.get("/documentacion", response_class=HTMLResponse)
async def documentacion(request: Request):
    """Página de documentación con bio del autor, instructivo y escenarios XAI."""
    return templates.TemplateResponse("documentacion.html", {"request": request})


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Página principal — redirige al dashboard."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/monitor", response_class=HTMLResponse)
async def monitor_page(request: Request, clave: str = ""):
    """Monitor con estadísticas — requiere clave monitor_2026."""
    MONITOR_KEY = os.getenv("MONITOR_KEY", "monitor_2026")
    autenticado = (clave == MONITOR_KEY)
    return templates.TemplateResponse("monitor.html", {
        "request": request,
        "autenticado": autenticado,
    })


@app.get("/mapa", response_class=HTMLResponse)
async def mapa_redirect(request: Request):
    """Redirige al Mapa de Transparencia externo."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="https://mapatransparencia-production.up.railway.app/")
# ── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/api/status")
def status():
    """Estado del servicio y último reporte disponible."""
    total_contratos = 0
    if _cache["df"] is not None and not _cache["df"].empty:
        total_contratos = len(_cache["df"])

    return {
        "servicio": "Monitor XAI - monitor_contratos_v2",
        "version": "2.0.0",
        "status": "activo",
        "ultimo_reporte": _cache.get("ultimo"),
        "total_contratos": total_contratos,
        "reportes_en_github": len(_cache.get("archivos", [])),
        "cache_timestamp": _cache.get("ts"),
        "timestamp": datetime.now().isoformat(),
    }


@app.post("/api/reload")
def reload_data(x_refresh_token: str = Header(None)):
    """
    Recarga el caché leyendo el xlsx más reciente desde GitHub.
    Lo llama el GitHub Actions después de cada push diario.
    """
    if x_refresh_token != REFRESH_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")

    _cargar_cache_desde_github()

    return {
        "status": "ok",
        "mensaje": "Caché recargado desde GitHub",
        "ultimo_reporte": _cache.get("ultimo"),
        "total_contratos": len(_cache["df"]) if _cache["df"] is not None else 0,
        "timestamp": datetime.now().isoformat(),
    }


@app.post("/api/refresh")
def refresh(x_refresh_token: str = Header(None)):
    """
    Alias de /api/reload para compatibilidad con versiones anteriores.
    También acepta disparar el scraping si DATABASE_URL está disponible.
    """
    if x_refresh_token != REFRESH_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")

    # Primero intentar recargar desde GitHub (más rápido)
    _cargar_cache_desde_github()

    # Si hay DATABASE_URL, también intentar scraping completo
    if os.getenv("DATABASE_URL"):
        try:
            from diario import (extraer_bora_licitaciones, extraer_bora_adjudicaciones,
                                extraer_comprar, extraer_pagos_tgn, cruzar_fuentes, guardar_excels)
            df_bora   = extraer_bora_licitaciones()
            df_adj    = extraer_bora_adjudicaciones(df_bora)
            df_licit  = (df_bora[df_bora["es_adjudicacion"] == False].copy().reset_index(drop=True)
                         if not df_bora.empty else pd.DataFrame())
            df_comprar = extraer_comprar()
            df_tgn    = extraer_pagos_tgn()
            df_cruce  = cruzar_fuentes(df_adj, df_comprar, df_tgn)
            guardar_excels(df_cruce, df_adj, df_licit, df_comprar, df_tgn)
            _cache["df"] = df_cruce
            return {
                "status": "ok",
                "fuente": "scraping_directo",
                "mensaje": "Ciclo de scraping completado",
                "timestamp": datetime.now().isoformat(),
                "registros": len(df_cruce),
            }
        except Exception as e:
            # Si falla el scraping, igual devolver los datos de GitHub
            print(f"⚠️  Scraping falló: {e} — usando datos de GitHub")

    return {
        "status": "ok",
        "fuente": "github",
        "mensaje": "Caché recargado desde GitHub",
        "ultimo_reporte": _cache.get("ultimo"),
        "total_contratos": len(_cache["df"]) if _cache["df"] is not None else 0,
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/resumen")
def resumen():
    """KPIs ejecutivos del último reporte."""
    df = _cache.get("df")
    if df is None or df.empty:
        return {"total": 0, "alto_riesgo": 0, "mensaje": "Sin datos — esperando próximo ciclo diario"}

    alto_riesgo = 0
    if "riesgo" in df.columns:
        alto_riesgo = int((df["riesgo"].astype(str).str.upper() == "ALTO").sum())
    elif "nivel_alerta" in df.columns:
        alto_riesgo = int((df["nivel_alerta"].astype(str).str.upper() == "ALTO").sum())
    elif "nivel_riesgo_licit" in df.columns:
        alto_riesgo = int((df["nivel_riesgo_licit"].astype(str).str.upper() == "ALTO").sum())

    return {
        "total": len(df),
        "alto_riesgo": alto_riesgo,
        "columnas": list(df.columns[:10]),
        "ultimo_reporte": _cache.get("ultimo"),
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/archivos")
def listar_archivos(x_refresh_token: str = Header(None)):
    """Lista todos los reportes xlsx disponibles en el repo de GitHub."""
    if x_refresh_token != REFRESH_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")

    archivos = _listar_xlsx_github()
    return {
        "total": len(archivos),
        "archivos": archivos,
        "timestamp": datetime.now().isoformat(),
    }
