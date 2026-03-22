import streamlit as st
import pandas as pd
import plotly.express as px
import os
from datetime import datetime

# ===============================
# CONFIGURACIÓN Y ESTILO
# ===============================
st.set_page_config(page_title="Monitor de Gran Corrupción", layout="wide")

# Rutas compatibles con Docker y Local
# En dashboard.py
DATA_DIR = "/app/gob_docker/data" if os.path.exists("/app") else "gob_docker/data"


# ===============================
# FUNCIONES DE GESTIÓN DE ARCHIVOS MENSUALES
# ===============================
def obtener_meses_disponibles():
    """Escanea el directorio de datos y retorna una lista de meses disponibles"""
    if not os.path.exists(DATA_DIR):
        return []

    meses = []
    for item in os.listdir(DATA_DIR):
        item_path = os.path.join(DATA_DIR, item)
        # Buscar carpetas con formato YYYY-MM (ej: 2026-01, 2026-02)
        if os.path.isdir(item_path) and len(item) == 7 and item[4] == "-":
            meses.append(item)

    return sorted(meses, reverse=True)  # Más reciente primero


def obtener_archivos_del_mes(mes):
    """Retorna todos los archivos .xlsx de un mes específico"""
    mes_dir = os.path.join(DATA_DIR, mes)
    if not os.path.exists(mes_dir):
        return []

    archivos = [f for f in os.listdir(mes_dir) if f.endswith(".xlsx")]
    return sorted(archivos, reverse=True)


def formatear_nombre_mes(mes_codigo):
    """Convierte 2026-01 a 'Enero 2026'"""
    meses_esp = {
        "01": "Enero",
        "02": "Febrero",
        "03": "Marzo",
        "04": "Abril",
        "05": "Mayo",
        "06": "Junio",
        "07": "Julio",
        "08": "Agosto",
        "09": "Septiembre",
        "10": "Octubre",
        "11": "Noviembre",
        "12": "Diciembre",
    }

    try:
        year, mes = mes_codigo.split("-")
        return f"{meses_esp[mes]} {year}"
    except:
        return mes_codigo


# ===============================
# TRATAMIENTO DE DATOS (COMPATIBILIDAD SEGURA)
# ===============================
def cargar_y_limpiar(ruta):
    df = pd.read_excel(ruta)

    # Mapeo de nombres antiguos a nuevos para compatibilidad histórica
    mapeo = {
        "indice_total": "indice_fenomeno_corruptivo",
        "nivel_riesgo": "nivel_riesgo_teorico",
        "origen": "transferencia",
    }

    # RENOMBRADO SEGURO
    for viejo, nuevo in mapeo.items():
        if viejo in df.columns and nuevo not in df.columns:
            df = df.rename(columns={viejo: nuevo})

    # Eliminar duplicados
    df = df.loc[:, ~df.columns.duplicated()]

    # Asegurar columnas críticas
    if "indice_fenomeno_corruptivo" not in df.columns:
        df["indice_fenomeno_corruptivo"] = 0.0
    if "tipo_decision" not in df.columns:
        df["tipo_decision"] = "No identificado"

    return df


# ===============================
# SIDEBAR Y NAVEGACIÓN
# ===============================
st.sidebar.header("⚙️ Configuración")

st.sidebar.divider()
st.sidebar.subheader("📑 Navegación")
pagina = st.sidebar.radio(
    "Seleccione una sección:",
    ["📊 Dashboard Principal", "📖 Instructivo de Uso"],
    label_visibility="collapsed",
)

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

# ===============================
# PÁGINA DE INSTRUCTIVO
# ===============================
if pagina == "📖 Instructivo de Uso":
    st.title("📖 Instructivo de Uso del Dashboard")
    st.markdown("### Guía completa para utilizar el Monitor de Fenómenos Corruptivos")
    st.warning("""
⚠️ **Nota:** Esta herramienta es de carácter experimental y académico. Los datos provienen de fuentes públicas oficiales del Estado argentino. Los resultados son indicadores algorítmicos de riesgo — no implican juicio de valor, acusación ni determinación de responsabilidad sobre ninguna empresa, organismo o persona. El objetivo es promover la transparencia y el debate informado sobre el gasto público.
""")
    st.divider()

    col_inst1, col_inst2 = st.columns([2, 1])

    with col_inst1:
        st.markdown("""
        ## 📘 Contenido del Instructivo

        ✅ **Introducción a la Teoría de Monteverde**  
        ✅ **Requisitos Previos**  
        ✅ **Estructura de Archivos**  
        ✅ **Guía de Ejecución**  
        ✅ **Componentes del Dashboard**  
        ✅ **Los 7 Escenarios Corruptivos**  
        ✅ **Interpretación de Resultados**  
        ✅ **Recursos Adicionales**
        """)

    with col_inst2:
        st.info("""
        **📄 Formato:** Word (.docx)
        **🎯 Público:** Técnicos y no técnicos
        **📅 Actualización:** Vigente
        """)

    st.divider()

    instructivo_path = "instructivo_dashboard.docx"
    if os.path.exists(instructivo_path):
        with open(instructivo_path, "rb") as file:
            st.download_button(
                label="⬇️ DESCARGAR INSTRUCTIVO COMPLETO (Word)",
                data=file,
                file_name="Instructivo_Monitor_Fenomenos_Corruptivos.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                use_container_width=True,
            )
    else:
        st.warning("⚠️ El instructivo no está disponible.")

    st.stop()

# ===============================
# DASHBOARD PRINCIPAL - SELECCIÓN MENSUAL
# ===============================

meses_disponibles = obtener_meses_disponibles()

if not meses_disponibles:
    st.error("""
    No se encontraron datos organizados por mes.

    **Estructura esperada:**
    ```
    data/
    ├── 2026-01/
    │   ├── reporte_fenomenos_20260130.xlsx
    │   └── reporte_fenomenos_20260131.xlsx
    ├── 2026-02/
    │   └── reporte_fenomenos_20260201.xlsx
    ```

    Ejecute 'diario.py' o migre sus datos con el script proporcionado.
    """)
    st.stop()

# Selector de mes
st.sidebar.divider()
st.sidebar.subheader("📅 Selección de Período")

mes_seleccionado = st.sidebar.selectbox(
    "Mes a analizar", meses_disponibles, format_func=formatear_nombre_mes
)

archivos_del_mes = obtener_archivos_del_mes(mes_seleccionado)

if not archivos_del_mes:
    st.error(
        f"No se encontraron reportes para {formatear_nombre_mes(mes_seleccionado)}"
    )
    st.stop()

archivo_selec = st.sidebar.selectbox(
    "Reporte Diario",
    archivos_del_mes,
    format_func=lambda x: x.replace("reporte_fenomenos_", "").replace(".xlsx", ""),
)

ruta_completa = os.path.join(DATA_DIR, mes_seleccionado, archivo_selec)
df = cargar_y_limpiar(ruta_completa)

st.sidebar.divider()
st.sidebar.info(f"""
**Período:** {formatear_nombre_mes(mes_seleccionado)}  
**Total reportes:** {len(archivos_del_mes)} días
""")

# ===============================
# HEADER Y MÉTRICAS
# ===============================
st.title("⚖️ Monitor de Fenómenos Corruptivos Legales")
st.markdown("### Implementación de la Teoría del **Ph.D. Vicente Humberto Monteverde**")

df_detectados = df[df["tipo_decision"] != "No identificado"]

m1, m2, m3, m4 = st.columns(4)
m1.metric("Normas Analizadas", len(df))
m2.metric("Fenómenos Detectados", len(df_detectados))
m3.metric("Riesgo Máximo", f"{df['indice_fenomeno_corruptivo'].max()}/10")
fecha_label = archivo_selec.split("_")[-1].split(".")[0]
m4.metric("Fecha del Reporte", fecha_label)

st.divider()

# ===============================
# VISUALIZACIÓN INTERACTIVA
# ===============================
col_g1, col_g2 = st.columns(2)

with col_g1:
    st.write("### 📊 Intensidad por Escenario Teórico")
    if not df_detectados.empty:
        fig_bar = px.bar(
            df_detectados,
            x="indice_fenomeno_corruptivo",
            y="tipo_decision",
            color="nivel_riesgo_teorico",
            orientation="h",
            color_discrete_map={
                "Alto": "#EF553B",
                "Medio": "#FECB52",
                "Bajo": "#636EFA",
            },
            labels={
                "indice_fenomeno_corruptivo": "Índice de Intensidad (0-10)",
                "tipo_decision": "Escenario de la Teoría",
            },
        )
        st.plotly_chart(fig_bar, use_container_width=True)
    else:
        st.info("No hay fenómenos detectados en este reporte.")

with col_g2:
    st.write("### 💸 Sectores de Transferencia Regresiva")
    if not df_detectados.empty:
        fig_pie = px.pie(
            df_detectados,
            names="transferencia",
            hole=0.4,
            title="Distribución de Impacto Económico",
        )
        st.plotly_chart(fig_pie, use_container_width=True)

# ===============================
# TABLA DE AUDITORÍA
# ===============================
st.write("### 🔍 Explorador de Decisiones Estatales")
cols_visibles = [
    "fecha",
    "tipo_decision",
    "transferencia",
    "indice_fenomeno_corruptivo",
    "nivel_riesgo_teorico",
    "link",
]
df_display = df[[c for c in cols_visibles if c in df.columns]]

st.dataframe(
    df_display,
    use_container_width=True,
    column_config={
        "link": st.column_config.LinkColumn("Norma Original"),
        "indice_fenomeno_corruptivo": st.column_config.ProgressColumn(
            "Intensidad", min_value=0, max_value=10
        ),
    },
)

# ===============================
# FUNDAMENTO CIENTÍFICO
# ===============================
st.divider()
with st.expander("🔬 Fundamento Científico y Matriz XAI", expanded=False):
    st.markdown("""
    #### Núcleo de la Teoría
    La corrupción muta y se diversifica, volviéndose **legal** a través de decisiones discrecionales del Estado. 
    Estos **fenómenos corruptivos** se basan en la legalidad pero producen situaciones de desigualdad económica e injusticia.

    #### Los 7 Escenarios Críticos Analizados:
    1. **Privatizaciones Subvaluadas**: Transferencia de patrimonio estatal a privados
    2. **Contratos Públicos**: Continuidad de obras ineficientes o con sobreprecios
    3. **Tarifas y Devaluación**: Compensaciones discrecionales a concesionarias
    4. **Servicios Públicos**: Ajustes tarifarios sin considerar el ingreso salarial
    5. **Salud y Educación**: Aumentos autorizados en servicios básicos privados
    6. **Cálculo Previsional**: Transferencia de ingresos de jubilados hacia el Estado (**Peso 10.0**)
    7. **Traslación Impositiva**: Transferencia de impuestos empresariales a los consumidores
    """)
    st.info(
        "Referencia: Monteverde, V. H. (2020). Great corruption – theory of corrupt phenomena. Journal of Financial Crime."
    )

st.caption(
    f"Sistema validado - Ph.D. Vicente Humberto Monteverde | Ejecución: {datetime.now().strftime('%d/%m/%Y %H:%M')}"
)

# ===============================
# DESCARGA DEL ARTÍCULO
# ===============================
st.divider()

col_art1, col_art2 = st.columns(2)

with col_art1:
    articulo_path = "articulo_monteverde_español.docx"
    if os.path.exists(articulo_path):
        with open(articulo_path, "rb") as file:
            st.download_button(
                label="📄 Descargar Artículo Original (Word)",
                data=file,
                file_name="articulo_monteverde_español.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                use_container_width=True,
            )
    else:
        st.warning("El artículo no está disponible")

with col_art2:
    articulo_pdf_path = "articulo_monteverde_español.pdf"
    if os.path.exists(articulo_pdf_path):
        with open(articulo_pdf_path, "rb") as file:
            st.download_button(
                label="📄 Descargar Artículo Original (PDF)",
                data=file,
                file_name="articulo_monteverde_español.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
    else:
        st.warning("El artículo PDF no está disponible")

# ===============================
# ANÁLISIS AVANZADOS
# ===============================
st.divider()
st.markdown("## 📈 Análisis Avanzados de Fenómenos Corruptivos")

# 1. ANÁLISIS TEMPORAL
st.write("### ⏱️ Análisis de Acumulación Temporal de Fenómenos")
col_temp1, col_temp2 = st.columns(2)

with col_temp1:
    if not df_detectados.empty:
        acumulacion = (
            df_detectados.groupby("tipo_decision").size().reset_index(name="cantidad")
        )
        acumulacion = acumulacion.sort_values("cantidad", ascending=False)

        fig_acum = px.bar(
            acumulacion,
            x="cantidad",
            y="tipo_decision",
            orientation="h",
            title="Frecuencia de Fenómenos por Escenario",
            labels={"cantidad": "Cantidad de Casos", "tipo_decision": "Escenario"},
            color="cantidad",
            color_continuous_scale="Reds",
        )
        st.plotly_chart(fig_acum, use_container_width=True)

with col_temp2:
    if not df_detectados.empty:
        intensidad_prom = (
            df_detectados.groupby("tipo_decision")["indice_fenomeno_corruptivo"]
            .mean()
            .reset_index()
        )
        intensidad_prom = intensidad_prom.sort_values(
            "indice_fenomeno_corruptivo", ascending=False
        )

        fig_int = px.bar(
            intensidad_prom,
            x="indice_fenomeno_corruptivo",
            y="tipo_decision",
            orientation="h",
            title="Intensidad Promedio por Escenario",
            labels={
                "indice_fenomeno_corruptivo": "Intensidad Promedio",
                "tipo_decision": "Escenario",
            },
            color="indice_fenomeno_corruptivo",
            color_continuous_scale="Oranges",
        )
        st.plotly_chart(fig_int, use_container_width=True)

# 2. MATRIZ DE RIESGO
st.write("### 🎯 Matriz de Riesgo: Intensidad vs Transferencia")

if not df_detectados.empty:
    col_matriz1, col_matriz2 = st.columns([2, 1])

    with col_matriz1:
        fig_scatter = px.scatter(
            df_detectados,
            x="indice_fenomeno_corruptivo",
            y="transferencia",
            color="nivel_riesgo_teorico",
            size="indice_fenomeno_corruptivo",
            hover_data=["tipo_decision"],
            color_discrete_map={
                "Alto": "#EF553B",
                "Medio": "#FECB52",
                "Bajo": "#636EFA",
            },
            title="Distribución de Fenómenos",
            labels={
                "indice_fenomeno_corruptivo": "Índice de Intensidad",
                "transferencia": "Dirección de Transferencia",
            },
        )
        fig_scatter.update_layout(height=500)
        st.plotly_chart(fig_scatter, use_container_width=True)

    with col_matriz2:
        st.markdown("#### 📊 Estadísticas por Transferencia")
        for transferencia in df_detectados["transferencia"].unique():
            df_trans = df_detectados[df_detectados["transferencia"] == transferencia]
            st.markdown(f"**{transferencia}:**")
            st.metric("Casos", len(df_trans))
            st.metric(
                "Intensidad Promedio",
                f"{df_trans['indice_fenomeno_corruptivo'].mean():.1f}/10",
            )
            st.divider()

# 3. CONCENTRACIÓN DE RIESGO
st.write("### 🔥 Concentración de Riesgo por Nivel")

if not df_detectados.empty:
    col_conc1, col_conc2, col_conc3 = st.columns(3)

    riesgo_stats = (
        df_detectados.groupby("nivel_riesgo_teorico")
        .agg({"indice_fenomeno_corruptivo": ["count", "mean", "sum"]})
        .reset_index()
    )
    riesgo_stats.columns = ["nivel_riesgo", "cantidad", "promedio", "total"]

    with col_conc1:
        if "Alto" in riesgo_stats["nivel_riesgo"].values:
            alto = riesgo_stats[riesgo_stats["nivel_riesgo"] == "Alto"].iloc[0]
            st.metric(
                "🔴 Riesgo ALTO",
                f"{int(alto['cantidad'])} casos",
                delta=f"Intensidad: {alto['promedio']:.1f}",
            )
        else:
            st.metric("🔴 Riesgo ALTO", "0 casos")

    with col_conc2:
        if "Medio" in riesgo_stats["nivel_riesgo"].values:
            medio = riesgo_stats[riesgo_stats["nivel_riesgo"] == "Medio"].iloc[0]
            st.metric(
                "🟡 Riesgo MEDIO",
                f"{int(medio['cantidad'])} casos",
                delta=f"Intensidad: {medio['promedio']:.1f}",
            )
        else:
            st.metric("🟡 Riesgo MEDIO", "0 casos")

    with col_conc3:
        if "Bajo" in riesgo_stats["nivel_riesgo"].values:
            bajo = riesgo_stats[riesgo_stats["nivel_riesgo"] == "Bajo"].iloc[0]
            st.metric(
                "🔵 Riesgo BAJO",
                f"{int(bajo['cantidad'])} casos",
                delta=f"Intensidad: {bajo['promedio']:.1f}",
            )
        else:
            st.metric("🔵 Riesgo BAJO", "0 casos")

# 4. RECOMENDACIONES
st.write("### 💡 Recomendaciones Basadas en la Teoría")

if not df_detectados.empty:
    col_rec1, col_rec2 = st.columns(2)

    with col_rec1:
        st.markdown("#### 🎯 Escenarios de Mayor Riesgo")
        top_riesgo = (
            df_detectados.groupby("tipo_decision")["indice_fenomeno_corruptivo"]
            .mean()
            .sort_values(ascending=False)
            .head(3)
        )

        for i, (escenario, intensidad) in enumerate(top_riesgo.items(), 1):
            st.markdown(f"{i}. **{escenario}**: {intensidad:.1f}/10")

    with col_rec2:
        st.markdown("#### 📊 Direcciones de Transferencia")
        trans_dist = df_detectados["transferencia"].value_counts()

        for transferencia, cantidad in trans_dist.items():
            porcentaje = (cantidad / len(df_detectados) * 100)
            st.markdown(f"• **{transferencia}**: {cantidad} casos ({porcentaje:.1f}%)")

    st.info("""
    **Según la teoría de Monteverde**, estos fenómenos corruptivos son **legales** pero generan 
    **transferencias regresivas de ingresos**, afectando la distribución económica y la equidad social.
    """)
