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
from fastapi.responses import HTMLResponse as _HTMLResponse
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

# Archivos estáticos
_BASE = os.path.dirname(os.path.abspath(__file__))
_STATIC = os.path.join(_BASE, "static")
os.makedirs(_STATIC, exist_ok=True)

try:
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")
except Exception:
    pass


# ─── FRONTEND ─────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    _registrar_visita("home")
    return _HTML_CONTENT

_HTML_CONTENT = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Monitor de Fenómenos Corruptivos</title>
<script src="https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.27.0/plotly.min.js"></script>
<style>
  :root {
    --bg: #f5f3ff; --card: #fff; --border: #ddd6fe; --accent: #7c3aed;
    --accent2: #8b5cf6; --pale: #ede9fe; --red: #dc2626; --amber: #d97706;
    --green: #059669; --text: #1e1b4b; --muted: #4c1d95; --shadow: 0 2px 12px rgba(124,58,237,.10);
    --sidebar: #4c1d95;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Inter', system-ui, sans-serif; background: var(--bg); color: var(--text); display: flex; min-height: 100vh; }
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

  /* ── Sidebar ── */
  #sidebar {
    width: 240px; min-width: 240px; background: linear-gradient(180deg,#4c1d95,#5b21b6 60%,#6d28d9);
    padding: 20px 0; display: flex; flex-direction: column; position: sticky; top: 0; height: 100vh; overflow-y: auto;
  }
  #sidebar .brand { padding: 0 20px 16px; border-bottom: 1px solid rgba(167,139,250,.3); }
  #sidebar .brand h1 { color: #e2e8f0; font-size: 15px; font-weight: 800; line-height: 1.3; }
  #sidebar .brand p  { color: #c4b5fd; font-size: 10px; margin-top: 3px; }
  #sidebar nav { padding: 12px 0; flex: 1; }
  #sidebar nav a {
    display: block; padding: 10px 20px; color: #c4b5fd; font-size: 13px; font-weight: 500;
    text-decoration: none; border-left: 3px solid transparent; transition: all .15s;
  }
  #sidebar nav a:hover, #sidebar nav a.active {
    color: #fff; background: rgba(255,255,255,.08); border-left-color: #a78bfa;
  }
  #sidebar .sidebar-footer { padding: 16px 20px; border-top: 1px solid rgba(167,139,250,.3); }
  #sidebar .sidebar-footer p { color: #94a3b8; font-size: 10px; line-height: 1.6; }
  #btn-apoyar {
    display: block; width: 100%; padding: 9px; background: linear-gradient(135deg,#f59e0b,#d97706);
    color: #fff; font-weight: 700; font-size: 12px; border: none; border-radius: 8px;
    cursor: pointer; margin-bottom: 12px; text-align: center;
  }
  #btn-apoyar:hover { filter: brightness(1.1); }

  /* ── Main ── */
  #main { flex: 1; overflow-y: auto; padding: 28px 32px; }
  .section { display: none; }
  .section.active { display: block; }
  h2.section-title { font-size: 22px; font-weight: 800; color: var(--text); margin-bottom: 4px; }
  p.section-sub { color: var(--muted); font-size: 13px; margin-bottom: 20px; }

  /* ── Cards ── */
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; box-shadow: var(--shadow); margin-bottom: 20px; }
  .metrics-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px,1fr)); gap: 14px; margin-bottom: 20px; }
  .metric-card {
    background: var(--card); border: 1px solid var(--border); border-left: 4px solid var(--accent);
    border-radius: 12px; padding: 16px 18px; box-shadow: var(--shadow);
  }
  .metric-card.red   { border-left-color: var(--red); }
  .metric-card.green { border-left-color: var(--green); }
  .metric-card.amber { border-left-color: var(--amber); }
  .metric-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; font-weight: 600; margin-bottom: 4px; }
  .metric-value { font-size: 24px; font-weight: 800; color: var(--text); }
  .metric-sub   { font-size: 11px; color: var(--accent); margin-top: 2px; }

  /* ── Charts grid ── */
  .charts-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }
  @media (max-width: 900px) { .charts-row { grid-template-columns: 1fr; } }
  .chart-box { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; box-shadow: var(--shadow); min-height: 300px; }

  /* ── Filtros ── */
  .filters { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
  .filters input, .filters select {
    border: 1px solid var(--border); border-radius: 8px; padding: 7px 12px; font-size: 13px;
    background: var(--card); color: var(--text); outline: none;
  }
  .filters input:focus, .filters select:focus { border-color: var(--accent2); }
  .btn {
    background: var(--accent); color: #fff; border: none; border-radius: 8px;
    padding: 8px 16px; font-size: 13px; font-weight: 600; cursor: pointer;
  }
  .btn:hover { background: var(--accent2); }
  .btn.outline { background: transparent; color: var(--accent); border: 1px solid var(--accent); }
  .btn.outline:hover { background: var(--pale); }

  /* ── Tabla ── */
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  thead th { background: var(--pale); color: var(--muted); padding: 8px 10px; text-align: left; font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: .04em; position: sticky; top: 0; }
  tbody tr:nth-child(even) { background: #faf5ff; }
  tbody tr:hover { background: var(--pale); }
  td { padding: 7px 10px; border-bottom: 1px solid #f3f0ff; color: var(--text); }
  td a { color: var(--accent); text-decoration: none; }
  td a:hover { text-decoration: underline; }

  /* ── Tags riesgo ── */
  .tag { display: inline-block; padding: 2px 8px; border-radius: 20px; font-size: 10px; font-weight: 600; }
  .tag-red    { background: #fee2e2; color: var(--red); }
  .tag-amber  { background: #fef3c7; color: var(--amber); }
  .tag-green  { background: #d1fae5; color: var(--green); }
  .tag-purple { background: var(--pale); color: var(--accent); }

  /* ── Diferencial ── */
  .diferencial { background: var(--pale); border-left: 4px solid var(--accent); padding: 10px 14px; border-radius: 0 8px 8px 0; font-size: 13px; margin-bottom: 16px; color: var(--text); }

  /* ── Modal ── */
  #modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.4); z-index: 1000; align-items: center; justify-content: center; }
  #modal-overlay.show { display: flex; }
  #modal-box { background: var(--card); border-radius: 16px; padding: 28px; max-width: 460px; width: 92%; box-shadow: 0 8px 40px rgba(0,0,0,.2); }
  #modal-box h3 { font-size: 18px; font-weight: 800; margin-bottom: 6px; }
  #modal-box p  { font-size: 13px; color: var(--muted); margin-bottom: 16px; }
  .form-row { display: flex; gap: 10px; }
  .form-row input, #modal-box input, #modal-box select {
    display: block; width: 100%; border: 1px solid var(--border); border-radius: 8px;
    padding: 9px 12px; font-size: 13px; color: var(--text); background: var(--bg);
    margin-bottom: 10px;
  }
  #modal-msg { font-size: 13px; color: var(--green); display: none; margin-top: 8px; }

  /* ── Tabs ── */
  .tabs { display: flex; gap: 6px; margin-bottom: 16px; flex-wrap: wrap; }
  .tab-btn {
    padding: 7px 16px; border: 1px solid var(--border); border-radius: 8px 8px 0 0;
    background: var(--card); color: var(--muted); font-size: 13px; font-weight: 500;
    cursor: pointer; border-bottom: none;
  }
  .tab-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); font-weight: 700; }
  .tab-content { display: none; }
  .tab-content.active { display: block; }

  /* ── Loading ── */
  .loading { text-align: center; padding: 40px; color: var(--muted); font-size: 13px; }
  .spinner { display: inline-block; width: 20px; height: 20px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin .6s linear infinite; margin-right: 8px; vertical-align: middle; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>

<!-- ── SIDEBAR ── -->
<aside id="sidebar">
  <div class="brand">
    <h1>⚖️ Monitor de Contratos</h1>
    <p>Ph.D. Monteverde · Algoritmos contra la Corrupción</p>
  </div>
  <nav>
    <a href="#" class="active" data-section="contratos">📄 Contratos</a>
    <a href="#" data-section="organismos">🏛️ Organismos</a>
    <a href="#" data-section="proveedores">🏢 Proveedores</a>
    <a href="#" data-section="monitor">🔍 Monitor</a>
    <a href="#" data-section="estadisticas">📊 Estadísticas</a>
  </nav>
  <div class="sidebar-footer">
    <button id="btn-apoyar">💛 Apoyar este proyecto</button>
    <p>Cruce exclusivo BORA → Comprar → TGN</p>
    <p style="margin-top:8px;">Ref: Monteverde, V. (2020). <em>Journal of Financial Crime</em>, Vol. 28 No. 2.</p>
  </div>
</aside>

<!-- ── MAIN ── -->
<main id="main">

  <!-- CONTRATOS -->
  <section id="sec-contratos" class="section active">
    <h2 class="section-title">📄 Contratos</h2>
    <p class="section-sub">Flujo diario: BORA → Comprar.gob.ar → TGN</p>

    <div id="metricas-contratos" class="metrics-row">
      <div class="metric-card"><div class="metric-label">Total registros</div><div class="metric-value" id="m-total">—</div></div>
      <div class="metric-card green"><div class="metric-label">Flujos BORA→TGN</div><div class="metric-value" id="m-flujos">—</div></div>
      <div class="metric-card amber"><div class="metric-label">Monto adjudicado</div><div class="metric-value" id="m-monto">—</div></div>
      <div class="metric-card amber"><div class="metric-label">Cobrado en TGN</div><div class="metric-value" id="m-cobrado">—</div></div>
      <div class="metric-card red"><div class="metric-label">🔴 Riesgo Alto</div><div class="metric-value" id="m-alto">—</div></div>
    </div>

    <div class="charts-row">
      <div class="chart-box" id="chart-alertas"><div class="loading"><span class="spinner"></span>Cargando gráfico…</div></div>
      <div class="chart-box" id="chart-evolucion"><div class="loading"><span class="spinner"></span>Cargando gráfico…</div></div>
    </div>

    <div class="card">
      <div class="filters">
        <input type="date" id="f-desde" placeholder="Desde">
        <input type="date" id="f-hasta" placeholder="Hasta">
        <input type="text" id="f-org" placeholder="Organismo…" style="width:200px">
        <select id="f-riesgo">
          <option value="">Todos los riesgos</option>
          <option value="Alto">Alto</option>
          <option value="Medio">Medio</option>
          <option value="Bajo">Bajo</option>
        </select>
        <button class="btn" onclick="cargarContratos()">Filtrar</button>
        <button class="btn outline" onclick="limpiarFiltros()">Limpiar</button>
      </div>
      <p id="contratos-count" style="font-size:12px;color:var(--muted);margin-bottom:10px;"></p>
      <div class="table-wrap">
        <table id="tabla-contratos">
          <thead><tr id="thead-contratos"></tr></thead>
          <tbody id="tbody-contratos"><tr><td colspan="10" class="loading"><span class="spinner"></span>Cargando…</td></tr></tbody>
        </table>
      </div>
    </div>
  </section>

  <!-- ORGANISMOS -->
  <section id="sec-organismos" class="section">
    <h2 class="section-title">🏛️ Organismos</h2>
    <p class="section-sub">Perfil por organismo contratante — concentración, proveedores, evolución</p>

    <div class="charts-row">
      <div class="chart-box" id="chart-org-monto"><div class="loading"><span class="spinner"></span>Cargando…</div></div>
      <div class="chart-box" id="chart-org-adj"><div class="loading"><span class="spinner"></span>Cargando…</div></div>
    </div>

    <div class="card">
      <h3 style="font-size:15px;font-weight:700;margin-bottom:12px;">🔎 Perfil individual</h3>
      <div class="filters">
        <input type="text" id="org-buscar" placeholder="Nombre del organismo…" style="width:280px">
        <button class="btn" onclick="buscarOrganismo()">Buscar</button>
      </div>
      <div id="perfil-organismo"></div>
    </div>

    <div class="card">
      <h3 style="font-size:15px;font-weight:700;margin-bottom:12px;">📋 Ranking general</h3>
      <div class="table-wrap">
        <table id="tabla-org">
          <thead><tr><th>Organismo</th><th>Adjudicaciones</th><th>Monto total ARS</th><th>CUITs distintos</th><th>Cobrado TGN</th></tr></thead>
          <tbody id="tbody-org"><tr><td colspan="5" class="loading"><span class="spinner"></span>Cargando…</td></tr></tbody>
        </table>
      </div>
    </div>
  </section>

  <!-- PROVEEDORES -->
  <section id="sec-proveedores" class="section">
    <h2 class="section-title">🏢 Proveedores</h2>
    <p class="section-sub">Quién cobró (TGN), no solo quién ganó — cruce exclusivo BORA→Comprar→TGN</p>
    <div class="diferencial">💡 <strong>Diferencial único:</strong> este sistema muestra quién <strong>cobró</strong> (TGN), no solo quién ganó la licitación.</div>

    <div class="tabs">
      <button class="tab-btn active" onclick="switchTab('tab-cobro',this)">💰 Ranking por cobro TGN</button>
      <button class="tab-btn" onclick="switchTab('tab-riesgo-prov',this)">🔴 Ranking por riesgo</button>
      <button class="tab-btn" onclick="switchTab('tab-perfil-prov',this)">🔎 Perfil individual</button>
    </div>

    <div id="tab-cobro" class="tab-content active">
      <div class="card">
        <div class="table-wrap">
          <table id="tabla-cobro">
            <thead><tr><th>CUIT</th><th>Nombre</th><th>Contratos</th><th>Monto Adj. ARS</th><th>Cobrado TGN ARS</th></tr></thead>
            <tbody id="tbody-cobro"><tr><td colspan="5" class="loading"><span class="spinner"></span>Cargando…</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <div id="tab-riesgo-prov" class="tab-content">
      <div class="card">
        <div class="chart-box" id="chart-riesgo-scatter" style="min-height:400px;"><div class="loading"><span class="spinner"></span>Cargando…</div></div>
        <div class="table-wrap" style="margin-top:16px;">
          <table id="tabla-riesgo-prov">
            <thead><tr><th>CUIT</th><th>Nombre</th><th>Contratos</th><th>Score prom.</th><th>Score máx.</th><th>Nivel</th></tr></thead>
            <tbody id="tbody-riesgo-prov"><tr><td colspan="6" class="loading"><span class="spinner"></span>Cargando…</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <div id="tab-perfil-prov" class="tab-content">
      <div class="card">
        <div class="filters">
          <input type="text" id="prov-buscar-cuit" placeholder="CUIT (30-12345678-9)…" style="width:200px">
          <input type="text" id="prov-buscar-nombre" placeholder="Nombre empresa…" style="width:200px">
          <button class="btn" onclick="buscarProveedor()">Buscar</button>
        </div>
        <div id="perfil-proveedor"></div>
      </div>
    </div>
  </section>

  <!-- MONITOR -->
  <section id="sec-monitor" class="section">
    <h2 class="section-title">🔍 Monitor de Concentración</h2>
    <p class="section-sub">Análisis sistémico de patrones — Teoría Monteverde (2020)</p>

    <div class="tabs">
      <button class="tab-btn active" onclick="switchTab('tab-frag',this)">🔪 Fragmentación</button>
      <button class="tab-btn" onclick="switchTab('tab-unico',this)">🔒 Proveedor Único</button>
      <button class="tab-btn" onclick="switchTab('tab-rafaga',this)">⚡ Ráfagas</button>
      <button class="tab-btn" onclick="switchTab('tab-hhi',this)">📐 HHI</button>
      <button class="tab-btn" onclick="switchTab('tab-fantasmas',this)">👻 Sin cobro TGN</button>
    </div>

    <div id="tab-frag" class="tab-content active">
      <div class="card">
        <p style="font-size:12px;color:var(--muted);margin-bottom:12px;">Organismos que dividen compras para evitar el umbral de licitación pública ($10M ARS)</p>
        <div id="tabla-frag-wrap" class="table-wrap"><div class="loading"><span class="spinner"></span>Cargando…</div></div>
      </div>
    </div>
    <div id="tab-unico" class="tab-content">
      <div class="card">
        <p style="font-size:12px;color:var(--muted);margin-bottom:12px;">Organismos que adjudican siempre (o casi) al mismo CUIT — señal de captura</p>
        <div id="tabla-unico-wrap" class="table-wrap"><div class="loading"><span class="spinner"></span>Cargando…</div></div>
      </div>
    </div>
    <div id="tab-rafaga" class="tab-content">
      <div class="card">
        <p style="font-size:12px;color:var(--muted);margin-bottom:12px;">Proveedores con 3+ adjudicaciones en 7 días — aceleración sospechosa</p>
        <div id="chart-rafaga" style="min-height:300px;"><div class="loading"><span class="spinner"></span>Cargando…</div></div>
        <div id="tabla-rafaga-wrap" class="table-wrap" style="margin-top:16px;"></div>
      </div>
    </div>
    <div id="tab-hhi" class="tab-content">
      <div class="card">
        <p style="font-size:12px;color:var(--muted);margin-bottom:12px;">HHI &gt; 2500: alta concentración | HHI &lt; 1500: mercado competitivo</p>
        <div id="chart-hhi" style="min-height:500px;"><div class="loading"><span class="spinner"></span>Cargando…</div></div>
        <div id="tabla-hhi-wrap" class="table-wrap" style="margin-top:16px;"></div>
      </div>
    </div>
    <div id="tab-fantasmas" class="tab-content">
      <div class="card">
        <p style="font-size:12px;color:var(--muted);margin-bottom:12px;">Proveedores que ganaron licitaciones pero no registran cobro en TGN</p>
        <div id="tabla-fantasmas-wrap" class="table-wrap"><div class="loading"><span class="spinner"></span>Cargando…</div></div>
      </div>
    </div>
  </section>

  <!-- ESTADÍSTICAS -->
  <section id="sec-estadisticas" class="section">
    <h2 class="section-title">📊 Estadísticas de acceso</h2>
    <p class="section-sub">Panel administrativo — requiere token</p>
    <div class="card">
      <div class="filters">
        <input type="password" id="admin-token" placeholder="Token de administrador">
        <button class="btn" onclick="cargarStats()">Ver stats</button>
      </div>
      <div id="stats-content"></div>
    </div>
  </section>

</main>

<!-- MODAL APOYAR -->
<div id="modal-overlay">
  <div id="modal-box">
    <h3>💛 Apoyar este proyecto</h3>
    <p>Dejá tus datos para recibir novedades y apoyar el Monitor de Fenómenos Corruptivos.</p>
    <div class="form-row">
      <input type="text" id="don-nombre" placeholder="Nombre">
      <input type="text" id="don-apellido" placeholder="Apellido">
    </div>
    <input type="email" id="don-email" placeholder="Email">
    <select id="don-pais">
      <option value="Argentina">Argentina</option>
      <option value="Uruguay">Uruguay</option>
      <option value="Chile">Chile</option>
      <option value="México">México</option>
      <option value="Otro">Otro</option>
    </select>
    <div style="display:flex;gap:10px;margin-top:4px;">
      <button class="btn" style="flex:1" onclick="enviarDonacion()">Enviar</button>
      <button class="btn outline" style="flex:1" onclick="cerrarModal()">Cancelar</button>
    </div>
    <p id="modal-msg">✅ ¡Gracias! Nos pondremos en contacto.</p>
  </div>
</div>

<script>
// ── Navegación ──────────────────────────────────────────────────────────────
const SECTIONS = ['contratos','organismos','proveedores','monitor','estadisticas'];
let _loaded = {};

document.querySelectorAll('#sidebar nav a').forEach(a => {
  a.addEventListener('click', e => {
    e.preventDefault();
    const s = a.dataset.section;
    document.querySelectorAll('#sidebar nav a').forEach(x => x.classList.remove('active'));
    a.classList.add('active');
    document.querySelectorAll('.section').forEach(x => x.classList.remove('active'));
    document.getElementById('sec-'+s).classList.add('active');
    if (!_loaded[s]) { _loaded[s] = true; loadSection(s); }
  });
});

function loadSection(s) {
  if (s === 'contratos')    { cargarContratos(); cargarChart('alertas','chart-alertas'); cargarChart('evolucion','chart-evolucion'); }
  if (s === 'organismos')   { cargarOrganismos(); }
  if (s === 'proveedores')  { cargarProveedores('cobro'); cargarChart('riesgo_scatter','chart-riesgo-scatter'); }
  if (s === 'monitor')      { cargarMonitor(); cargarChart('hhi','chart-hhi'); }
}

// ── Charts (Plotly) ─────────────────────────────────────────────────────────
async function cargarChart(tipo, elId) {
  const el = document.getElementById(elId);
  try {
    const r = await fetch('/api/chart/'+tipo);
    const d = await r.json();
    if (!d.data || !d.data.length) { el.innerHTML = '<p class="loading">Sin datos para graficar.</p>'; return; }
    const layout = Object.assign({ margin: {t:40,b:40,l:40,r:40}, paper_bgcolor:'rgba(0,0,0,0)', plot_bgcolor:'rgba(0,0,0,0)' }, d.layout);
    Plotly.newPlot(elId, d.data, layout, { responsive: true, displayModeBar: false });
  } catch(e) { el.innerHTML = '<p class="loading">Error cargando gráfico.</p>'; }
}

// ── Contratos ───────────────────────────────────────────────────────────────
async function cargarContratos() {
  const desde  = document.getElementById('f-desde').value;
  const hasta  = document.getElementById('f-hasta').value;
  const org    = document.getElementById('f-org').value;
  const riesgo = document.getElementById('f-riesgo').value;
  let url = '/api/contratos?limit=500';
  if (desde)  url += '&fecha_desde='+desde;
  if (hasta)  url += '&fecha_hasta='+hasta;
  if (org)    url += '&organismo='+encodeURIComponent(org);
  if (riesgo) url += '&riesgo='+encodeURIComponent(riesgo);

  document.getElementById('tbody-contratos').innerHTML = '<tr><td colspan="10" class="loading"><span class="spinner"></span>Cargando…</td></tr>';

  try {
    const r = await fetch(url);
    const d = await r.json();
    const m = d.metricas || {};
    document.getElementById('m-total').textContent   = (d.total||0).toLocaleString();
    document.getElementById('m-flujos').textContent  = (m.flujos_completos||0).toLocaleString();
    document.getElementById('m-monto').textContent   = '$'+(((m.monto_total_ars||0)/1e6).toFixed(1))+'M';
    document.getElementById('m-cobrado').textContent = '$'+(((m.monto_cobrado_ars||0)/1e6).toFixed(1))+'M';
    document.getElementById('m-alto').textContent    = (m.alto_riesgo||0).toLocaleString();
    document.getElementById('contratos-count').textContent = (d.total||0).toLocaleString()+' contratos con los filtros aplicados';
    renderTabla('tabla-contratos', d.contratos || []);
  } catch(e) {
    document.getElementById('tbody-contratos').innerHTML = '<tr><td colspan="10" style="color:red;padding:12px">Error cargando datos.</td></tr>';
  }
}

function limpiarFiltros() {
  ['f-desde','f-hasta','f-org'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('f-riesgo').value = '';
  cargarContratos();
}

// ── Organismos ──────────────────────────────────────────────────────────────
async function cargarOrganismos() {
  try {
    const r = await fetch('/api/organismos?top=20');
    const d = await r.json();
    const rows = d.ranking || [];

    // Charts
    const top15 = rows.slice(0,15);
    const colores_monto = top15.map(() => '#7c3aed');
    Plotly.newPlot('chart-org-monto', [{
      type:'bar', orientation:'h',
      x: top15.map(r=>r.monto_total||0),
      y: top15.map(r=>String(r.organismo_contratante||r.organismo||'').slice(0,40)),
      marker:{color:colores_monto}
    }], { title:'Top 15 por monto', xaxis:{title:'Monto ARS'}, yaxis:{automargin:true}, margin:{l:280}, height:420, paper_bgcolor:'rgba(0,0,0,0)', plot_bgcolor:'rgba(0,0,0,0)' }, {responsive:true,displayModeBar:false});

    Plotly.newPlot('chart-org-adj', [{
      type:'bar', orientation:'h',
      x: top15.map(r=>r.adjudicaciones||0),
      y: top15.map(r=>String(r.organismo_contratante||r.organismo||'').slice(0,40)),
      marker:{color:'#d97706'}
    }], { title:'Top 15 por adjudicaciones', xaxis:{title:'Cantidad'}, yaxis:{automargin:true}, margin:{l:280}, height:420, paper_bgcolor:'rgba(0,0,0,0)', plot_bgcolor:'rgba(0,0,0,0)' }, {responsive:true,displayModeBar:false});

    renderTabla('tabla-org', rows);
  } catch(e) {
    document.getElementById('tbody-org').innerHTML = '<tr><td colspan="5" style="color:red;padding:12px">Error cargando datos.</td></tr>';
  }
}

async function buscarOrganismo() {
  const nombre = document.getElementById('org-buscar').value.trim();
  if (!nombre) return;
  const el = document.getElementById('perfil-organismo');
  el.innerHTML = '<div class="loading"><span class="spinner"></span>Buscando…</div>';
  try {
    const r = await fetch('/api/organismos?nombre='+encodeURIComponent(nombre));
    const d = await r.json();
    const p = d.perfil;
    if (!p) { el.innerHTML = '<p style="color:var(--red);padding:12px">No se encontró el organismo.</p>'; return; }
    el.innerHTML = `
      <div class="metrics-row" style="margin-top:12px">
        <div class="metric-card"><div class="metric-label">Adjudicaciones</div><div class="metric-value">${(p.total||0).toLocaleString()}</div></div>
        <div class="metric-card amber"><div class="metric-label">Monto total</div><div class="metric-value">$${((p.monto_total||0)/1e6).toFixed(2)}M</div></div>
        <div class="metric-card green"><div class="metric-label">Cobrado TGN</div><div class="metric-value">$${((p.monto_cobrado||0)/1e6).toFixed(2)}M</div></div>
        <div class="metric-card ${p.hhi>2500?'red':p.hhi>1500?'amber':'green'}">
          <div class="metric-label">HHI Concentración</div>
          <div class="metric-value">${(p.hhi||0).toFixed(0)}</div>
          <div class="metric-sub">${p.hhi_interpretacion||''}</div>
        </div>
      </div>
      ${p.red_flags && p.red_flags.length ? '<h4 style="font-weight:700;margin:12px 0 8px">🚨 Red flags</h4>'+renderMiniTabla(['flag','frecuencia'],p.red_flags) : ''}
    `;
    // Gráficos de evolución
    if (p.evolucion && p.evolucion.length) {
      const evDiv = document.createElement('div');
      evDiv.id = 'chart-org-ev-'+Date.now();
      evDiv.style.minHeight = '250px';
      evDiv.style.marginTop = '12px';
      el.appendChild(evDiv);
      Plotly.newPlot(evDiv.id, [{
        type:'bar', x:p.evolucion.map(e=>e._mes), y:p.evolucion.map(e=>e.contratos),
        name:'Contratos', marker:{color:'#7c3aed'}
      }], { title:'Evolución mensual', paper_bgcolor:'rgba(0,0,0,0)', plot_bgcolor:'rgba(0,0,0,0)', margin:{t:40,b:40,l:40,r:20} }, {responsive:true,displayModeBar:false});
    }
  } catch(e) { el.innerHTML = '<p style="color:red;padding:12px">Error.</p>'; }
}

// ── Proveedores ─────────────────────────────────────────────────────────────
async function cargarProveedores(modo='cobro') {
  try {
    const r = await fetch('/api/proveedores?top=20&modo='+modo);
    const d = await r.json();
    if (modo === 'cobro') renderTabla('tabla-cobro', d.ranking || []);
    else renderTabla('tabla-riesgo-prov', d.ranking || [], row => {
      const s = parseFloat(row.score_promedio||0);
      row._nivel = s>=7?'<span class="tag tag-red">Alto</span>':s>=4?'<span class="tag tag-amber">Medio</span>':'<span class="tag tag-green">Bajo</span>';
      return row;
    });
  } catch(e) {}
}

async function buscarProveedor() {
  const cuit   = document.getElementById('prov-buscar-cuit').value.trim();
  const nombre = document.getElementById('prov-buscar-nombre').value.trim();
  if (!cuit && !nombre) return;
  const el = document.getElementById('perfil-proveedor');
  el.innerHTML = '<div class="loading"><span class="spinner"></span>Buscando…</div>';
  let url = '/api/proveedores?';
  if (cuit)   url += 'cuit='+encodeURIComponent(cuit);
  if (nombre) url += 'nombre='+encodeURIComponent(nombre);
  try {
    const r = await fetch(url);
    const d = await r.json();
    const p = d.perfil;
    if (!p) { el.innerHTML = '<p style="color:var(--red);padding:12px">No se encontró.</p>'; return; }

    if (p.candidatos) {
      el.innerHTML = '<h4 style="font-weight:700;margin-bottom:8px">Coincidencias encontradas:</h4>'+renderMiniTabla(['cuit_proveedor','nombre_can','contratos'],p.candidatos);
      return;
    }

    el.innerHTML = `
      <h3 style="font-weight:800;margin:12px 0 6px">${p.nombre||p.cuit}</h3>
      <div class="metrics-row">
        <div class="metric-card"><div class="metric-label">Contratos</div><div class="metric-value">${(p.total_contratos||0).toLocaleString()}</div></div>
        <div class="metric-card amber"><div class="metric-label">Monto Adj.</div><div class="metric-value">$${((p.monto_adj||0)/1e6).toFixed(2)}M</div></div>
        <div class="metric-card green"><div class="metric-label">Cobrado TGN</div><div class="metric-value">$${((p.monto_tgn||0)/1e6).toFixed(2)}M</div><div class="metric-sub">${p.ratio_cobro_pct||0}% del adj.</div></div>
        <div class="metric-card"><div class="metric-label">Organismos</div><div class="metric-value">${p.organismos||0}</div></div>
      </div>
    `;
    if (p.red_flags && p.red_flags.length) {
      el.innerHTML += '<h4 style="font-weight:700;margin:12px 0 8px">🚨 Red flags</h4>'+renderMiniTabla(['flag','frecuencia'],p.red_flags);
    }
  } catch(e) { el.innerHTML = '<p style="color:red;padding:12px">Error.</p>'; }
}

// ── Monitor ──────────────────────────────────────────────────────────────────
async function cargarMonitor() {
  try {
    const r = await fetch('/api/monitor');
    const d = await r.json();

    renderTablaWrap('tabla-frag-wrap',    d.fragmentacion   || [], 'Sin fragmentación detectada');
    renderTablaWrap('tabla-unico-wrap',   d.proveedor_unico || [], 'Sin proveedor único detectado');
    renderTablaWrap('tabla-rafaga-wrap',  d.rafagas         || [], 'Sin ráfagas detectadas');
    renderTablaWrap('tabla-hhi-wrap',     d.hhi             || [], 'Sin datos HHI');
    renderTablaWrap('tabla-fantasmas-wrap',d.fantasmas      || [], 'Sin fantasmas detectados');

    // Scatter ráfagas
    if (d.rafagas && d.rafagas.length) {
      const rf = d.rafagas;
      const colors = rf.map(r => r.nivel_alerta && r.nivel_alerta.includes('Crítico') ? '#dc2626' : r.nivel_alerta && r.nivel_alerta.includes('Moderado') ? '#d97706' : '#f07850');
      Plotly.newPlot('chart-rafaga', [{
        type:'scatter', mode:'markers',
        x: rf.map(r=>r.fecha_inicio||''),
        y: rf.map(r=>r.adj_en_ventana||0),
        text: rf.map(r=>r.nombre||r.cuit||''),
        marker: { color:colors, size:rf.map(r=>Math.max(8,Math.min(30,(r.monto_total||0)/5e6))), opacity:.8 },
        hovertemplate:'<b>%{text}</b><br>Fecha: %{x}<br>Adj: %{y}<extra></extra>'
      }], { title:'Ráfagas detectadas', xaxis:{title:'Fecha inicio'}, yaxis:{title:'Adj. en 7 días'}, paper_bgcolor:'rgba(0,0,0,0)', plot_bgcolor:'rgba(0,0,0,0)' }, {responsive:true,displayModeBar:false});
    } else {
      document.getElementById('chart-rafaga').innerHTML = '<p class="loading">Sin ráfagas detectadas.</p>';
    }
  } catch(e) {
    console.error('monitor error', e);
  }
}

// ── Stats ────────────────────────────────────────────────────────────────────
async function cargarStats() {
  const token = document.getElementById('admin-token').value;
  const el = document.getElementById('stats-content');
  el.innerHTML = '<div class="loading"><span class="spinner"></span>Cargando…</div>';
  try {
    const r = await fetch('/api/stats', { headers: { 'X-Admin-Token': token } });
    if (r.status === 403) { el.innerHTML = '<p style="color:red;padding:12px">Token incorrecto.</p>'; return; }
    const d = await r.json();
    if (d.error) { el.innerHTML = `<p style="color:red;padding:12px">${d.error}</p>`; return; }
    el.innerHTML = `<p style="margin-bottom:12px"><strong>Total visitas:</strong> ${d.total||0}</p>`;
    if (d.por_seccion && d.por_seccion.length) {
      el.innerHTML += '<h4 style="font-weight:700;margin-bottom:8px">Por sección</h4>'+renderMiniTabla(['seccion','v'],d.por_seccion);
    }
    if (d.por_dia && d.por_dia.length) {
      el.innerHTML += '<h4 style="font-weight:700;margin:12px 0 8px">Por día (últimos 14)</h4>'+renderMiniTabla(['dia','v'],d.por_dia);
    }
    if (d.donaciones && d.donaciones.length) {
      el.innerHTML += '<h4 style="font-weight:700;margin:12px 0 8px">Donaciones/consultas</h4>'+renderMiniTabla(['nombre','apellido','email','pais','created_at'],d.donaciones);
    }
  } catch(e) { el.innerHTML = '<p style="color:red;padding:12px">Error.</p>'; }
}

// ── Tabs ─────────────────────────────────────────────────────────────────────
function switchTab(id, btn) {
  const parent = btn.closest('section') || btn.parentElement.parentElement;
  parent.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  parent.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById(id).classList.add('active');
  // Lazy load proveedores por riesgo
  if (id === 'tab-riesgo-prov' && !_loaded['riesgo-prov']) {
    _loaded['riesgo-prov'] = true;
    cargarProveedores('riesgo');
  }
}

// ── Modal ────────────────────────────────────────────────────────────────────
document.getElementById('btn-apoyar').addEventListener('click', () => {
  document.getElementById('modal-overlay').classList.add('show');
});
function cerrarModal() { document.getElementById('modal-overlay').classList.remove('show'); }
document.getElementById('modal-overlay').addEventListener('click', e => { if(e.target===document.getElementById('modal-overlay')) cerrarModal(); });

async function enviarDonacion() {
  const body = {
    nombre:   document.getElementById('don-nombre').value,
    apellido: document.getElementById('don-apellido').value,
    email:    document.getElementById('don-email').value,
    pais:     document.getElementById('don-pais').value,
  };
  if (!body.nombre || !body.email) return alert('Completá nombre y email.');
  try {
    await fetch('/api/donacion', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
    document.getElementById('modal-msg').style.display = 'block';
    setTimeout(cerrarModal, 2000);
  } catch(e) { alert('Error al enviar.'); }
}

// ── Helpers de tabla ─────────────────────────────────────────────────────────
function renderTabla(tableId, rows, transformRow) {
  if (!rows.length) {
    const tbody = document.getElementById('tbody-'+tableId.replace('tabla-',''));
    if (tbody) tbody.innerHTML = '<tr><td colspan="99" style="padding:14px;color:var(--muted);text-align:center">Sin datos disponibles.</td></tr>';
    return;
  }
  const cols = Object.keys(rows[0]).filter(c => !c.startsWith('_'));
  const thead = document.getElementById('thead-'+tableId.replace('tabla-',''));
  const tbody = document.getElementById('tbody-'+tableId.replace('tabla-',''));
  if (thead) thead.innerHTML = cols.map(c=>`<th>${c.replace(/_/g,' ')}</th>`).join('');
  if (tbody) {
    tbody.innerHTML = rows.map(row => {
      if (transformRow) row = transformRow({...row});
      return '<tr>'+cols.map(c=>{
        let v = row[c];
        if (c==='link_bora'||c==='link') return `<td><a href="${v}" target="_blank" rel="noopener">Ver</a></td>`;
        if (c==='nivel_riesgo_licit'||c==='nivel') {
          const cls = v==='Alto'?'tag-red':v==='Medio'?'tag-amber':'tag-green';
          return `<td><span class="tag ${cls}">${v||''}</span></td>`;
        }
        if (typeof v === 'number' && v > 1e5) return `<td>$${v.toLocaleString('es-AR',{maximumFractionDigits:0})}</td>`;
        return `<td>${v??''}</td>`;
      }).join('')+'</tr>';
    }).join('');
  }
}

function renderTablaWrap(wrapId, rows, emptyMsg='Sin datos') {
  const el = document.getElementById(wrapId);
  if (!el) return;
  if (!rows.length) { el.innerHTML = `<p style="padding:12px;color:var(--muted)">${emptyMsg}</p>`; return; }
  const cols = Object.keys(rows[0]).filter(c=>!c.startsWith('_'));
  el.innerHTML = '<table><thead><tr>'+cols.map(c=>`<th>${c.replace(/_/g,' ')}</th>`).join('')+'</tr></thead><tbody>'+
    rows.map(row=>'<tr>'+cols.map(c=>{
      let v=row[c];
      if(typeof v==='number'&&v>1e5) return `<td>$${v.toLocaleString('es-AR',{maximumFractionDigits:0})}</td>`;
      return `<td>${v??''}</td>`;
    }).join('')+'</tr>').join('')+'</tbody></table>';
}

function renderMiniTabla(cols, rows) {
  if (!rows || !rows.length) return '<p style="color:var(--muted);font-size:12px">Sin datos.</p>';
  return '<div class="table-wrap"><table><thead><tr>'+cols.map(c=>`<th>${c.replace(/_/g,' ')}</th>`).join('')+'</tr></thead><tbody>'+
    rows.map(r=>'<tr>'+cols.map(c=>`<td>${r[c]??''}</td>`).join('')+'</tr>').join('')+'</tbody></table></div>';
}

// ── Init ─────────────────────────────────────────────────────────────────────
_loaded['contratos'] = true;
loadSection('contratos');
</script>
</body>
</html>
"""


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
