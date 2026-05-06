"""
main.py — Monitor de Fenómenos Corruptivos v2
FastAPI + Jinja2 + Plotly.js (sin Streamlit)

Secciones:
  GET  /                  → dashboard HTML (SPA)
  GET  /api/status        → estado del servicio
  GET  /api/contratos     → datos flujo (filtros: fecha_desde, fecha_hasta, organismo, riesgo, limit)
  GET  /api/organismos    → ranking organismos + perfil individual (?nombre=)
  GET  /api/proveedores   → ranking proveedores (cobro TGN / riesgo) + perfil (?cuit= / ?nombre=)
  GET  /api/monitor       → análisis de concentración (fragmentacion/unico/rafaga/hhi/fantasmas)
  GET  /api/stats         → estadísticas de acceso + donaciones (admin)
  POST /api/refresh       → dispara scraping manual (header X-Refresh-Token)
  POST /api/donacion      → registra consulta de donación
  GET  /api/chart/{tipo}  → JSON listo para Plotly (tipos: alertas, evolucion, hhi, riesgo_scatter)
"""

import os
import re
import glob
import logging
import unicodedata
from contextlib import asynccontextmanager
from datetime import datetime

import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor

from fastapi import FastAPI, HTTPException, Request, Header, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── módulos propios (no se modifican) ────────────────────────────────────────
from perfil_organismo import (
    cargar_historico as cargar_hist_org,
    buscar_organismo,
    parsear_monto as pm_org,
    calcular_hhi,
    interpretar_hhi,
)
from perfil_cuit import (
    cargar_historico as cargar_hist_cuit,
    buscar_por_cuit,
    buscar_por_nombre_listar,
    top_por_cobro_tgn,
    ranking_por_riesgo,
)
from analisis_concentracion import (
    cargar_historico as cargar_hist_conc,
    preparar_df,
    detectar_fragmentacion,
    detectar_proveedor_unico,
    detectar_rafaga,
    analisis_hhi,
    detectar_fantasmas,
)

logger = logging.getLogger(__name__)

# ─── BASE DE DATOS ────────────────────────────────────────────────────────────
_DATABASE_URL = os.getenv("DATABASE_URL", "")


def _get_conn():
    url = _DATABASE_URL.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url, cursor_factory=RealDictCursor)


def _init_db():
    if not _DATABASE_URL:
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS donaciones_consultas (
                    id SERIAL PRIMARY KEY,
                    nombre VARCHAR(100),
                    apellido VARCHAR(100),
                    email VARCHAR(254),
                    pais VARCHAR(30),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS estadisticas_acceso (
                    id SERIAL PRIMARY KEY,
                    seccion VARCHAR(100) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_ea_ts ON estadisticas_acceso (created_at);
                CREATE INDEX IF NOT EXISTS idx_dc_ts ON donaciones_consultas (created_at);
            """)
            conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"DB init error: {e}")


def _registrar_visita(seccion: str):
    if not _DATABASE_URL:
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO estadisticas_acceso (seccion) VALUES (%s)", (seccion,)
            )
            conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"tracking error: {e}")


def _get_stats():
    if not _DATABASE_URL:
        return None
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM estadisticas_acceso;")
            total = cur.fetchone()["total"]
            cur.execute(
                "SELECT seccion, COUNT(*) AS v FROM estadisticas_acceso GROUP BY seccion ORDER BY v DESC;"
            )
            por_seccion = cur.fetchall()
            cur.execute(
                "SELECT DATE(created_at AT TIME ZONE 'America/Argentina/Buenos_Aires') AS dia, "
                "COUNT(*) AS v FROM estadisticas_acceso GROUP BY dia ORDER BY dia DESC LIMIT 14;"
            )
            por_dia = cur.fetchall()
            cur.execute(
                "SELECT id, nombre, apellido, email, pais, created_at "
                "FROM donaciones_consultas ORDER BY created_at DESC LIMIT 50;"
            )
            donaciones = cur.fetchall()
        conn.close()
        return {
            "total": total,
            "por_seccion": list(por_seccion),
            "por_dia": list(por_dia),
            "donaciones": list(donaciones),
        }
    except Exception as e:
        return {"error": str(e)}


# ─── DATOS ────────────────────────────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "dev")

_cache: dict = {}   # {"flujo": df, "ts": datetime}
CACHE_TTL = 3600    # segundos


def _cache_valido() -> bool:
    if "ts" not in _cache:
        return False
    return (datetime.now() - _cache["ts"]).seconds < CACHE_TTL


def _cargar_datos():
    """Carga todos los xlsx y devuelve (df_flujo, df_adj, df_tgn, n_archivos)."""
    if _cache_valido():
        return _cache["flujo"], _cache["adj"], _cache["tgn"], _cache["n"]

    patron = os.path.join(DATA_DIR, "**", "reporte_*.xlsx")
    archivos = sorted(glob.glob(patron, recursive=True))

    dfs_flujo, dfs_adj, dfs_tgn = [], [], []
    for archivo in archivos:
        try:
            xl = pd.ExcelFile(archivo, engine="openpyxl")
            for sheet in ["🚨 Flujo Completo", "🔗 Flujo Cruzado"]:
                if sheet in xl.sheet_names:
                    df = pd.read_excel(xl, sheet_name=sheet, engine="openpyxl")
                    df["_archivo"] = os.path.basename(archivo)
                    dfs_flujo.append(df)
                    break
            if "🏆 Adjudicaciones" in xl.sheet_names:
                dfs_adj.append(
                    pd.read_excel(xl, sheet_name="🏆 Adjudicaciones", engine="openpyxl")
                )
            if "💰 TGN" in xl.sheet_names:
                dfs_tgn.append(
                    pd.read_excel(xl, sheet_name="💰 TGN", engine="openpyxl")
                )
        except Exception:
            pass

    def concat(lst):
        return pd.concat(lst, ignore_index=True) if lst else pd.DataFrame()

    df_flujo = concat(dfs_flujo)
    df_adj   = concat(dfs_adj)
    df_tgn   = concat(dfs_tgn)

    for df in [df_flujo, df_adj]:
        for col in ["fecha", "fecha_extraccion", "fecha_publicacion"]:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")

    _cache.update({"flujo": df_flujo, "adj": df_adj, "tgn": df_tgn,
                   "n": len(archivos), "ts": datetime.now()})
    return df_flujo, df_adj, df_tgn, len(archivos)


# ─── HELPERS ──────────────────────────────────────────────────────────────────
def _parsear_monto(valor):
    if pd.isna(valor) or not str(valor).strip():
        return 0.0
    try:
        limpio = re.sub(r"[^\d,]", "", str(valor)).replace(",", ".")
        partes = limpio.split(".")
        if len(partes) > 2:
            limpio = "".join(partes[:-1]) + "." + partes[-1]
        return float(limpio)
    except Exception:
        return 0.0


def _col(df: pd.DataFrame, opciones: list):
    return next((c for c in opciones if c in df.columns), None)


def _df_to_records(df: pd.DataFrame, max_rows=500) -> list:
    """Convierte DataFrame a lista de dicts serializable."""
    df2 = df.head(max_rows).copy()
    for col in df2.select_dtypes(include=["datetime64[ns]", "datetime64[ns, UTC]"]).columns:
        df2[col] = df2[col].dt.strftime("%Y-%m-%d").fillna("")
    for col in df2.select_dtypes(include=["period"]).columns:
        df2[col] = df2[col].astype(str)
    return df2.fillna("").to_dict(orient="records")


# ─── LIFESPAN ─────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_db()
    os.makedirs(DATA_DIR, exist_ok=True)
    logger.info("Monitor XAI arrancando — FastAPI puro")
    yield
    logger.info("Monitor XAI apagando")


# ─── APP ──────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Monitor de Fenómenos Corruptivos — Ph.D. Monteverde",
    description="Algoritmos contra la Corrupción — FastAPI edition",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Archivos estáticos y templates
_BASE = os.path.dirname(os.path.abspath(__file__))
_STATIC = os.path.join(_BASE, "static")
_TEMPLATES = os.path.join(_BASE, "templates")
os.makedirs(_STATIC, exist_ok=True)
os.makedirs(_TEMPLATES, exist_ok=True)

app.mount("/static", StaticFiles(directory=_STATIC), name="static")
templates = Jinja2Templates(directory=_TEMPLATES)


# ─── FRONTEND ─────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    _registrar_visita("home")
    return templates.TemplateResponse(request=request, name="index.html")


# ─── API: STATUS ──────────────────────────────────────────────────────────────
@app.get("/api/status")
def api_status():
    archivos = sorted(
        glob.glob(os.path.join(DATA_DIR, "**", "reporte_*.xlsx"), recursive=True)
    )
    ultimo = os.path.basename(archivos[0]) if archivos else None
    total = 0
    if archivos:
        try:
            xl = pd.ExcelFile(archivos[0], engine="openpyxl")
            sheet = next(
                (s for s in ["🚨 Flujo Completo", "🔗 Flujo Cruzado"] if s in xl.sheet_names),
                xl.sheet_names[0],
            )
            total = len(xl.parse(sheet))
        except Exception:
            pass
    return {
        "servicio": "Monitor XAI v2 — FastAPI",
        "version": "2.0.0",
        "status": "activo",
        "ultimo_reporte": ultimo,
        "total_contratos": total,
        "reportes_en_disco": len(archivos),
        "timestamp": datetime.now().isoformat(),
    }


# ─── API: CONTRATOS ───────────────────────────────────────────────────────────
@app.get("/api/contratos")
def api_contratos(
    fecha_desde: str = Query(None),
    fecha_hasta: str = Query(None),
    organismo: str = Query(None),
    riesgo: str = Query(None),
    limit: int = Query(500, ge=1, le=5000),
):
    _registrar_visita("contratos")
    df_flujo, _, df_tgn, n = _cargar_datos()

    if df_flujo.empty:
        return {"total": 0, "contratos": [], "metricas": {}}

    df = df_flujo.copy()
    col_fecha = _col(df, ["fecha", "fecha_publicacion", "fecha_extraccion"])
    col_org   = _col(df, ["organismo_contratante", "organismo"])
    col_monto = _col(df, ["monto_adjudicado_bora", "monto_adjudicado"])
    col_tgn   = _col(df, ["monto_cobrado_tgn"])

    df["_monto"] = df[col_monto].apply(_parsear_monto) if col_monto else 0.0
    df["_tgn"]   = df[col_tgn].apply(_parsear_monto) if col_tgn else 0.0

    # Filtros
    if fecha_desde and col_fecha:
        df = df[df[col_fecha] >= pd.to_datetime(fecha_desde, errors="coerce")]
    if fecha_hasta and col_fecha:
        df = df[df[col_fecha] <= pd.to_datetime(fecha_hasta, errors="coerce")]
    if organismo and col_org:
        df = df[df[col_org].str.contains(organismo, case=False, na=False)]
    if riesgo and "nivel_riesgo_licit" in df.columns:
        df = df[df["nivel_riesgo_licit"] == riesgo]

    monto_total   = df["_monto"].sum()
    monto_cobrado = df["_tgn"].sum()
    alto_riesgo   = int((df.get("nivel_riesgo_licit", pd.Series(dtype=str)) == "Alto").sum())
    flujos_completos = int(
        df["alerta"].str.contains("FLUJO COMPLETO", na=False).sum()
        if "alerta" in df.columns else 0
    )

    cols_tabla = [c for c in [
        col_fecha, col_org, "cuit_proveedor", "proveedor_adjudicado",
        col_monto, "cobro_en_tgn", col_tgn,
        "nivel_riesgo_licit", "indicadores_riesgo", "alerta", "link_bora",
    ] if c and c in df.columns]

    return {
        "total": len(df),
        "metricas": {
            "monto_total_ars": round(monto_total, 2),
            "monto_cobrado_ars": round(monto_cobrado, 2),
            "alto_riesgo": alto_riesgo,
            "flujos_completos": flujos_completos,
        },
        "contratos": _df_to_records(df[cols_tabla] if cols_tabla else df, limit),
    }


# ─── API: ORGANISMOS ──────────────────────────────────────────────────────────
@app.get("/api/organismos")
def api_organismos(nombre: str = Query(None), top: int = Query(15, ge=1, le=100)):
    _registrar_visita("organismos")
    df_flujo, _, _, _ = _cargar_datos()

    if df_flujo.empty:
        return {"ranking": [], "perfil": None}

    df = df_flujo.copy()
    col_org   = _col(df, ["organismo_contratante", "organismo"])
    col_monto = _col(df, ["monto_adjudicado_bora", "monto_adjudicado"])
    col_cuit  = _col(df, ["cuit_proveedor", "cuit"])
    col_tgn   = _col(df, ["monto_cobrado_tgn"])

    df["_monto"] = df[col_monto].apply(_parsear_monto) if col_monto else 0.0
    df["_tgn"]   = df[col_tgn].apply(_parsear_monto) if col_tgn else 0.0

    # Ranking global
    grp_agg = {"_monto": ["count", "sum"]}
    if col_cuit:
        grp_agg[col_cuit] = "nunique"
    if col_tgn:
        grp_agg["_tgn"] = "sum"

    grp = df.groupby(col_org).agg(**{
        "adjudicaciones": pd.NamedAgg(column="_monto", aggfunc="count"),
        "monto_total":    pd.NamedAgg(column="_monto", aggfunc="sum"),
        **({"cuits_distintos": pd.NamedAgg(column=col_cuit, aggfunc="nunique")} if col_cuit else {}),
        **({"monto_cobrado":  pd.NamedAgg(column="_tgn", aggfunc="sum")} if col_tgn else {}),
    }).reset_index().sort_values("monto_total", ascending=False)

    ranking = _df_to_records(grp.head(top), top)

    # Perfil individual
    perfil = None
    if nombre:
        df_org, nombre_can = buscar_organismo(df, nombre)
        if not df_org.empty:
            df_org["_monto"] = df_org[col_monto].apply(_parsear_monto) if col_monto else 0.0
            df_org["_tgn"]   = df_org[col_tgn].apply(_parsear_monto) if col_tgn else 0.0

            hhi_val = 0.0
            hhi_txt = ""
            if col_cuit:
                por_cuit = df_org.groupby(col_cuit)["_monto"].sum()
                hhi_val = calcular_hhi(por_cuit)
                hhi_txt = interpretar_hhi(hhi_val)

            # Evolución mensual
            col_fecha = _col(df_org, ["fecha", "fecha_publicacion", "fecha_extraccion"])
            evolucion = []
            if col_fecha:
                df_org["_mes"] = df_org[col_fecha].dt.to_period("M").astype(str)
                evolucion = _df_to_records(
                    df_org.groupby("_mes").agg(
                        contratos=("_monto", "count"),
                        monto=("_monto", "sum"),
                    ).reset_index()
                )

            # Proveedores
            proveedores = []
            if col_cuit:
                col_prov = _col(df_org, ["proveedor_adjudicado", "proveedor_nombre"])
                grp_p = df_org.groupby(col_cuit).agg(
                    contratos=("_monto", "count"),
                    monto=("_monto", "sum"),
                    **({"nombre": pd.NamedAgg(column=col_prov, aggfunc="first")} if col_prov else {}),
                ).reset_index().sort_values("contratos", ascending=False)
                proveedores = _df_to_records(grp_p.head(20))

            # Red flags
            red_flags = []
            if "indicadores_riesgo" in df_org.columns:
                flags = df_org["indicadores_riesgo"].dropna()
                flags = flags[flags != "✅ Sin alertas"]
                todos = []
                for f in flags:
                    todos.extend([x.strip() for x in str(f).split("|")])
                if todos:
                    red_flags = (
                        pd.Series(todos)
                        .value_counts()
                        .reset_index()
                        .rename(columns={"index": "flag", 0: "frecuencia"})
                        .to_dict(orient="records")
                    )

            perfil = {
                "nombre": nombre_can,
                "total": len(df_org),
                "monto_total": round(df_org["_monto"].sum(), 2),
                "monto_cobrado": round(df_org["_tgn"].sum(), 2),
                "hhi": hhi_val,
                "hhi_interpretacion": hhi_txt,
                "evolucion": evolucion,
                "proveedores": proveedores,
                "red_flags": red_flags,
            }

    return {"ranking": ranking, "perfil": perfil}


# ─── API: PROVEEDORES ─────────────────────────────────────────────────────────
@app.get("/api/proveedores")
def api_proveedores(
    cuit: str = Query(None),
    nombre: str = Query(None),
    top: int = Query(20, ge=1, le=100),
    modo: str = Query("cobro", pattern="^(cobro|riesgo)$"),
):
    _registrar_visita("proveedores")
    df_flujo, df_adj, df_tgn, _ = _cargar_datos()

    if df_flujo.empty:
        return {"ranking": [], "perfil": None}

    df = df_flujo.copy()
    col_cuit  = _col(df, ["cuit_proveedor", "cuit"])
    col_prov  = _col(df, ["proveedor_adjudicado", "proveedor_nombre"])
    col_monto = _col(df, ["monto_adjudicado_bora", "monto_adjudicado"])
    col_tgn   = _col(df, ["monto_cobrado_tgn"])

    df["_monto"] = df[col_monto].apply(_parsear_monto) if col_monto else 0.0
    df["_tgn"]   = df[col_tgn].apply(_parsear_monto) if col_tgn else 0.0

    # Ranking
    ranking = []
    if col_cuit:
        if modo == "cobro":
            df_c = df[df["_tgn"] > 0]
            grp = df_c.groupby(col_cuit).agg(
                monto_tgn=("_tgn", "sum"),
                monto_adj=("_monto", "sum"),
                contratos=("_monto", "count"),
                **({"nombre": pd.NamedAgg(column=col_prov, aggfunc="first")} if col_prov else {}),
            ).reset_index().sort_values("monto_tgn", ascending=False)
        else:
            col_score = "indice_riesgo_licit"
            if col_score in df.columns:
                grp = df[df[col_cuit].astype(str).str.strip() != ""].groupby(col_cuit).agg(
                    score_promedio=(col_score, "mean"),
                    score_maximo=(col_score, "max"),
                    contratos=(col_score, "count"),
                    **({"nombre": pd.NamedAgg(column=col_prov, aggfunc="first")} if col_prov else {}),
                ).reset_index().sort_values("score_promedio", ascending=False)
            else:
                grp = pd.DataFrame()
        ranking = _df_to_records(grp.head(top)) if not grp.empty else []

    # Perfil individual
    perfil = None
    if cuit and col_cuit:
        df_prov, nombre_can = buscar_por_cuit(df, cuit)
        if df_prov.empty and not df_adj.empty:
            df_prov, nombre_can = buscar_por_cuit(df_adj, cuit)
        if not df_prov.empty:
            df_prov["_monto"] = df_prov[col_monto].apply(_parsear_monto) if col_monto else 0.0
            df_prov["_tgn"]   = df_prov[col_tgn].apply(_parsear_monto) if col_tgn else 0.0

            col_org   = _col(df_prov, ["organismo_contratante", "organismo"])
            col_fecha = _col(df_prov, ["fecha", "fecha_publicacion"])

            evolucion = []
            if col_fecha:
                df_prov["_mes"] = df_prov[col_fecha].dt.to_period("M").astype(str)
                evolucion = _df_to_records(
                    df_prov.groupby("_mes").agg(
                        contratos=("_monto", "count"),
                        monto_adj=("_monto", "sum"),
                        monto_tgn=("_tgn", "sum"),
                    ).reset_index()
                )

            por_organismo = []
            if col_org:
                por_organismo = _df_to_records(
                    df_prov.groupby(col_org).agg(
                        contratos=("_monto", "count"),
                        monto_adj=("_monto", "sum"),
                        monto_tgn=("_tgn", "sum"),
                    ).reset_index().sort_values("contratos", ascending=False).head(15)
                )

            red_flags = []
            if "indicadores_riesgo" in df_prov.columns:
                flags = df_prov["indicadores_riesgo"].dropna()
                flags = flags[flags != "✅ Sin alertas"]
                todos = []
                for f in flags:
                    todos.extend([x.strip() for x in str(f).split("|")])
                if todos:
                    red_flags = (
                        pd.Series(todos)
                        .value_counts()
                        .reset_index()
                        .rename(columns={"index": "flag", 0: "frecuencia"})
                        .to_dict(orient="records")
                    )

            monto_adj = df_prov["_monto"].sum()
            monto_tgn = df_prov["_tgn"].sum()
            perfil = {
                "cuit": cuit,
                "nombre": nombre_can,
                "total_contratos": len(df_prov),
                "monto_adj": round(monto_adj, 2),
                "monto_tgn": round(monto_tgn, 2),
                "ratio_cobro_pct": round(monto_tgn / monto_adj * 100, 1) if monto_adj else 0,
                "organismos": df_prov[col_org].nunique() if col_org else 0,
                "evolucion": evolucion,
                "por_organismo": por_organismo,
                "red_flags": red_flags,
                "detalle": _df_to_records(df_prov, 200),
            }

    elif nombre and col_prov:
        # Búsqueda por nombre: devuelve lista de CUIT/nombre coincidentes
        norm = nombre.upper()
        norm = unicodedata.normalize("NFD", norm)
        norm = "".join(c for c in norm if unicodedata.category(c) != "Mn")
        mask = df[col_prov].apply(
            lambda x: norm in unicodedata.normalize("NFD", str(x).upper())
        )
        df_m = df[mask]
        if not df_m.empty and col_cuit:
            candidatos = (
                df_m.groupby(col_cuit)
                .agg(
                    nombre_can=(col_prov, "first"),
                    contratos=("_monto", "count"),
                )
                .reset_index()
                .sort_values("contratos", ascending=False)
                .head(20)
            )
            perfil = {"candidatos": _df_to_records(candidatos)}

    return {"ranking": ranking, "perfil": perfil}


# ─── API: MONITOR ─────────────────────────────────────────────────────────────
@app.get("/api/monitor")
def api_monitor(analisis: str = Query("todos")):
    """
    analisis: todos | fragmentacion | unico | rafaga | hhi | fantasmas
    """
    _registrar_visita("monitor")
    df_flujo, _, _, _ = _cargar_datos()

    if df_flujo.empty:
        return {"error": "Sin datos. Ejecutá diario.py primero."}

    result = {}
    run_all = analisis == "todos"

    def _safe_run(fn, *args, **kwargs):
        try:
            df = fn(*args, **kwargs)
            return _df_to_records(df) if isinstance(df, pd.DataFrame) and not df.empty else []
        except Exception as e:
            logger.warning(f"{fn.__name__} error: {e}")
            return []

    if run_all or analisis == "fragmentacion":
        result["fragmentacion"] = _safe_run(detectar_fragmentacion, df_flujo, exportar_df=True)

    if run_all or analisis == "unico":
        result["proveedor_unico"] = _safe_run(detectar_proveedor_unico, df_flujo, exportar_df=True)

    if run_all or analisis == "rafaga":
        result["rafagas"] = _safe_run(detectar_rafaga, df_flujo, exportar_df=True)

    if run_all or analisis == "hhi":
        result["hhi"] = _safe_run(analisis_hhi, df_flujo, top_n=30, exportar_df=True)

    if run_all or analisis == "fantasmas":
        result["fantasmas"] = _safe_run(detectar_fantasmas, df_flujo, exportar_df=True)

    return result


# ─── API: CHARTS (JSON listo para Plotly.js) ──────────────────────────────────
@app.get("/api/chart/{tipo}")
def api_chart(tipo: str):
    """
    Devuelve data + layout para Plotly.js
    tipos: alertas | evolucion | hhi | riesgo_scatter
    """
    df_flujo, _, _, _ = _cargar_datos()

    if df_flujo.empty:
        return {"data": [], "layout": {}}

    df = df_flujo.copy()
    col_fecha = _col(df, ["fecha", "fecha_publicacion", "fecha_extraccion"])
    col_monto = _col(df, ["monto_adjudicado_bora", "monto_adjudicado"])
    col_org   = _col(df, ["organismo_contratante", "organismo"])
    df["_monto"] = df[col_monto].apply(_parsear_monto) if col_monto else 0.0

    if tipo == "alertas":
        if "alerta" not in df.columns:
            return {"data": [], "layout": {}}
        conteo = df["alerta"].value_counts().reset_index()
        conteo.columns = ["alerta", "cantidad"]
        return {
            "data": [{
                "type": "bar",
                "orientation": "h",
                "x": conteo["cantidad"].tolist(),
                "y": conteo["alerta"].tolist(),
                "marker": {"color": "#7c3aed"},
            }],
            "layout": {
                "title": "Distribución por tipo de flujo",
                "xaxis": {"title": "Contratos"},
                "yaxis": {"automargin": True},
                "margin": {"l": 220},
                "height": max(300, len(conteo) * 36),
                "paper_bgcolor": "rgba(0,0,0,0)",
                "plot_bgcolor": "rgba(0,0,0,0)",
                "font": {"color": "#1e1b4b"},
            },
        }

    elif tipo == "evolucion":
        if not col_fecha:
            return {"data": [], "layout": {}}
        df["_mes"] = df[col_fecha].dt.to_period("M").astype(str)
        evol = df.groupby("_mes").agg(contratos=("_monto", "count"), monto=("_monto", "sum")).reset_index()
        return {
            "data": [
                {
                    "type": "bar",
                    "name": "Contratos",
                    "x": evol["_mes"].tolist(),
                    "y": evol["contratos"].tolist(),
                    "marker": {"color": "#8b5cf6"},
                },
                {
                    "type": "scatter",
                    "name": "Monto ARS",
                    "x": evol["_mes"].tolist(),
                    "y": evol["monto"].tolist(),
                    "yaxis": "y2",
                    "line": {"color": "#d97706"},
                },
            ],
            "layout": {
                "title": "Evolución mensual",
                "xaxis": {"title": "Mes"},
                "yaxis": {"title": "Contratos"},
                "yaxis2": {"title": "Monto ARS", "overlaying": "y", "side": "right"},
                "paper_bgcolor": "rgba(0,0,0,0)",
                "plot_bgcolor": "rgba(0,0,0,0)",
                "font": {"color": "#1e1b4b"},
                "legend": {"orientation": "h"},
            },
        }

    elif tipo == "hhi":
        df_h = df.copy()
        if not col_org:
            return {"data": [], "layout": {}}
        df_h["_cuit"] = df_h[_col(df_h, ["cuit_proveedor", "cuit"])].astype(str) if _col(df_h, ["cuit_proveedor", "cuit"]) else ""
        resultados = []
        for org, grp in df_h[df_h["_monto"] > 0].groupby(col_org):
            total = grp["_monto"].sum()
            if total == 0 or len(grp) < 2:
                continue
            grp_c = grp[grp["_cuit"].str.strip() != ""]
            if grp_c.empty:
                continue
            por_c = grp_c.groupby("_cuit")["_monto"].sum()
            hhi = round(((por_c / total) ** 2).sum() * 10_000, 1)
            resultados.append({"org": str(org)[:50], "hhi": hhi})
        if not resultados:
            return {"data": [], "layout": {}}
        df_r = pd.DataFrame(resultados).sort_values("hhi", ascending=False).head(20)
        colors = ["#dc2626" if h > 2500 else "#d97706" if h > 1500 else "#059669" for h in df_r["hhi"]]
        return {
            "data": [{
                "type": "bar",
                "orientation": "h",
                "x": df_r["hhi"].tolist(),
                "y": df_r["org"].tolist(),
                "marker": {"color": colors},
            }],
            "layout": {
                "title": "Top 20 organismos por HHI",
                "xaxis": {"title": "HHI"},
                "yaxis": {"automargin": True},
                "margin": {"l": 300},
                "height": 600,
                "shapes": [
                    {"type": "line", "x0": 2500, "x1": 2500, "y0": 0, "y1": 1,
                     "yref": "paper", "line": {"color": "#dc2626", "dash": "dash"}},
                    {"type": "line", "x0": 1500, "x1": 1500, "y0": 0, "y1": 1,
                     "yref": "paper", "line": {"color": "#d97706", "dash": "dash"}},
                ],
                "paper_bgcolor": "rgba(0,0,0,0)",
                "plot_bgcolor": "rgba(0,0,0,0)",
                "font": {"color": "#1e1b4b"},
            },
        }

    elif tipo == "riesgo_scatter":
        col_score = "indice_riesgo_licit"
        col_cuit  = _col(df, ["cuit_proveedor", "cuit"])
        col_prov  = _col(df, ["proveedor_adjudicado", "proveedor_nombre"])
        if col_score not in df.columns or not col_cuit:
            return {"data": [], "layout": {}}
        grp = df.groupby(col_cuit).agg(
            score=("indice_riesgo_licit", "mean"),
            monto=("_monto", "sum"),
            contratos=("_monto", "count"),
            **({"nombre": pd.NamedAgg(column=col_prov, aggfunc="first")} if col_prov else {}),
        ).reset_index().dropna(subset=["score"]).head(100)
        colors = ["#dc2626" if s >= 7 else "#d97706" if s >= 4 else "#059669" for s in grp["score"]]
        texto = grp["nombre"].astype(str).str[:30].tolist() if "nombre" in grp.columns else grp[col_cuit].tolist()
        return {
            "data": [{
                "type": "scatter",
                "mode": "markers",
                "x": grp["score"].tolist(),
                "y": grp["contratos"].tolist(),
                "text": texto,
                "marker": {
                    "color": colors,
                    "size": [max(6, min(30, m / 1e6)) for m in grp["monto"]],
                    "opacity": 0.8,
                },
                "hovertemplate": "<b>%{text}</b><br>Score: %{x:.2f}<br>Contratos: %{y}<extra></extra>",
            }],
            "layout": {
                "title": "Scatter: score de riesgo vs cantidad de contratos",
                "xaxis": {"title": "Score de riesgo promedio (0-10)"},
                "yaxis": {"title": "Cantidad de contratos"},
                "paper_bgcolor": "rgba(0,0,0,0)",
                "plot_bgcolor": "rgba(0,0,0,0)",
                "font": {"color": "#1e1b4b"},
            },
        }

    raise HTTPException(status_code=404, detail=f"Tipo de chart desconocido: {tipo}")


# ─── API: STATS (admin) ───────────────────────────────────────────────────────
@app.get("/api/stats")
def api_stats(x_admin_token: str = Header(None)):
    token = os.getenv("ADMIN_TOKEN", "admin")
    if x_admin_token != token:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    data = _get_stats()
    if data is None:
        return {"error": "Base de datos no configurada"}
    return data


# ─── API: DONACION ────────────────────────────────────────────────────────────
class DonacionIn(BaseModel):
    nombre: str
    apellido: str
    email: str
    pais: str = "Argentina"


@app.post("/api/donacion")
def api_donacion(payload: DonacionIn):
    if not _DATABASE_URL:
        return {"ok": False, "mensaje": "DB no configurada"}
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO donaciones_consultas (nombre, apellido, email, pais) "
                "VALUES (%s,%s,%s,%s) RETURNING id",
                (payload.nombre.strip(), payload.apellido.strip(),
                 payload.email.strip(), payload.pais),
            )
            row = cur.fetchone()
            conn.commit()
        conn.close()
        return {"ok": True, "id": row["id"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── API: REFRESH MANUAL ──────────────────────────────────────────────────────
@app.post("/api/refresh")
def api_refresh(x_refresh_token: str = Header(None)):
    if x_refresh_token != REFRESH_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")

    _cache.clear()   # invalidar caché

    try:
        from diario import (
            extraer_bora_licitaciones,
            extraer_bora_adjudicaciones,
            extraer_comprar,
            extraer_pagos_tgn,
            cruzar_fuentes,
            guardar_excels,
        )
        df_bora  = extraer_bora_licitaciones()
        df_adj   = extraer_bora_adjudicaciones(df_bora)
        df_licit = (
            df_bora[df_bora["es_adjudicacion"] == False].copy().reset_index(drop=True)
            if not df_bora.empty else pd.DataFrame()
        )
        df_comprar = extraer_comprar()
        df_tgn     = extraer_pagos_tgn()
        df_cruce   = cruzar_fuentes(df_adj, df_comprar, df_tgn)
        guardar_excels(df_cruce, df_adj, df_licit, df_comprar, df_tgn)
        return {
            "status": "ok",
            "mensaje": "Ciclo completado",
            "timestamp": datetime.now().isoformat(),
            "registros": len(df_cruce),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── RESUMEN (retrocompat) ────────────────────────────────────────────────────
@app.get("/resumen")
def resumen():
    df_flujo, _, _, _ = _cargar_datos()
    if df_flujo.empty:
        return {"total": 0, "alto_riesgo": 0}
    alto = 0
    for col in ["riesgo", "nivel_alerta", "nivel_riesgo_licit"]:
        if col in df_flujo.columns:
            alto = int((df_flujo[col].astype(str).str.upper() == "ALTO").sum())
            break
    return {
        "total": len(df_flujo),
        "alto_riesgo": alto,
        "timestamp": datetime.now().isoformat(),
    }
