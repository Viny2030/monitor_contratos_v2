"""
dashboard_v2.py — Dashboard Integrado v2
Monitor de Fenómenos Corruptivos — Ph.D. Monteverde (2020)

4 secciones principales:
  📄 Contratos     — flujo diario BORA→Comprar→TGN con filtros
  🏛️ Organismos   — perfil por organismo contratante
  🏢 Proveedores   — perfil por CUIT (quién cobró, no solo quién ganó)
  🔍 Monitor       — análisis de concentración y red flags sistémicas

Correr:
    streamlit run dashboard_v2.py
"""

import os
import re
import glob
import logging
import unicodedata
from datetime import datetime

import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

# ─── BASE DE DATOS ────────────────────────────────────────────────────────────
_DATABASE_URL = os.getenv("DATABASE_URL", "")

def _get_conn():
    url = _DATABASE_URL.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url, cursor_factory=RealDictCursor)

def _init_db():
    """Crea las tablas si no existen. Se llama una vez al iniciar."""
    if not _DATABASE_URL:
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS donaciones_consultas (
                    id         SERIAL PRIMARY KEY,
                    nombre     VARCHAR(100),
                    apellido   VARCHAR(100),
                    email      VARCHAR(254),
                    pais       VARCHAR(30),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS estadisticas_acceso (
                    id         SERIAL PRIMARY KEY,
                    seccion    VARCHAR(100) NOT NULL,
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
    """Registra que el usuario navegó a una sección."""
    if not _DATABASE_URL:
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO estadisticas_acceso (seccion) VALUES (%s)", (seccion,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"tracking error: {e}")

def _registrar_donacion(nombre: str, apellido: str, email: str, pais: str):
    """Registra una consulta de donación."""
    if not _DATABASE_URL:
        return None
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO donaciones_consultas (nombre, apellido, email, pais) VALUES (%s,%s,%s,%s) RETURNING id",
                (nombre.strip(), apellido.strip(), email.strip(), pais)
            )
            row = cur.fetchone()
        conn.commit()
        conn.close()
        return row["id"]
    except Exception as e:
        logger.warning(f"donacion registro error: {e}")
        return None

def _get_stats():
    """Devuelve estadísticas para el panel admin."""
    if not _DATABASE_URL:
        return None
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM estadisticas_acceso;")
            total = cur.fetchone()["total"]
            cur.execute("SELECT seccion, COUNT(*) AS v FROM estadisticas_acceso GROUP BY seccion ORDER BY v DESC;")
            por_seccion = cur.fetchall()
            cur.execute("SELECT DATE(created_at AT TIME ZONE 'America/Argentina/Buenos_Aires') AS dia, COUNT(*) AS v FROM estadisticas_acceso GROUP BY dia ORDER BY dia DESC LIMIT 14;")
            por_dia = cur.fetchall()
            cur.execute("SELECT id, nombre, apellido, email, pais, created_at FROM donaciones_consultas ORDER BY created_at DESC LIMIT 50;")
            donaciones = cur.fetchall()
        conn.close()
        return {"total": total, "por_seccion": list(por_seccion), "por_dia": list(por_dia), "donaciones": list(donaciones)}
    except Exception as e:
        return {"error": str(e)}

# Inicializar DB al importar el módulo
_init_db()

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

# Módulos propios
from perfil_organismo    import cargar_historico as cargar_hist_org
from perfil_organismo    import buscar_organismo, parsear_monto as pm_org, calcular_hhi, interpretar_hhi
from perfil_cuit         import cargar_historico as cargar_hist_cuit
from perfil_cuit         import buscar_por_cuit, buscar_por_nombre_listar, top_por_cobro_tgn, ranking_por_riesgo
from analisis_concentracion import (
    cargar_historico as cargar_hist_conc,
    preparar_df,
    detectar_fragmentacion,
    detectar_proveedor_unico,
    detectar_rafaga,
    analisis_hhi,
    detectar_fantasmas,
)

# ─────────────────────────────────────────
# CONFIGURACIÓN DE PÁGINA
# ─────────────────────────────────────────
st.set_page_config(
    page_title="Monitor de Fenomenos Corruptivos",
    page_icon="🏛️",
    layout="wide",
    initial_sidebar_state="expanded",
)

import plotly.io as pio
pio.templates["indigo"] = go.layout.Template(
    layout=go.Layout(
        paper_bgcolor="rgba(245,243,255,0)",
        plot_bgcolor="rgba(245,243,255,0)",
        font=dict(color="#1e1b4b", family="Inter, sans-serif"),
        colorway=["#7c3aed","#8b5cf6","#059669","#d97706","#dc2626","#0891b2","#a78bfa"],
        xaxis=dict(gridcolor="#ddd6fe", linecolor="#c4b5fd", zerolinecolor="#ddd6fe"),
        yaxis=dict(gridcolor="#ddd6fe", linecolor="#c4b5fd", zerolinecolor="#ddd6fe"),
        legend=dict(bgcolor="#ffffff", bordercolor="#ddd6fe"),
        hoverlabel=dict(bgcolor="#ffffff", bordercolor="#c4b5fd", font_color="#1e1b4b"),
    )
)
pio.templates.default = "indigo"

# Path compatible con local y Railway (/app es el WORKDIR del Dockerfile)
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# ─────────────────────────────────────────
# ESTILOS
# ----------------------------------------
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
    :root {
        --bg-primary:#f5f3ff; --bg-card:#ffffff; --border:#ddd6fe;
        --border-bright:#c4b5fd; --accent-main:#7c3aed; --accent-light:#8b5cf6;
        --accent-pale:#ede9fe; --accent-red:#dc2626; --accent-amber:#d97706;
        --accent-green:#059669; --text-primary:#1e1b4b; --text-secondary:#4c1d95;
        --text-muted:#7c3aed; --shadow:0 2px 12px rgba(124,58,237,0.10);
    }
    html,body,[class*="css"]{font-family:'Inter',sans-serif!important;}
    .stApp{background:linear-gradient(135deg,#f5f3ff 0%,#ede9fe 50%,#faf5ff 100%)!important;color:var(--text-primary)!important;}
    section[data-testid="stSidebar"]{background:linear-gradient(180deg,#4c1d95 0%,#5b21b6 60%,#6d28d9 100%)!important;border-right:none!important;box-shadow:4px 0 20px rgba(124,58,237,0.25)!important;}
    section[data-testid="stSidebar"] *{color:#ede9fe!important;}
    section[data-testid="stSidebar"] hr{border-color:rgba(167,139,250,0.3)!important;}
    section[data-testid="stSidebar"] button[kind="primary"]{background:linear-gradient(135deg,#f59e0b,#d97706)!important;color:#fff!important;border:none!important;font-weight:700!important;border-radius:10px!important;}
    [data-testid="stMetric"]{background:var(--bg-card)!important;border:1px solid var(--border)!important;border-radius:12px!important;padding:16px 20px!important;box-shadow:var(--shadow)!important;}
    [data-testid="stMetricLabel"]{color:var(--text-secondary)!important;font-size:11px!important;font-weight:600!important;text-transform:uppercase!important;letter-spacing:.06em!important;}
    [data-testid="stMetricValue"]{color:var(--text-primary)!important;font-weight:800!important;}
    [data-testid="stMetricDelta"]{color:var(--accent-green)!important;}
    button[data-baseweb="tab"]{background:var(--bg-card)!important;color:var(--text-secondary)!important;border:1px solid var(--border)!important;border-radius:8px 8px 0 0!important;font-weight:500!important;}
    button[data-baseweb="tab"][aria-selected="true"]{background:var(--accent-main)!important;color:#fff!important;border-color:var(--accent-main)!important;font-weight:700!important;}
    [data-baseweb="select"]>div,[data-baseweb="input"]>div{background:var(--bg-card)!important;border-color:var(--border-bright)!important;color:var(--text-primary)!important;border-radius:8px!important;}
    [data-testid="stDataFrame"]{border:1px solid var(--border)!important;border-radius:10px!important;box-shadow:var(--shadow)!important;}
    hr{border-color:var(--border)!important;}
    .metric-card{background:var(--bg-card);border-radius:12px;padding:16px 20px;border-left:4px solid var(--accent-main);margin-bottom:8px;border:1px solid var(--border);border-left:4px solid var(--accent-main);box-shadow:var(--shadow);}
    .metric-card.rojo{border-left-color:var(--accent-red)!important;}
    .metric-card.verde{border-left-color:var(--accent-green)!important;}
    .metric-card.naranja{border-left-color:var(--accent-amber)!important;}
    .metric-label{font-size:11px;color:var(--text-secondary);margin-bottom:4px;text-transform:uppercase;letter-spacing:.06em;font-weight:600;}
    .metric-value{font-size:26px;font-weight:800;color:var(--text-primary);}
    .metric-sub{font-size:12px;color:var(--text-muted);margin-top:4px;}
    .tag-rojo{background:#fee2e2;color:var(--accent-red);padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600;}
    .tag-naranja{background:#fef3c7;color:var(--accent-amber);padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600;}
    .tag-verde{background:#d1fae5;color:var(--accent-green);padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600;}
    .seccion-titulo{font-size:18px;font-weight:800;margin:20px 0 8px;color:var(--text-primary);}
    .diferencial{background:var(--accent-pale);border-left:4px solid var(--accent-main);padding:10px 14px;border-radius:8px;font-size:13px;margin:8px 0;color:var(--text-primary);box-shadow:var(--shadow);}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────
# CARGA DE DATOS (cacheada)
# ─────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner="Cargando datos históricos...")
def cargar_datos():
    patron   = os.path.join(DATA_DIR, "**", "reporte_*.xlsx")
    archivos = sorted(glob.glob(patron, recursive=True))

    dfs_flujo = []
    dfs_adj   = []
    dfs_tgn   = []

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
                df = pd.read_excel(xl, sheet_name="🏆 Adjudicaciones", engine="openpyxl")
                dfs_adj.append(df)
            if "💰 TGN" in xl.sheet_names:
                df = pd.read_excel(xl, sheet_name="💰 TGN", engine="openpyxl")
                dfs_tgn.append(df)
        except Exception:
            pass

    df_flujo = pd.concat(dfs_flujo, ignore_index=True) if dfs_flujo else pd.DataFrame()
    df_adj   = pd.concat(dfs_adj,   ignore_index=True) if dfs_adj   else pd.DataFrame()
    df_tgn   = pd.concat(dfs_tgn,   ignore_index=True) if dfs_tgn   else pd.DataFrame()

    for df in [df_flujo, df_adj]:
        for col in ["fecha", "fecha_extraccion", "fecha_publicacion"]:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")

    return df_flujo, df_adj, df_tgn, len(archivos)


def parsear_monto(valor):
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


def col_de(df, opciones):
    return next((c for c in opciones if c in df.columns), None)


# ─────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────
def sidebar():
    st.sidebar.markdown("""
    <div style="padding:12px 4px 4px 4px;">
      <div style="font-size:18px;font-weight:800;color:#e2e8f0;">
        ⚖️ Monitor de Contratos
      </div>
      <div style="font-size:11px;color:#c4b5fd;margin-top:3px;">
        Ph.D. Monteverde · Algoritmos contra la Corrupción
      </div>
    </div>
    """, unsafe_allow_html=True)
    st.sidebar.divider()

    seccion = st.sidebar.radio(
        "Sección",
        ["📄 Contratos", "🏛️ Organismos", "🏢 Proveedores", "🔍 Monitor", "📊 Estadísticas"],
        label_visibility="collapsed",
    )
    st.sidebar.divider()
    st.sidebar.markdown("""
    <div style="font-size:12px;color:#c4b5fd;line-height:1.6;padding:0 4px;">
      <b style="color:#e2e8f0;">Diferencial único:</b><br>
      Muestra quién <b style="color:#a78bfa;">cobró</b> (TGN),
      no solo quién ganó.<br><br>
      <span style="color:#7c3aed;">Cruce BORA → Comprar → TGN</span>
    </div>
    """, unsafe_allow_html=True)

    st.sidebar.markdown("<br>", unsafe_allow_html=True)

    # Botón Apoyar nativo Streamlit
    if st.sidebar.button("💛 Apoyar este proyecto", use_container_width=True, type="primary"):
        st.session_state["mostrar_modal_apoyar"] = True

    st.sidebar.divider()

    # Botón descarga instructivo
    import os
    instructivo_path = os.path.join(os.getcwd(), "instructivo_monitor.docx")
    if os.path.exists(instructivo_path):
        with open(instructivo_path, "rb") as f:
            st.sidebar.download_button(
                label="📥 Descargar instructivo",
                data=f,
                file_name="instructivo_monitor.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                use_container_width=True,
            )

    st.sidebar.caption(
        "Ref: Monteverde, V. (2021). "
        "*Great corruption – theory of corrupt phenomena*. "
        "*Journal of Financial Crime*, Vol. 28 No. 2. "
        "https://doi.org/10.1108/jfc-07-2019-0104"
    )

    st.sidebar.divider()
    st.sidebar.markdown("""
    <div style="padding:8px 4px;">
      <div style="font-size:12px;font-weight:700;color:#e2e8f0;margin-bottom:8px;">👤 Sobre el Autor</div>
      <img src="https://raw.githubusercontent.com/Viny2030/monitor_contratos_v2/feature/base-datos/static/foto_monteverde.jpg"
           style="width:64px;height:64px;border-radius:50%;border:2px solid #7c3aed;display:block;margin:0 auto 8px auto;">
      <div style="font-size:11px;font-weight:700;color:#c4b5fd;text-align:center;">Ph.D. Vicente H. Monteverde</div>
      <div style="font-size:10px;color:#94a3b8;text-align:center;margin-top:4px;line-height:1.5;">
        Investigador en economía política y fenómenos de corrupción.
      </div>
      <div style="margin-top:10px;display:flex;flex-direction:column;gap:4px;">
        <a href="mailto:vhmonte@retina.ar"
           style="display:block;text-align:center;font-size:10px;color:#a78bfa;
                  background:rgba(124,58,237,0.15);border-radius:6px;padding:4px 8px;
                  text-decoration:none;">✉️ vhmonte@retina.ar</a>
        <a href="mailto:viny01958@gmail.com"
           style="display:block;text-align:center;font-size:10px;color:#a78bfa;
                  background:rgba(124,58,237,0.15);border-radius:6px;padding:4px 8px;
                  text-decoration:none;">✉️ viny01958@gmail.com</a>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # Registrar visita a la sección (solo cuando cambia)
    if st.session_state.get("_ultima_seccion") != seccion:
        st.session_state["_ultima_seccion"] = seccion
        _registrar_visita(seccion)

    return seccion


# ─────────────────────────────────────────
# SECCIÓN 1: CONTRATOS
# ─────────────────────────────────────────
def seccion_contratos(df_flujo, df_tgn):
    st.title("📄 Contratos")
    st.caption("Flujo diario: BORA → Comprar.gob.ar → TGN")

    if df_flujo.empty:
        st.warning("Sin datos. Ejecutá `python diario.py` primero.")
        return

    df = df_flujo.copy()
    df["_monto"] = df[col_de(df, ["monto_adjudicado_bora", "monto_adjudicado"])].apply(parsear_monto) if col_de(df, ["monto_adjudicado_bora", "monto_adjudicado"]) else 0.0
    df["_tgn"]   = df[col_de(df, ["monto_cobrado_tgn"])].apply(parsear_monto) if col_de(df, ["monto_cobrado_tgn"]) else 0.0

    col_fecha = col_de(df, ["fecha", "fecha_publicacion", "fecha_extraccion"])

    # ── Métricas globales ──
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.metric("Total registros", f"{len(df):,}")
    with c2:
        flujos = df["alerta"].str.contains("FLUJO COMPLETO", na=False).sum() if "alerta" in df.columns else 0
        st.metric("Flujos BORA→TGN", f"{flujos:,}", help="Contratos con trazabilidad completa")
    with c3:
        monto_total = df["_monto"].sum()
        st.metric("Monto adjudicado", f"${monto_total/1e6:.1f}M ARS")
    with c4:
        monto_cobrado = df["_tgn"].sum()
        st.metric("Cobrado en TGN", f"${monto_cobrado/1e6:.1f}M ARS",
                  delta=f"{monto_cobrado/monto_total*100:.1f}% del adjudicado" if monto_total > 0 else "")
    with c5:
        alto = (df["nivel_riesgo_licit"] == "Alto").sum() if "nivel_riesgo_licit" in df.columns else 0
        st.metric("🔴 Riesgo Alto", f"{alto:,}")

    st.divider()

    # ── Filtros ──
    col_f1, col_f2, col_f3 = st.columns(3)

    with col_f1:
        if col_fecha:
            fechas = df[col_fecha].dropna()
            if not fechas.empty:
                fecha_min = fechas.min().date()
                fecha_max = fechas.max().date()
                rango = st.date_input(
                    "Período",
                    value=(fecha_min, fecha_max),
                    min_value=fecha_min,
                    max_value=fecha_max,
                )
                if len(rango) == 2:
                    df = df[
                        df[col_fecha].dt.date.between(rango[0], rango[1])
                    ]

    with col_f2:
        if "nivel_riesgo_licit" in df.columns:
            niveles = ["Todos"] + sorted(df["nivel_riesgo_licit"].dropna().unique().tolist())
            nivel_sel = st.selectbox("Nivel de riesgo", niveles)
            if nivel_sel != "Todos":
                df = df[df["nivel_riesgo_licit"] == nivel_sel]

    with col_f3:
        col_org = col_de(df, ["organismo_contratante", "organismo"])
        if col_org:
            orgs = ["Todos"] + sorted(df[col_org].dropna().unique().tolist())
            org_sel = st.selectbox("Organismo", orgs)
            if org_sel != "Todos":
                df = df[df[col_org] == org_sel]

    st.caption(f"{len(df):,} contratos con los filtros aplicados")

    # ── Gráfico: alertas por tipo ──
    if "alerta" in df.columns and not df.empty:
        col_g1, col_g2 = st.columns(2)
        with col_g1:
            conteo_alertas = df["alerta"].value_counts().reset_index()
            conteo_alertas.columns = ["alerta", "cantidad"]
            fig = px.bar(
                conteo_alertas,
                x="cantidad", y="alerta",
                orientation="h",
                title="Distribución por tipo de flujo",
                color="cantidad",
                color_continuous_scale=[[0,"#faf5ff"],[0.5,"#dc2626"],[1,"#991b1b"]],
            )
            fig.update_layout(showlegend=False, yaxis_title="", xaxis_title="Contratos")
            st.plotly_chart(fig, use_container_width=True)

        with col_g2:
            if col_fecha and not df[col_fecha].isna().all():
                df["_mes"] = df[col_fecha].dt.to_period("M").astype(str)
                evol = df.groupby("_mes").agg(
                    contratos=("_monto", "count"),
                    monto=("_monto", "sum"),
                ).reset_index()
                fig2 = px.bar(
                    evol, x="_mes", y="contratos",
                    title="Evolución mensual de adjudicaciones",
                    labels={"_mes": "Mes", "contratos": "Contratos"},
                )
                fig2.update_layout(xaxis_title="", yaxis_title="Contratos")
                st.plotly_chart(fig2, use_container_width=True)

    # ── Tabla de contratos ──
    st.subheader("📋 Tabla de contratos")

    cols_tabla = [c for c in [
        col_fecha, col_org,
        "cuit_proveedor", "proveedor_adjudicado",
        "monto_adjudicado_bora", "cobro_en_tgn", "monto_cobrado_tgn",
        "nivel_riesgo_licit", "indicadores_riesgo", "alerta", "link_bora",
    ] if c and c in df.columns]

    df_tabla = df[cols_tabla].copy() if cols_tabla else df.copy()
    if col_fecha and col_fecha in df_tabla.columns:
        df_tabla[col_fecha] = df_tabla[col_fecha].dt.strftime("%Y-%m-%d")

    st.dataframe(
        df_tabla.head(500),
        use_container_width=True,
        hide_index=True,
        column_config={
            "link_bora": st.column_config.LinkColumn("Link BORA"),
            "nivel_riesgo_licit": st.column_config.TextColumn("Riesgo"),
            "monto_adjudicado_bora": st.column_config.TextColumn("Monto Adj."),
            "monto_cobrado_tgn": st.column_config.TextColumn("Cobrado TGN"),
        }
    )


# ─────────────────────────────────────────
# SECCIÓN 2: ORGANISMOS
# ─────────────────────────────────────────
def seccion_organismos(df_flujo):
    st.title("🏛️ Organismos")
    st.caption("Perfil por organismo contratante — concentración, proveedores, evolución")

    if df_flujo.empty:
        st.warning("Sin datos. Ejecutá `python diario.py` primero.")
        return

    col_org  = col_de(df_flujo, ["organismo_contratante", "organismo"])
    col_monto = col_de(df_flujo, ["monto_adjudicado_bora", "monto_adjudicado"])
    col_cuit  = col_de(df_flujo, ["cuit_proveedor", "cuit"])
    col_fecha = col_de(df_flujo, ["fecha", "fecha_publicacion"])

    if not col_org:
        st.error("No se encontró columna de organismo en los datos.")
        return

    df = df_flujo.copy()
    df["_monto"] = df[col_monto].apply(parsear_monto) if col_monto else 0.0

    # ── Ranking de organismos ──
    grp = df.groupby(col_org).agg(
        adjudicaciones=("_monto", "count"),
        monto_total=("_monto", "sum"),
        cuits_distintos=(col_cuit, "nunique") if col_cuit else ("_monto", "count"),
    ).reset_index().sort_values("monto_total", ascending=False)

    col_r1, col_r2 = st.columns([2, 1])
    with col_r1:
        fig = px.bar(
            grp.head(15),
            x="monto_total", y=col_org,
            orientation="h",
            title="Top 15 organismos por monto adjudicado",
            labels={"monto_total": "Monto ARS", col_org: ""},
            color="monto_total",
            color_continuous_scale=[[0,"#faf5ff"],[0.5,"#7c3aed"],[1,"#4c1d95"]],
        )
        fig.update_layout(showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

    with col_r2:
        fig2 = px.bar(
            grp.head(15),
            x="adjudicaciones", y=col_org,
            orientation="h",
            title="Top 15 por cantidad de adjudicaciones",
            labels={"adjudicaciones": "Adjudicaciones", col_org: ""},
            color="adjudicaciones",
            color_continuous_scale=[[0,"#faf5ff"],[0.5,"#d97706"],[1,"#92400e"]],
        )
        fig2.update_layout(showlegend=False)
        st.plotly_chart(fig2, use_container_width=True)

    st.divider()

    # ── Perfil individual ──
    st.subheader("🔎 Perfil individual de organismo")
    organismos_lista = sorted(df[col_org].dropna().unique().tolist())
    org_sel = st.selectbox("Seleccioná un organismo", ["— elegir —"] + organismos_lista)

    if org_sel != "— elegir —":
        df_org = df[df[col_org] == org_sel].copy()

        m1, m2, m3, m4 = st.columns(4)
        monto_tot = df_org["_monto"].sum()
        cuits_u   = df_org[col_cuit].nunique() if col_cuit else 0
        flujos    = df_org["alerta"].str.contains("FLUJO COMPLETO", na=False).sum() if "alerta" in df_org.columns else 0
        cobros    = df_org["cobro_en_tgn"].isin(["✅ SÍ", True]).sum() if "cobro_en_tgn" in df_org.columns else 0

        m1.metric("Adjudicaciones", f"{len(df_org):,}")
        m2.metric("Monto total", f"${monto_tot/1e6:.2f}M ARS")
        m3.metric("Proveedores (CUITs)", f"{cuits_u:,}")
        m4.metric("Cobraron en TGN", f"{cobros:,}")

        # HHI
        if col_cuit:
            por_cuit = df_org.groupby(col_cuit)["_monto"].sum()
            hhi = calcular_hhi(por_cuit)
            interpretacion = interpretar_hhi(hhi)
            st.markdown(
                f"<div class='diferencial'>📐 <b>Índice HHI de concentración:</b> "
                f"{hhi:.0f} — {interpretacion}</div>",
                unsafe_allow_html=True,
            )

        col_p1, col_p2 = st.columns(2)

        # Proveedores frecuentes
        with col_p1:
            if col_cuit:
                col_prov = col_de(df_org, ["proveedor_adjudicado", "proveedor_nombre"])
                grp_prov = df_org.groupby(col_cuit).agg(
                    contratos=("_monto", "count"),
                    monto=("_monto", "sum"),
                    **({"nombre": pd.NamedAgg(column=col_prov, aggfunc="first")} if col_prov else {}),
                ).reset_index().sort_values("contratos", ascending=False).head(10)

                etiquetas = grp_prov.get("nombre", grp_prov[col_cuit]).fillna(grp_prov[col_cuit]).astype(str).str[:30]
                fig3 = px.pie(
                    grp_prov,
                    values="monto",
                    names=etiquetas,
                    title="Distribución de monto por proveedor (top 10)",
                    hole=0.4,
                )
                st.plotly_chart(fig3, use_container_width=True)

        # Evolución mensual
        with col_p2:
            if col_fecha:
                df_org["_mes"] = df_org[col_fecha].dt.to_period("M").astype(str)
                evol = df_org.groupby("_mes").agg(
                    contratos=("_monto", "count"),
                    monto=("_monto", "sum"),
                ).reset_index()
                fig4 = px.line(
                    evol, x="_mes", y="monto",
                    title="Evolución mensual del monto adjudicado",
                    labels={"_mes": "", "monto": "Monto ARS"},
                    markers=True,
                )
                st.plotly_chart(fig4, use_container_width=True)

        # Red flags
        if "indicadores_riesgo" in df_org.columns:
            st.subheader("🚨 Red flags históricas")
            flags = df_org["indicadores_riesgo"].dropna()
            flags = flags[flags != "✅ Sin alertas"]
            if flags.empty:
                st.success("Sin red flags detectadas para este organismo")
            else:
                todos = []
                for f in flags:
                    todos.extend([x.strip() for x in str(f).split("|")])
                conteo = pd.Series(todos).value_counts().reset_index()
                conteo.columns = ["Red Flag", "Frecuencia"]
                st.dataframe(conteo, use_container_width=True, hide_index=True)

        # Tabla de contratos del organismo
        st.subheader("📄 Contratos")
        cols_m = [c for c in [
            col_fecha, col_cuit, "proveedor_adjudicado",
            "monto_adjudicado_bora", "cobro_en_tgn",
            "nivel_riesgo_licit", "alerta", "link_bora",
        ] if c and c in df_org.columns]
        df_show = df_org[cols_m].copy()
        if col_fecha in df_show.columns:
            df_show[col_fecha] = df_show[col_fecha].dt.strftime("%Y-%m-%d")
        st.dataframe(df_show, use_container_width=True, hide_index=True,
                     column_config={"link_bora": st.column_config.LinkColumn("Link")})


# ─────────────────────────────────────────
# SECCIÓN 3: PROVEEDORES
# ─────────────────────────────────────────
def seccion_proveedores(df_flujo, df_tgn):
    st.title("🏢 Proveedores")
    st.markdown(
        "<div class='diferencial'>💡 <b>Diferencial único:</b> este sistema muestra quién "
        "<b>cobró</b> (TGN), no solo quién ganó la licitación. "
        "El cruce BORA → Comprar → TGN es exclusivo en Argentina.</div>",
        unsafe_allow_html=True,
    )

    if df_flujo.empty:
        st.warning("Sin datos. Ejecutá `python diario.py` primero.")
        return

    df = df_flujo.copy()
    col_cuit  = col_de(df, ["cuit_proveedor", "cuit"])
    col_prov  = col_de(df, ["proveedor_adjudicado", "proveedor_nombre"])
    col_monto = col_de(df, ["monto_adjudicado_bora", "monto_adjudicado"])
    col_tgn   = col_de(df, ["monto_cobrado_tgn"])
    col_org   = col_de(df, ["organismo_contratante", "organismo"])
    col_fecha = col_de(df, ["fecha", "fecha_publicacion"])

    df["_monto"] = df[col_monto].apply(parsear_monto) if col_monto else 0.0
    df["_tgn"]   = df[col_tgn].apply(parsear_monto)   if col_tgn   else 0.0

    tab1, tab2, tab3 = st.tabs([
        "🏆 Ranking por cobro TGN",
        "🔴 Ranking por riesgo",
        "🔎 Perfil individual",
    ])

    # ── Tab 1: Ranking por cobro TGN ──
    with tab1:
        st.subheader("💰 Quiénes más cobraron del Estado (vía TGN)")
        if col_cuit and col_tgn:
            df_cobros = df[df["_tgn"] > 0].copy()
            if df_cobros.empty:
                st.info("Sin registros de cobros en TGN todavía. "
                        "Puede ser que TGN no devolvió datos en los últimos scrapings.")
            else:
                grp = (
                    df_cobros.groupby(col_cuit)
                    .agg(
                        monto_tgn=("_tgn", "sum"),
                        monto_adj=("_monto", "sum"),
                        contratos=("_monto", "count"),
                        **({"nombre": pd.NamedAgg(column=col_prov, aggfunc="first")} if col_prov else {}),
                    )
                    .reset_index()
                    .sort_values("monto_tgn", ascending=False)
                    .head(20)
                )
                etiquetas = grp.get("nombre", grp[col_cuit]).fillna(grp[col_cuit]).astype(str).str[:35]
                fig = px.bar(
                    grp.head(15),
                    x="monto_tgn",
                    y=etiquetas[:15],
                    orientation="h",
                    title="Top 15 proveedores por monto cobrado en TGN",
                    labels={"x": "Monto cobrado ARS", "y": ""},
                    color="monto_tgn",
                    color_continuous_scale=[[0,"#faf5ff"],[0.5,"#059669"],[1,"#064e3b"]],
                )
                fig.update_layout(showlegend=False)
                st.plotly_chart(fig, use_container_width=True)
                st.dataframe(grp, use_container_width=True, hide_index=True)
        else:
            st.info("No hay columnas de TGN en los datos actuales.")

    # ── Tab 2: Ranking por riesgo ──
    with tab2:
        st.subheader("🔴 Proveedores con mayor score de riesgo (Monteverde)")
        if "indice_riesgo_licit" in df.columns and col_cuit:
            df_riesgo = df[df[col_cuit].astype(str).str.strip() != ""].copy()
            grp_r = (
                df_riesgo.groupby(col_cuit)
                .agg(
                    score_promedio=("indice_riesgo_licit", "mean"),
                    score_maximo=("indice_riesgo_licit", "max"),
                    contratos=("indice_riesgo_licit", "count"),
                    **({"nombre": pd.NamedAgg(column=col_prov, aggfunc="first")} if col_prov else {}),
                )
                .reset_index()
                .sort_values("score_promedio", ascending=False)
                .head(20)
            )
            grp_r["nivel"] = grp_r["score_promedio"].apply(
                lambda s: "🔴 Alto" if s >= 7 else ("🟡 Medio" if s >= 4 else "🟢 Bajo")
            )
            etiq_r = grp_r.get("nombre", grp_r[col_cuit]).fillna(grp_r[col_cuit]).astype(str).str[:35]
            fig_r = px.bar(
                grp_r.head(15),
                x="score_promedio",
                y=etiq_r[:15],
                orientation="h",
                title="Top 15 proveedores por score de riesgo promedio",
                color="score_promedio",
                color_continuous_scale=[[0,"#faf5ff"],[0.5,"#dc2626"],[1,"#991b1b"]],
                range_color=[0, 10],
            )
            fig_r.update_layout(showlegend=False)
            st.plotly_chart(fig_r, use_container_width=True)
            st.dataframe(grp_r, use_container_width=True, hide_index=True)
        else:
            st.info("Sin scores de riesgo calculados. Verificá que `analisis.py` corra junto con `diario.py`.")

    # ── Tab 3: Perfil individual ──
    with tab3:
        st.subheader("🔎 Perfil completo por CUIT")
        busqueda = st.text_input("Ingresá CUIT o nombre de empresa", placeholder="30-12345678-9 o TECHINT")

        if busqueda:
            es_cuit = bool(re.search(r"\d{7,}", busqueda))
            if es_cuit:
                cuit_norm = re.sub(r"[^\d]", "", busqueda)
                df_prov = df[df[col_cuit].apply(
                    lambda x: re.sub(r"[^\d]", "", str(x)) == cuit_norm
                )].copy() if col_cuit else pd.DataFrame()
            else:
                def normalizar(t):
                    t = t.upper()
                    t = unicodedata.normalize("NFD", t)
                    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
                    return re.sub(r"\s+", " ", t).strip()
                busq_norm = normalizar(busqueda)
                df_prov = df[df[col_prov].apply(
                    lambda x: busq_norm in normalizar(str(x))
                )].copy() if col_prov else pd.DataFrame()

            if df_prov.empty:
                st.warning(f"No se encontraron resultados para '{busqueda}'")
            else:
                nombre_canon = ""
                if col_prov:
                    nombres = df_prov[col_prov].dropna()
                    if not nombres.empty:
                        nombre_canon = nombres.value_counts().index[0]

                st.success(f"**{nombre_canon or busqueda}** — {len(df_prov)} contratos encontrados")

                m1, m2, m3, m4 = st.columns(4)
                monto_a = df_prov["_monto"].sum()
                monto_t = df_prov["_tgn"].sum()
                orgs_u  = df_prov[col_org].nunique() if col_org else 0
                ratio   = monto_t / monto_a * 100 if monto_a > 0 else 0

                m1.metric("Contratos", f"{len(df_prov):,}")
                m2.metric("Monto adjudicado", f"${monto_a/1e6:.2f}M ARS")
                m3.metric("Cobrado en TGN", f"${monto_t/1e6:.2f}M ARS",
                          delta=f"{ratio:.1f}% del adjudicado")
                m4.metric("Organismos", f"{orgs_u:,}")

                col_v1, col_v2 = st.columns(2)

                with col_v1:
                    if col_org:
                        por_org = df_prov.groupby(col_org)["_monto"].agg(["sum", "count"]).reset_index()
                        por_org.columns = [col_org, "monto", "contratos"]
                        fig_o = px.bar(
                            por_org.sort_values("monto", ascending=False).head(10),
                            x="monto", y=col_org,
                            orientation="h",
                            title="Monto por organismo",
                        )
                        st.plotly_chart(fig_o, use_container_width=True)

                with col_v2:
                    if col_fecha:
                        df_prov["_mes"] = df_prov[col_fecha].dt.to_period("M").astype(str)
                        evol_p = df_prov.groupby("_mes")["_monto"].sum().reset_index()
                        fig_ep = px.line(
                            evol_p, x="_mes", y="_monto",
                            title="Evolución mensual",
                            markers=True,
                        )
                        st.plotly_chart(fig_ep, use_container_width=True)

                # Red flags del proveedor
                if "indicadores_riesgo" in df_prov.columns:
                    flags = df_prov["indicadores_riesgo"].dropna()
                    flags = flags[flags != "✅ Sin alertas"]
                    if not flags.empty:
                        st.subheader("🚨 Red flags de este proveedor")
                        todos = []
                        for f in flags:
                            todos.extend([x.strip() for x in str(f).split("|")])
                        conteo = pd.Series(todos).value_counts().reset_index()
                        conteo.columns = ["Red Flag", "Frecuencia"]
                        st.dataframe(conteo, use_container_width=True, hide_index=True)

                # Tabla de contratos
                st.subheader("📄 Contratos de este proveedor")
                cols_p = [c for c in [
                    col_fecha, col_org, "monto_adjudicado_bora",
                    "cobro_en_tgn", "monto_cobrado_tgn",
                    "nivel_riesgo_licit", "alerta", "link_bora",
                ] if c and c in df_prov.columns]
                df_sp = df_prov[cols_p].copy()
                if col_fecha in df_sp.columns:
                    df_sp[col_fecha] = df_sp[col_fecha].dt.strftime("%Y-%m-%d")
                st.dataframe(df_sp, use_container_width=True, hide_index=True,
                             column_config={"link_bora": st.column_config.LinkColumn("Link")})


# ─────────────────────────────────────────
# SECCIÓN 4: MONITOR
# ─────────────────────────────────────────
def seccion_monitor(df_flujo):
    st.title("🔍 Monitor de Concentración")
    st.caption("Análisis sistémico de patrones — Teoría Monteverde (2020)")

    if df_flujo.empty:
        st.warning("Sin datos. Ejecutá `python diario.py` primero.")
        return

    df, col_org, col_cuit, col_fecha = preparar_df(df_flujo)

    tab_frag, tab_unico, tab_rafaga, tab_hhi, tab_fantasmas = st.tabs([
        "🔪 Fragmentación",
        "🔒 Proveedor Único",
        "⚡ Ráfagas",
        "📐 HHI",
        "👻 Sin cobro TGN",
    ])

    with tab_frag:
        st.subheader("🔪 Fragmentación de contratos")
        st.caption("Organismos que dividen compras para evitar el umbral de licitación pública ($10M ARS)")
        df_f = detectar_fragmentacion(df_flujo, exportar_df=True)
        if df_f.empty:
            st.success("No se detectaron patrones de fragmentación")
        else:
            st.warning(f"{len(df_f)} organismos con posible fragmentación")
            st.dataframe(df_f, use_container_width=True, hide_index=True)

    with tab_unico:
        st.subheader("🔒 Proveedor único por organismo")
        st.caption("Organismos que adjudican siempre (o casi) al mismo CUIT — señal de captura")
        df_u = detectar_proveedor_unico(df_flujo, exportar_df=True)
        if df_u.empty:
            st.success("No se detectaron organismos con proveedor único")
        else:
            st.warning(f"{len(df_u)} organismos con concentración alta")
            st.dataframe(df_u, use_container_width=True, hide_index=True)

    with tab_rafaga:
        st.subheader("⚡ Ráfagas de adjudicación")
        st.caption("Proveedores con 3+ adjudicaciones en 7 días — aceleración sospechosa")
        df_r = detectar_rafaga(df_flujo, exportar_df=True)
        if df_r.empty:
            st.success("No se detectaron ráfagas")
        else:
            st.warning(f"{len(df_r)} proveedores con ráfaga detectada")
            fig_r = px.scatter(
                df_r,
                x="fecha_inicio",
                y="adj_en_ventana",
                size="monto_total",
                color="nivel_alerta",
                hover_name="cuit",
                title="Ráfagas detectadas (tamaño = monto total)",
                color_discrete_map={
                    "🔴 Crítico": "#e05252",
                    "🟡 Moderado": "#f0a500",
                    "🟠 Leve": "#f07850",
                },
            )
            st.plotly_chart(fig_r, use_container_width=True)
            st.dataframe(df_r, use_container_width=True, hide_index=True)

    with tab_hhi:
        st.subheader("📐 Índice HHI de concentración por organismo")
        st.caption("HHI > 2500: alta concentración (riesgo de captura) | HHI < 1500: mercado competitivo")
        df_hhi = analisis_hhi(df_flujo, top_n=30, exportar_df=True)
        if df_hhi.empty:
            st.info("Sin datos suficientes para calcular HHI")
        else:
            fig_hhi = px.bar(
                df_hhi.head(20),
                x="hhi", y="organismo",
                orientation="h",
                title="Top 20 organismos por índice HHI",
                color="hhi",
                color_continuous_scale=[[0,"#faf5ff"],[0.5,"#dc2626"],[1,"#991b1b"]],
                range_color=[0, 10000],
            )
            fig_hhi.add_vline(x=2500, line_dash="dash", line_color="red",
                              annotation_text="Alta concentración")
            fig_hhi.add_vline(x=1500, line_dash="dash", line_color="orange",
                              annotation_text="Moderada")
            fig_hhi.update_layout(showlegend=False, yaxis_title="")
            st.plotly_chart(fig_hhi, use_container_width=True)
            st.dataframe(df_hhi, use_container_width=True, hide_index=True)

    with tab_fantasmas:
        st.subheader("👻 Adjudicados sin cobro en TGN")
        st.caption("Proveedores que ganaron licitaciones pero no registran cobro en TGN")
        df_fan = detectar_fantasmas(df_flujo, exportar_df=True)
        if df_fan.empty:
            st.success("Todos los proveedores adjudicados registran cobro en TGN")
        else:
            st.warning(f"{len(df_fan)} adjudicados sin registro de cobro en TGN")
            st.dataframe(df_fan, use_container_width=True, hide_index=True)

    # ── Fuentes de datos scrapeadas ──────────────────────────────
    st.divider()
    st.subheader("🌐 Fuentes de datos monitoreadas diariamente")
    st.caption("El robot de scraping consulta estas fuentes cada día hábil de forma automática.")

    fuentes = [
        {
            "icon": "📰",
            "nombre": "Boletín Oficial de la República Argentina (BORA)",
            "url": "https://www.boletinoficial.gob.ar",
            "descripcion": "Sección Tercera — Avisos Oficiales. Se extraen las adjudicaciones publicadas en el día, incluyendo organismo, proveedor, monto y enlace al aviso original.",
            "endpoints": [
                "https://www.boletinoficial.gob.ar/seccion/tercera",
                "https://www.boletinoficial.gob.ar/detalleAviso/tercera/{id}/{fecha}",
                "https://www.boletinoficial.gob.ar/pdf/aviso/tercera/{id}/{fecha}",
            ]
        },
        {
            "icon": "🛒",
            "nombre": "Comprar.gob.ar — Portal de Compras Públicas",
            "url": "https://comprar.gob.ar",
            "descripcion": "Portal oficial de contrataciones del Estado Nacional. Se consultan los procesos de compra recientes para cruzar con las adjudicaciones BORA y verificar el número de proceso licitatorio.",
            "endpoints": [
                "https://comprar.gob.ar/Compras.aspx?qs=W1HXHGHtH10=",
            ]
        },
        {
            "icon": "💰",
            "nombre": "Presupuesto Abierto — Tesorería General de la Nación (TGN)",
            "url": "https://www.presupuestoabierto.gob.ar",
            "descripcion": "Portal de datos abiertos del Ministerio de Economía. Se descarga el crédito ejecutado por beneficiario (CUIT) para confirmar qué proveedores cobraron efectivamente del Estado.",
            "endpoints": [
                "https://www.presupuestoabierto.gob.ar/sici/datos-abiertos-download?file=credito-{año}.csv",
                "https://www.presupuestoabierto.gob.ar/sici/rest-api/credito/ejecutado?anio={año}",
            ]
        },
        {
            "icon": "📊",
            "nombre": "datos.gob.ar — Portal de Datos Abiertos",
            "url": "https://datos.gob.ar",
            "descripcion": "Portal nacional de datos abiertos. Se usan como fuente alternativa los datasets de crédito ejecutado de la Secretaría de Hacienda cuando la API de Presupuesto Abierto no responde.",
            "endpoints": [
                "https://datos.gob.ar/dataset/sspre-presupuesto-administracion-publica-nacional-{año}",
            ]
        },
    ]

    for f in fuentes:
        with st.expander(f"{f['icon']}  **{f['nombre']}** — {f['url']}"):
            st.markdown(f"**Descripción:** {f['descripcion']}")
            st.markdown("**Endpoints consultados:**")
            for ep in f["endpoints"]:
                st.code(ep, language="text")
            st.markdown(f"[🔗 Abrir sitio]({f['url']})")


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
@st.dialog("💛 Apoyar este proyecto", width="small")
def modal_apoyar():
    st.markdown("Completá tus datos y te enviamos la información para realizar tu donación.")
    col1, col2 = st.columns(2)
    with col1:
        st.text_input("Nombre", placeholder="Tu nombre", key="apoyar_nombre")
    with col2:
        st.text_input("Apellido", placeholder="Tu apellido", key="apoyar_apellido")
    st.text_input("Email", placeholder="tu@email.com", key="apoyar_email")
    origen = st.selectbox("¿Desde dónde donás?", ["— Seleccioná —", "Argentina", "Exterior"], key="apoyar_origen")
    if st.button("Ver datos para transferir →", type="primary", use_container_width=True):
        nombre   = st.session_state.get("apoyar_nombre", "").strip()
        apellido = st.session_state.get("apoyar_apellido", "").strip()
        email    = st.session_state.get("apoyar_email", "").strip()
        if nombre and apellido and email and origen != "— Seleccioná —":
            _registrar_donacion(nombre, apellido, email, origen)
        if origen == "Argentina":
            st.markdown("### 🇦🇷 Datos para transferencia desde Argentina")
            st.markdown("Usá alguno de estos alias desde tu homebanking o billetera virtual:")
            st.divider()
            st.markdown("#### 💰 En Pesos")
            c1, c2 = st.columns(2)
            with c1:
                st.caption("ALIAS"); st.code("ALGORIT.MONTE.PESOS")
                st.caption("TITULAR"); st.code("Vicente Humberto Monteverde")
            with c2:
                st.caption("TIPO"); st.code("Caja de Ahorro · Pesos")
            st.markdown("#### 💵 En Dólares")
            c3, c4 = st.columns(2)
            with c3:
                st.caption("ALIAS"); st.code("ALGO.MONTE.DOLARES")
                st.caption("TITULAR"); st.code("Vicente Humberto Monteverde")
            with c4:
                st.caption("TIPO"); st.code("Caja de Ahorro · Dólares")
            st.success("¡Muchas gracias por apoyar la transparencia! 🙏")
        elif origen == "Exterior":
            st.markdown("### 🌐 Wire Transfer — International donation")
            st.markdown("Please use the following wire transfer details:")
            st.divider()
            for label, val in [
                ("BANK",           "Banco Santander Montevideo"),
                ("BENEFICIARY",    "Vicente Humberto Monteverde"),
                ("ADDRESS",        "Av. Directorio 3024-PB-DTO 04"),
                ("ACCOUNT TYPE",   "Savings Account · USD"),
                ("ACCOUNT NUMBER", "005200183500"),
                ("SWIFT / BIC",    "BSCHUYMM"),
            ]:
                st.caption(label); st.code(val)
            st.success("Thank you for supporting transparency! 🙏")
        else:
            st.warning("Seleccioná desde dónde donás")

def _seccion_estadisticas():
    """Panel de estadísticas — protegido por clave admin."""
    st.title("📊 Estadísticas del Sistema")

    ADMIN_KEY = os.getenv("ADMIN_KEY", "monitor2026")
    clave = st.text_input("🔑 Clave de acceso", type="password", placeholder="Ingresá la clave admin")

    if not clave:
        st.info("Ingresá la clave para ver las estadísticas.")
        return
    if clave != ADMIN_KEY:
        st.error("❌ Clave incorrecta.")
        return

    stats = _get_stats()
    if stats is None:
        st.warning("⚠️ DATABASE_URL no configurada — estadísticas no disponibles.")
        return
    if "error" in stats:
        st.error(f"Error DB: {stats['error']}")
        return

    # ── Métricas ──────────────────────────────────────────────
    col1, col2, col3 = st.columns(3)
    col1.metric("👁️ Visitas totales",        stats["total"])
    col2.metric("🗂️ Secciones distintas",    len(stats["por_seccion"]))
    col3.metric("💛 Consultas donación",      len(stats["donaciones"]))

    st.divider()

    # ── Visitas por día ───────────────────────────────────────
    if stats["por_dia"]:
        st.subheader("📅 Visitas por día (últimos 14 días)")
        import pandas as pd
        df_dia = pd.DataFrame(stats["por_dia"])
        df_dia.columns = ["Día", "Visitas"]
        import plotly.express as px
        fig = px.bar(df_dia, x="Día", y="Visitas", color_discrete_sequence=["#a78bfa"])
        fig.update_layout(plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                          font_color="#e2e8f0", xaxis_title="", yaxis_title="Visitas")
        st.plotly_chart(fig, use_container_width=True)

    # ── Visitas por sección ───────────────────────────────────
    if stats["por_seccion"]:
        st.subheader("🗂️ Visitas por sección")
        df_sec = pd.DataFrame(stats["por_seccion"])
        df_sec.columns = ["Sección", "Visitas"]
        st.dataframe(df_sec, use_container_width=True, hide_index=True)

    st.divider()

    # ── Consultas de donación ─────────────────────────────────
    st.subheader("💛 Consultas de Donación")
    if stats["donaciones"]:
        df_don = pd.DataFrame(stats["donaciones"])
        df_don = df_don[["created_at", "nombre", "apellido", "email", "pais"]]
        df_don.columns = ["Fecha", "Nombre", "Apellido", "Email", "País"]
        st.dataframe(df_don, use_container_width=True, hide_index=True)
    else:
        st.info("Aún no hay consultas de donación registradas.")


def main():
    df_flujo, df_adj, df_tgn, n_archivos = cargar_datos()

    seccion = sidebar()

    # Mostrar modal si fue activado
    if st.session_state.get("mostrar_modal_apoyar"):
        st.session_state["mostrar_modal_apoyar"] = False
        modal_apoyar()

    if df_flujo.empty and df_adj.empty:
        st.error(
            "⚠️ No se encontraron datos en `data/`. "
            "Ejecutá `python diario.py` para generar el primer reporte."
        )
        st.code("python diario.py", language="bash")
        return

    st.caption(
        f"📁 {n_archivos} archivos cargados · "
        f"{len(df_flujo):,} registros de flujo · "
        f"Actualizado: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )

    if seccion == "📄 Contratos":
        seccion_contratos(df_flujo, df_tgn)
    elif seccion == "🏛️ Organismos":
        seccion_organismos(df_flujo)
    elif seccion == "🏢 Proveedores":
        seccion_proveedores(df_flujo, df_tgn)
    elif seccion == "🔍 Monitor":
        seccion_monitor(df_flujo)
    elif seccion == "📊 Estadísticas":
        _seccion_estadisticas()


if __name__ == "__main__":
    main()
