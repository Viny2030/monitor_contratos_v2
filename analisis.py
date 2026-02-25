import os
import unicodedata
import re
import pandas as pd
from datetime import datetime

# --- CONFIGURACIÓN DE RUTAS DINÁMICAS ---
BASE_PATH = os.getcwd()

# En Railway /app/data puede no ser escribible; usamos /tmp como fallback
if os.path.exists("/app"):
    DATA_DIR = "/app/data"
    FALLBACK_DIR = "/tmp/data"
else:
    DATA_DIR = os.path.join(BASE_PATH, "data")
    FALLBACK_DIR = DATA_DIR

for d in [DATA_DIR, FALLBACK_DIR]:
    try:
        os.makedirs(d, exist_ok=True)
    except Exception as e:
        print(f"⚠️ No se pudo crear {d}: {e}")


# ═══════════════════════════════════════════════════════════════════
# MATRIZ TEÓRICA - Ph.D. Vicente Humberto Monteverde
# Detecta fenómenos corruptivos por keywords en texto libre
# ═══════════════════════════════════════════════════════════════════
MATRIZ_TEORICA = {
    "Privatización / Concesión": {
        "keywords": ["concesion", "privatizacion", "venta de pliegos", "subvaluacion"],
        "transferencia": "Estado a Privados",
        "peso": 9.0,
    },
    "Obra Pública / Contratos": {
        "keywords": ["obra publica", "licitacion", "contratacion directa", "sobreprecio", "redeterminacion"],
        "transferencia": "Estado a Empresas",
        "peso": 8.5,
    },
    "Tarifas Servicios Públicos": {
        "keywords": ["cuadro tarifario", "aumento de tarifa", "revision tarifaria", "peaje"],
        "transferencia": "Usuarios a Concesionarias",
        "peso": 7.5,
    },
    "Precios de Consumo Regulados": {
        "keywords": ["precios justos", "canasta basica", "viveres", "alimento"],
        "transferencia": "Consumidores a Productores",
        "peso": 6.5,
    },
    "Salarios y Paritarias": {
        "keywords": ["paritaria", "salario minimo", "ajuste salarial", "convenio colectivo"],
        "transferencia": "Asalariados a Empleadores",
        "peso": 5.5,
    },
    "Jubilaciones / Pensiones": {
        "keywords": ["movilidad jubilatoria", "haber minimo", "anses", "ajuste previsional"],
        "transferencia": "Jubilados al Estado",
        "peso": 10.0,
    },
    "Traslado de Impuestos": {
        "keywords": ["iva", "ingresos brutos", "doble imposicion", "presion tributaria"],
        "transferencia": "Contribuyentes al Estado",
        "peso": 9.5,
    },
}


# ═══════════════════════════════════════════════════════════════════
# MATRIZ DE RIESGO LICITATORIO - Ph.D. Vicente Humberto Monteverde
# Detecta patrones de irregularidad en el flujo BORA→Comprar→TGN
# Cada indicador tiene peso independiente; el índice final es la
# suma acumulada normalizada a escala 0–10.
# ═══════════════════════════════════════════════════════════════════
MATRIZ_LICITACIONES = {

    "Contratación Directa": {
        "descripcion": "Se usó contratación directa en lugar de licitación pública, "
                       "reduciendo la competencia y la transparencia.",
        "keywords_tipo": ["contratacion directa", "contratacion por excepcion",
                          "compulsa abreviada", "cdi", "cdr"],
        "peso": 3.0,
        "umbral": None,
    },

    "Proveedor Único": {
        "descripcion": "El mismo CUIT aparece adjudicado en múltiples procesos "
                       "del mismo día, indicando posible concentración.",
        "keywords_tipo": None,
        "peso": 2.5,
        "umbral": 2,          # apariciones del mismo CUIT en el mismo día
    },

    "Monto Límite": {
        "descripcion": "El monto adjudicado es cercano (±10%) al umbral que "
                       "obliga a licitación pública ($10M ARS), sugiriendo "
                       "fraccionamiento para evitar el procedimiento.",
        "keywords_tipo": None,
        "peso": 2.5,
        "umbral": 10_000_000,  # ARS — umbral licitación pública
        "tolerancia": 0.10,    # ±10%
    },

    "Velocidad Adjudicación": {
        "descripcion": "El proceso fue adjudicado el mismo día de su publicación "
                       "en BORA, sin tiempo razonable para que oferentes compitan.",
        "keywords_tipo": None,
        "peso": 1.5,
        "umbral": 0,           # días entre publicación y adjudicación
    },

    "Proveedor Multi-Organismo": {
        "descripcion": "El mismo CUIT cobra en múltiples organismos distintos "
                       "el mismo día en TGN, patrón atípico de concentración.",
        "keywords_tipo": None,
        "peso": 1.5,
        "umbral": 2,           # organismos distintos con el mismo CUIT en TGN
    },
}

# Peso máximo posible (suma de todos los pesos) — para normalizar a 0–10
_PESO_MAX_LICITACIONES = sum(v["peso"] for v in MATRIZ_LICITACIONES.values())


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

def limpiar_texto_curado(texto):
    """Normaliza texto eliminando acentos y convirtiendo a minúsculas."""
    if not isinstance(texto, str):
        return ""
    texto = texto.lower()
    return "".join(
        c for c in unicodedata.normalize("NFD", texto)
        if unicodedata.category(c) != "Mn"
    )


def evaluar_riesgo(score):
    """Clasifica el nivel de riesgo según el índice (escala 0–10)."""
    if score >= 7:
        return "Alto"
    if score >= 4:
        return "Medio"
    return "Bajo"


def _parsear_monto(valor):
    """Convierte string de monto '$1.234.567,89' a float."""
    if not valor:
        return None
    try:
        limpio = re.sub(r'[^\d,]', '', str(valor)).replace(',', '.')
        # Si hay más de un punto, solo el último es decimal
        partes = limpio.split('.')
        if len(partes) > 2:
            limpio = ''.join(partes[:-1]) + '.' + partes[-1]
        return float(limpio)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL — MATRIZ LICITACIONES
# ═══════════════════════════════════════════════════════════════════

def analizar_adjudicaciones(df_adjudicaciones, df_tgn=None):
    """
    Aplica la Matriz de Riesgo Licitatorio sobre el DataFrame de
    adjudicaciones del flujo BORA→Comprar→TGN.

    Parámetros
    ----------
    df_adjudicaciones : DataFrame con columnas mínimas:
        organismo_contratante, tipo_proceso_bora, cuit_proveedor,
        monto_adjudicado_bora, fecha (fecha de publicación BORA),
        link_bora, etapa, alerta
    df_tgn : DataFrame opcional con columnas cuit, beneficiario,
        monto_pagado — para detectar proveedor multi-organismo en TGN

    Retorna
    -------
    DataFrame con columnas adicionales de riesgo:
        indicadores_riesgo    — lista de patrones detectados
        score_riesgo_licit    — puntaje crudo (suma de pesos)
        indice_riesgo_licit   — normalizado 0–10
        nivel_riesgo_licit    — Alto / Medio / Bajo
    """
    if df_adjudicaciones is None or df_adjudicaciones.empty:
        return df_adjudicaciones

    df = df_adjudicaciones.copy()

    # ── Pre-cálculo: frecuencia de CUITs por día ──────────────────
    cuit_por_dia = {}
    if "cuit_proveedor" in df.columns and "fecha" in df.columns:
        for _, row in df.iterrows():
            cuit  = str(row.get("cuit_proveedor", "")).strip()
            fecha = str(row.get("fecha", "")).strip()
            if cuit and fecha:
                key = (cuit, fecha)
                cuit_por_dia[key] = cuit_por_dia.get(key, 0) + 1

    # ── Pre-cálculo: CUITs multi-organismo en TGN ────────────────
    cuit_organismos_tgn = {}
    if df_tgn is not None and not df_tgn.empty and "cuit" in df_tgn.columns:
        for _, row in df_tgn.iterrows():
            cuit = str(row.get("cuit", "")).strip()
            org  = str(row.get("beneficiario", "")).strip()
            if cuit:
                if cuit not in cuit_organismos_tgn:
                    cuit_organismos_tgn[cuit] = set()
                if org:
                    cuit_organismos_tgn[cuit].add(org)

    # ── Evaluación fila por fila ──────────────────────────────────
    resultados_indicadores = []
    resultados_score       = []

    for _, row in df.iterrows():
        indicadores = []
        score       = 0.0

        tipo_raw  = limpiar_texto_curado(str(row.get("tipo_proceso_bora", "")))
        cuit      = str(row.get("cuit_proveedor", "")).strip()
        fecha     = str(row.get("fecha", "")).strip()
        monto_raw = row.get("monto_adjudicado_bora", "")
        monto     = _parsear_monto(monto_raw)

        # 1. CONTRATACIÓN DIRECTA
        cfg = MATRIZ_LICITACIONES["Contratación Directa"]
        if any(kw in tipo_raw for kw in cfg["keywords_tipo"]):
            indicadores.append("🔴 Contratación Directa")
            score += cfg["peso"]

        # 2. PROVEEDOR ÚNICO (mismo CUIT, mismo día, múltiples adjudicaciones)
        cfg = MATRIZ_LICITACIONES["Proveedor Único"]
        if cuit and fecha:
            frecuencia = cuit_por_dia.get((cuit, fecha), 0)
            if frecuencia >= cfg["umbral"]:
                indicadores.append(f"🔴 Proveedor Único ({frecuencia}x mismo día)")
                score += cfg["peso"]

        # 3. MONTO LÍMITE (fraccionamiento para evitar licitación)
        cfg = MATRIZ_LICITACIONES["Monto Límite"]
        if monto is not None:
            umbral     = cfg["umbral"]
            tolerancia = cfg["tolerancia"]
            if umbral * (1 - tolerancia) <= monto <= umbral * (1 + tolerancia):
                indicadores.append(f"🟡 Monto Límite (${monto:,.0f} ≈ umbral)")
                score += cfg["peso"]

        # 4. VELOCIDAD DE ADJUDICACIÓN (publicado y adjudicado el mismo día)
        cfg = MATRIZ_LICITACIONES["Velocidad Adjudicación"]
        fecha_pub = str(row.get("fecha", "")).strip()
        fecha_ext = str(row.get("fecha_extraccion", row.get("fecha", ""))).strip()
        if fecha_pub and fecha_ext and fecha_pub == fecha_ext:
            indicadores.append("🟡 Adjudicación mismo día de publicación")
            score += cfg["peso"]

        # 5. PROVEEDOR MULTI-ORGANISMO EN TGN
        cfg = MATRIZ_LICITACIONES["Proveedor Multi-Organismo"]
        if cuit and cuit in cuit_organismos_tgn:
            n_orgs = len(cuit_organismos_tgn[cuit])
            if n_orgs >= cfg["umbral"]:
                indicadores.append(f"🟡 Multi-Organismo TGN ({n_orgs} organismos)")
                score += cfg["peso"]

        resultados_indicadores.append(" | ".join(indicadores) if indicadores else "✅ Sin alertas")
        resultados_score.append(round(score, 2))

    df["indicadores_riesgo"]  = resultados_indicadores
    df["score_riesgo_licit"]  = resultados_score
    df["indice_riesgo_licit"] = df["score_riesgo_licit"].apply(
        lambda s: round(min(s / _PESO_MAX_LICITACIONES * 10, 10), 2)
    )
    df["nivel_riesgo_licit"]  = df["indice_riesgo_licit"].apply(evaluar_riesgo)

    # Resumen por consola
    alto   = (df["nivel_riesgo_licit"] == "Alto").sum()
    medio  = (df["nivel_riesgo_licit"] == "Medio").sum()
    bajo   = (df["nivel_riesgo_licit"] == "Bajo").sum()
    print(f"\n🔬 Matriz Licitaciones aplicada: {len(df)} registros")
    print(f"   🔴 Alto: {alto}  |  🟡 Medio: {medio}  |  🟢 Bajo: {bajo}")

    return df


# ═══════════════════════════════════════════════════════════════════
# FUNCIÓN ORIGINAL — MATRIZ XAI (sin cambios)
# ═══════════════════════════════════════════════════════════════════

def analizar_boletin(df, directorio_destino=None):
    """
    Aplica la matriz de Monteverde y guarda el reporte resultante.
    """
    if df is None or df.empty:
        return pd.DataFrame(), None, pd.DataFrame()

    df = df.copy()

    # 1. Limpieza y preparación
    df["texto_clean"] = df["detalle"].apply(limpiar_texto_curado)
    df["tipo_decision"] = "No identificado"
    df["transferencia"] = "No identificado"
    df["indice_fenomeno_corruptivo"] = 0.0

    # 2. Aplicación de la Matriz Teórica
    for categoria, info in MATRIZ_TEORICA.items():
        pattern = "|".join(info["keywords"])
        mask = df["texto_clean"].str.contains(pattern, na=False, regex=True)
        df.loc[mask, "tipo_decision"] = categoria
        df.loc[mask, "transferencia"] = info["transferencia"]
        df.loc[mask, "indice_fenomeno_corruptivo"] = info["peso"]

    df["nivel_riesgo_teorico"] = df["indice_fenomeno_corruptivo"].apply(evaluar_riesgo)

    # 3. Determinar directorio de guardado
    for candidate in [directorio_destino, DATA_DIR, FALLBACK_DIR]:
        if candidate and os.path.exists(candidate):
            save_dir = candidate
            break
    else:
        save_dir = FALLBACK_DIR
        os.makedirs(save_dir, exist_ok=True)

    fecha_str   = datetime.now().strftime("%Y%m%d_%H%M%S")
    nombre_base = f"reporte_fenomenos_{fecha_str}"
    path_excel  = os.path.join(save_dir, f"{nombre_base}.xlsx")

    cols = [
        "fecha", "nro_proceso", "detalle", "tipo_proceso",
        "tipo_decision", "transferencia",
        "indice_fenomeno_corruptivo", "nivel_riesgo_teorico", "link",
    ]
    df_export = df[[c for c in cols if c in df.columns]]

    try:
        df_export.to_excel(path_excel, index=False, engine="openpyxl")
        print(f"✅ Reporte generado: {path_excel}")
    except Exception as e:
        print(f"❌ Error al guardar Excel: {e}. Intentando CSV...")
        path_excel = os.path.join(save_dir, f"{nombre_base}.csv")
        try:
            df_export.to_csv(path_excel, index=False)
            print(f"✅ Reporte CSV generado: {path_excel}")
        except Exception as e2:
            print(f"❌ Error al guardar CSV: {e2}")
            path_excel = None

    return df, path_excel, pd.DataFrame()
