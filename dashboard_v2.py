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
import unicodedata
from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

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
    page_title="Monitor de Fenómenos Corruptivos",
    page_icon="🏛️",
    layout="wide",
    initial_sidebar_state="expanded",
)

DATA_DIR = os.path.join(os.getcwd(), "data")

# ─────────────────────────────────────────
# ESTILOS
# ─────────────────────────────────────────
st.markdown("""
<style>
    .metric-card {
        background: #1e2330;
        border-radius: 8px;
        padding: 16px 20px;
        border-left: 4px solid #4f8ef7;
        margin-bottom: 8px;
    }
    .metric-card.rojo  { border-left-color: #e05252; }
    .metric-card.verde { border-left-color: #52c07a; }
    .metric-card.naranja { border-left-color: #f0a500; }
    .metric-label { font-size: 12px; color: #8899aa; margin-bottom: 4px; }
    .metric-value { font-size: 26px; font-weight: 700; color: #ffffff; }
    .metric-sub   { font-size: 12px; color: #8899aa; margin-top: 4px; }
    .tag-rojo    { background:#e05252; color:#fff; padding:2px 8px; border-radius:4px; font-size:11px; }
    .tag-naranja { background:#f0a500; color:#fff; padding:2px 8px; border-radius:4px; font-size:11px; }
    .tag-verde   { background:#52c07a; color:#fff; padding:2px 8px; border-radius:4px; font-size:11px; }
    .seccion-titulo { font-size:18px; font-weight:700; margin:20px 0 8px; }
    .diferencial { background:#0d3349; border-left:4px solid #4f8ef7;
                   padding:10px 14px; border-radius:6px; font-size:13px; margin:8px 0; }
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
    st.sidebar.image(
        "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1a/24701-nature-natural-beauty.jpg/1px-1px.jpg",
        width=1,
    )
    st.sidebar.title("🏛️ Monitor")
    st.sidebar.caption("Fenómenos Corruptivos — Monteverde (2020)")
    st.sidebar.divider()

    seccion = st.sidebar.radio(
        "Sección",
        ["📄 Contratos", "🏛️ Organismos", "🏢 Proveedores", "🔍 Monitor"],
        label_visibility="collapsed",
    )
    st.sidebar.divider()
    st.sidebar.caption(
        "**Diferencial único:**\n\n"
        "Este sistema muestra quién **cobró** (TGN), "
        "no solo quién ganó la licitación.\n\n"
        "*Cruce BORA → Comprar → TGN*"
    )
    st.sidebar.caption(
        "Ref: Monteverde, V.H. (2020). "
        "*Journal of Financial Crime*, Vol. 28 No. 2."
    )
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
                color_continuous_scale="Reds",
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
            color_continuous_scale="Blues",
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
            color_continuous_scale="Oranges",
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
                    color_continuous_scale="Greens",
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
                color_continuous_scale="Reds",
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
        "👻 Fantasmas",
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
                color_continuous_scale="Reds",
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
        st.subheader("👻 Proveedores fantasma")
        st.caption("Adjudicados que no aparecen en TGN — ganaron pero no cobraron")
        df_fan = detectar_fantasmas(df_flujo, exportar_df=True)
        if df_fan.empty:
            st.success("Todos los proveedores adjudicados aparecen en TGN")
        else:
            st.warning(f"{len(df_fan)} proveedores sin registro de cobro en TGN")
            st.dataframe(df_fan, use_container_width=True, hide_index=True)


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
def main():
    df_flujo, df_adj, df_tgn, n_archivos = cargar_datos()

    seccion = sidebar()

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


if __name__ == "__main__":
    main()
