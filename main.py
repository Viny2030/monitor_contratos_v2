from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from datetime import datetime
import pandas as pd
import os
import json

# ── Lifespan — SIN scraping, solo inicialización ────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Railway solo sirve la API.
    El scraping diario lo maneja GitHub Actions (ejecucion_diaria.yml).
    """
    print("✅ Monitor XAI arrancando — modo API puro (sin scraping al inicio)")
    yield
    print("Monitor XAI apagando")

app = FastAPI(
    title="Monitor XAI - Ph.D. Monteverde",
    description="Algoritmos contra la Corrupción",
    version="1.0.0",
    lifespan=lifespan
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

DATA_DIR = "/app/data" if os.path.exists("/app") else "data"
os.makedirs(DATA_DIR, exist_ok=True)

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "dev")

_df_cache = None


# ── Helpers ─────────────────────────────────────────────────────────────────
def buscar_todos_los_xlsx(base_dir):
    archivos = []
    for root, dirs, files in os.walk(base_dir):
        for f in files:
            if f.startswith("reporte_202") and f.endswith(".xlsx"):
                archivos.append(os.path.join(root, f))
    archivos.sort(key=lambda x: os.path.getmtime(x), reverse=True)
    return archivos


def etiqueta_archivo(ruta):
    partes = ruta.replace("\\", "/").split("/")
    return f"{partes[-2]} / {partes[-1]}" if len(partes) >= 3 else partes[-1]


def leer_hoja(ruta, hojas_preferidas):
    try:
        xl = pd.ExcelFile(ruta)
        hoja = next((h for h in hojas_preferidas if h in xl.sheet_names), None)
        if not hoja:
            return []
        df = xl.parse(hoja).fillna("").astype(str)
        return df.to_dict(orient="records")
    except Exception as e:
        print(f"Error leyendo {ruta}: {e}")
        return []


def cargar_ultimo_reporte():
    global _df_cache
    if _df_cache is not None and not _df_cache.empty:
        return _df_cache
    archivos = buscar_todos_los_xlsx(DATA_DIR)
    if not archivos:
        return pd.DataFrame()
    try:
        xl = pd.ExcelFile(archivos[0])
        hojas = ["🚨 Flujo Completo", "🔗 Flujo Cruzado", "Sheet1"]
        hoja = next((h for h in hojas if h in xl.sheet_names), xl.sheet_names[0])
        return xl.parse(hoja)
    except Exception as e:
        print(f"Error cargando reporte: {e}")
        return pd.DataFrame()


def set_cache(df):
    global _df_cache
    _df_cache = df


# ── Endpoints ────────────────────────────────────────────────────────────────
@app.get("/api/status")
def status():
    """Estado del servicio y último reporte disponible."""
    archivos = buscar_todos_los_xlsx(DATA_DIR)
    ultimo = None
    total_contratos = 0
    if archivos:
        ultimo = os.path.basename(archivos[0])
        try:
            xl = pd.ExcelFile(archivos[0])
            hojas = ["🚨 Flujo Completo", "🔗 Flujo Cruzado", "Sheet1"]
            hoja = next((h for h in hojas if h in xl.sheet_names), xl.sheet_names[0])
            df = xl.parse(hoja)
            total_contratos = len(df)
        except Exception:
            pass
    return {
        "servicio": "Monitor XAI - monitor_contratos_v2",
        "version": "1.0.0",
        "status": "activo",
        "ultimo_reporte": ultimo,
        "total_contratos": total_contratos,
        "reportes_en_disco": len(archivos),
        "timestamp": datetime.now().isoformat(),
    }


@app.post("/api/refresh")
def refresh(x_refresh_token: str = Header(None)):
    """
    Dispara el ciclo de scraping manualmente.
    Normalmente lo ejecuta GitHub Actions — este endpoint es solo para emergencias.
    """
    if x_refresh_token != REFRESH_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")
    global _df_cache
    _df_cache = None  # invalidar caché
    try:
        from diario import (extraer_bora_licitaciones, extraer_bora_adjudicaciones,
                            extraer_comprar, extraer_pagos_tgn, cruzar_fuentes, guardar_excels)
        df_bora    = extraer_bora_licitaciones()
        df_adj     = extraer_bora_adjudicaciones(df_bora)
        df_licit   = (df_bora[df_bora["es_adjudicacion"] == False].copy().reset_index(drop=True)
                      if not df_bora.empty else pd.DataFrame())
        df_comprar = extraer_comprar()
        df_tgn     = extraer_pagos_tgn()
        df_cruce   = cruzar_fuentes(df_adj, df_comprar, df_tgn)
        guardar_excels(df_cruce, df_adj, df_licit, df_comprar, df_tgn)
        _df_cache = df_cruce
        return {
            "status": "ok",
            "mensaje": "Ciclo completado",
            "timestamp": datetime.now().isoformat(),
            "registros": len(df_cruce)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/resumen")
def resumen():
    """KPIs ejecutivos del último reporte."""
    df = cargar_ultimo_reporte()
    if df.empty:
        return {"total": 0, "alto_riesgo": 0, "mensaje": "Sin datos"}
    alto_riesgo = 0
    if "riesgo" in df.columns:
        alto_riesgo = int((df["riesgo"].astype(str).str.upper() == "ALTO").sum())
    elif "nivel_alerta" in df.columns:
        alto_riesgo = int((df["nivel_alerta"].astype(str).str.upper() == "ALTO").sum())
    return {
        "total": len(df),
        "alto_riesgo": alto_riesgo,
        "columnas": list(df.columns[:10]),
        "timestamp": datetime.now().isoformat(),
    }
