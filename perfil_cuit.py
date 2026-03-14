"""
perfil_cuit.py — Perfil por Proveedor/CUIT
Monitor de Fenómenos Corruptivos — Ph.D. Monteverde (2020)

Genera el perfil completo de un proveedor a partir de los datos
acumulados en los Excel diarios (data/YYYY-MM/).

Diferencial clave: muestra quién COBRÓ (TGN), no solo quién ganó.

Incluye:
  - Estadísticas generales (contratos, montos adjudicados vs cobrados)
  - Organismos con los que opera (concentración o diversificación)
  - Evolución temporal
  - Red flags acumuladas por este CUIT
  - Flujos completos BORA→Comprar→TGN
  - Score de riesgo histórico

Uso:
    python perfil_cuit.py 30-12345678-9
    python perfil_cuit.py --buscar "CONSTRUCTORA"   # busca por nombre parcial
    python perfil_cuit.py --top 10                  # top 10 por monto cobrado en TGN
    python perfil_cuit.py --ranking-riesgo          # ranking por score de riesgo
"""

import os
import re
import sys
import glob
import argparse
import unicodedata
from datetime import datetime

import pandas as pd


# ─────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────
DATA_DIR = os.path.join(os.getcwd(), "data")


# ─────────────────────────────────────────
# CARGA DE DATOS HISTÓRICOS
# ─────────────────────────────────────────
def cargar_historico():
    """
    Levanta todos los reporte_*.xlsx y flujo_licitaciones_*.xlsx
    de las carpetas mensuales y los consolida.
    """
    patron = os.path.join(DATA_DIR, "**", "reporte_*.xlsx")
    archivos = sorted(glob.glob(patron, recursive=True))

    if not archivos:
        print(f"⚠️  No se encontraron archivos en {DATA_DIR}")
        print("    Ejecutá primero: python diario.py")
        return pd.DataFrame(), pd.DataFrame()

    dfs_flujo = []
    dfs_adj   = []

    for archivo in archivos:
        try:
            xl = pd.ExcelFile(archivo, engine="openpyxl")

            # Pestaña flujo completo (tiene el cruce con TGN)
            for sheet in ["🚨 Flujo Completo", "🔗 Flujo Cruzado"]:
                if sheet in xl.sheet_names:
                    df = pd.read_excel(xl, sheet_name=sheet, engine="openpyxl")
                    df["_archivo"] = os.path.basename(archivo)
                    dfs_flujo.append(df)
                    break

            # Pestaña adjudicaciones (tiene CUIT + proveedor limpio)
            if "🏆 Adjudicaciones" in xl.sheet_names:
                df = pd.read_excel(xl, sheet_name="🏆 Adjudicaciones", engine="openpyxl")
                dfs_adj.append(df)

        except Exception as e:
            print(f"  ⚠️  Error leyendo {archivo}: {e}")

    df_flujo = pd.concat(dfs_flujo, ignore_index=True) if dfs_flujo else pd.DataFrame()
    df_adj   = pd.concat(dfs_adj,   ignore_index=True) if dfs_adj   else pd.DataFrame()

    # Normalizar fechas
    for df in [df_flujo, df_adj]:
        for col in ["fecha", "fecha_extraccion", "fecha_publicacion"]:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")

    print(f"  ✅ {len(df_flujo)} registros de flujo | {len(df_adj)} adjudicaciones")
    return df_flujo, df_adj


def cargar_tgn():
    """Lee la pestaña TGN de todos los reportes para cruce de cobros."""
    patron = os.path.join(DATA_DIR, "**", "reporte_*.xlsx")
    archivos = sorted(glob.glob(patron, recursive=True))
    dfs = []
    for archivo in archivos:
        try:
            xl = pd.ExcelFile(archivo, engine="openpyxl")
            if "💰 TGN" in xl.sheet_names:
                df = pd.read_excel(xl, sheet_name="💰 TGN", engine="openpyxl")
                dfs.append(df)
        except Exception:
            pass
    return pd.concat(dfs, ignore_index=True).drop_duplicates() if dfs else pd.DataFrame()


# ─────────────────────────────────────────
# NORMALIZACIÓN
# ─────────────────────────────────────────
def normalizar(texto):
    if not isinstance(texto, str):
        return ""
    t = texto.upper().strip()
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    for p in ["S.A.U.", "S.A.", "S.R.L.", "S.A.S.", " SA ", " SRL "]:
        t = t.replace(p, " ")
    return re.sub(r"\s+", " ", t).strip()


def limpiar_cuit(cuit):
    """Normaliza CUIT: acepta 30-12345678-9, 30123456789, etc."""
    if not isinstance(cuit, str):
        cuit = str(cuit)
    return re.sub(r"[^\d]", "", cuit)


# ─────────────────────────────────────────
# PARSEO DE MONTOS
# ─────────────────────────────────────────
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


# ─────────────────────────────────────────
# BÚSQUEDA POR CUIT O NOMBRE
# ─────────────────────────────────────────
def buscar_por_cuit(df, cuit_busqueda):
    """Filtra por CUIT exacto (acepta formatos con/sin guiones)."""
    cuit_norm = limpiar_cuit(cuit_busqueda)
    col = next(
        (c for c in ["cuit_proveedor", "cuit", "cuit_proveedor"] if c in df.columns), None
    )
    if not col:
        return pd.DataFrame(), ""
    mask = df[col].apply(lambda x: limpiar_cuit(str(x)) == cuit_norm)
    df_filtrado = df[mask].copy()

    # Nombre canónico del proveedor
    nombre = ""
    for col_nombre in ["proveedor_adjudicado", "proveedor_nombre", "beneficiario_tgn", "beneficiario"]:
        if col_nombre in df_filtrado.columns:
            nombres = df_filtrado[col_nombre].dropna()
            nombres = nombres[nombres.astype(str).str.strip() != ""]
            if not nombres.empty:
                nombre = nombres.value_counts().index[0]
                break
    return df_filtrado, nombre


def buscar_por_nombre(df, nombre_busqueda):
    """Filtra por nombre parcial del proveedor."""
    nombre_norm = normalizar(nombre_busqueda)
    for col in ["proveedor_adjudicado", "proveedor_nombre", "beneficiario"]:
        if col in df.columns:
            mask = df[col].apply(lambda x: nombre_norm in normalizar(str(x)))
            df_filtrado = df[mask].copy()
            if not df_filtrado.empty:
                return df_filtrado
    return pd.DataFrame()


# ─────────────────────────────────────────
# PERFIL PRINCIPAL
# ─────────────────────────────────────────
def generar_perfil(cuit_busqueda, df_flujo, df_adj, df_tgn, exportar=True):
    """
    Genera el perfil completo de un proveedor por CUIT.

    Retorna dict con todas las secciones del perfil.
    """
    print(f"\n{'='*60}")
    print(f"🏢 PERFIL DE PROVEEDOR: CUIT {cuit_busqueda}")
    print(f"{'='*60}")

    df_prov, nombre_canonico = buscar_por_cuit(df_flujo, cuit_busqueda)

    if df_prov.empty and not df_adj.empty:
        df_prov, nombre_canonico = buscar_por_cuit(df_adj, cuit_busqueda)

    if df_prov.empty:
        print(f"❌ CUIT '{cuit_busqueda}' no encontrado en el histórico.")
        print("   Usá --buscar NOMBRE para buscar por nombre de empresa.")
        return {}

    print(f"  Nombre: {nombre_canonico or '(sin nombre registrado)'}")
    print(f"  CUIT:   {cuit_busqueda}")
    print(f"  Registros: {len(df_prov)}\n")

    # ── Columnas clave ──
    col_monto_adj = next(
        (c for c in ["monto_adjudicado_bora", "monto_adjudicado", "_monto_num"] if c in df_prov.columns), None
    )
    col_monto_tgn = next(
        (c for c in ["monto_cobrado_tgn", "monto_pagado"] if c in df_prov.columns), None
    )
    col_org = next(
        (c for c in ["organismo_contratante", "organismo", "unidad_ejecutora"] if c in df_prov.columns), None
    )
    col_fecha = next(
        (c for c in ["fecha", "fecha_extraccion", "fecha_publicacion"] if c in df_prov.columns), None
    )

    df_prov["_monto_adj_num"] = df_prov[col_monto_adj].apply(parsear_monto) if col_monto_adj else 0.0
    df_prov["_monto_tgn_num"] = df_prov[col_monto_tgn].apply(parsear_monto) if col_monto_tgn else 0.0

    # ── 1. ESTADÍSTICAS GENERALES ──────────────────────────────
    total_contratos    = len(df_prov)
    monto_adj_total    = df_prov["_monto_adj_num"].sum()
    monto_tgn_total    = df_prov["_monto_tgn_num"].sum()
    cobraron_tgn       = df_prov["_monto_tgn_num"].gt(0).sum() if col_monto_tgn else 0
    flujos_completos   = 0
    if "alerta" in df_prov.columns:
        flujos_completos = df_prov["alerta"].str.contains("FLUJO COMPLETO", na=False).sum()

    fecha_inicio = fecha_fin = None
    if col_fecha:
        fechas = df_prov[col_fecha].dropna()
        if not fechas.empty:
            fecha_inicio = fechas.min()
            fecha_fin    = fechas.max()

    organismos_distintos = df_prov[col_org].nunique() if col_org else 0

    # Ratio cobrado/adjudicado — el dato diferencial
    ratio_cobro = (monto_tgn_total / monto_adj_total * 100) if monto_adj_total > 0 else 0

    print("📊 ESTADÍSTICAS GENERALES")
    print(f"   Contratos totales:        {total_contratos}")
    print(f"   Monto total adjudicado:   ${monto_adj_total:>15,.0f} ARS")
    print(f"   Monto total cobrado (TGN):${monto_tgn_total:>15,.0f} ARS  "
          f"({ratio_cobro:.1f}% del adjudicado)")
    print(f"   Organismos distintos:     {organismos_distintos}")
    print(f"   Flujos BORA→Comprar→TGN: {flujos_completos}")
    if fecha_inicio and fecha_fin:
        print(f"   Período:                  {fecha_inicio.date()} → {fecha_fin.date()}")

    # ── 2. ORGANISMOS CON LOS QUE OPERA ───────────────────────
    print("\n🏛️  ORGANISMOS CONTRATANTES")
    df_por_organismo = pd.DataFrame()
    if col_org:
        grp = df_prov.groupby(col_org).agg(
            contratos=("_monto_adj_num", "count"),
            monto_adj=("_monto_adj_num", "sum"),
            monto_tgn=("_monto_tgn_num", "sum"),
        ).reset_index().sort_values("contratos", ascending=False)
        grp.columns = ["organismo", "contratos", "monto_adj", "monto_tgn"]
        df_por_organismo = grp.copy()

        for i, row in grp.iterrows():
            pct = row["contratos"] / total_contratos * 100
            cobro_label = f"  | cobró ${row['monto_tgn']:>10,.0f}" if row["monto_tgn"] > 0 else ""
            print(f"   {str(row['organismo'])[:58]:<58} "
                  f"×{int(row['contratos']):>3} ({pct:.0f}%)  "
                  f"${row['monto_adj']:>12,.0f}{cobro_label}")

        # Alerta si opera con muchos organismos — patrón de concentración inversa
        if organismos_distintos >= 5:
            print(f"\n   🟡 Opera con {organismos_distintos} organismos distintos "
                  f"— patrón de presencia amplia")

    # ── 3. EVOLUCIÓN TEMPORAL ──────────────────────────────────
    print("\n📅 EVOLUCIÓN MENSUAL")
    df_temporal = pd.DataFrame()
    if col_fecha:
        df_prov["_mes"] = df_prov[col_fecha].dt.to_period("M")
        df_temporal = (
            df_prov.groupby("_mes").agg(
                contratos=("_monto_adj_num", "count"),
                monto_adj=("_monto_adj_num", "sum"),
                monto_tgn=("_monto_tgn_num", "sum"),
            ).reset_index()
        )
        df_temporal["_mes"] = df_temporal["_mes"].astype(str)

        max_contratos = df_temporal["contratos"].max()
        for _, row in df_temporal.iterrows():
            barra = "█" * min(int(row["contratos"] / max(max_contratos, 1) * 20), 20)
            cobro_label = f"  | TGN ${row['monto_tgn']:>10,.0f}" if row["monto_tgn"] > 0 else ""
            print(f"   {row['_mes']}  {barra:<20}  "
                  f"{int(row['contratos']):>3} contratos  "
                  f"${row['monto_adj']:>12,.0f}{cobro_label}")

    # ── 4. RED FLAGS ACUMULADAS ────────────────────────────────
    print("\n🚨 RED FLAGS HISTÓRICAS DE ESTE CUIT")
    df_red_flags = pd.DataFrame()
    if "indicadores_riesgo" in df_prov.columns:
        flags_series = df_prov["indicadores_riesgo"].dropna()
        flags_series = flags_series[flags_series != "✅ Sin alertas"]

        if flags_series.empty:
            print("   ✅ Sin red flags detectadas en el período")
        else:
            todos_flags = []
            for flags_str in flags_series:
                todos_flags.extend([f.strip() for f in str(flags_str).split("|")])
            conteo = pd.Series(todos_flags).value_counts()
            for flag, count in conteo.items():
                print(f"   {flag:<55}  ×{count}")
            df_red_flags = conteo.reset_index()
            df_red_flags.columns = ["indicador", "frecuencia"]

    # Score de riesgo acumulado
    if "indice_riesgo_licit" in df_prov.columns:
        score_prom = df_prov["indice_riesgo_licit"].mean()
        score_max  = df_prov["indice_riesgo_licit"].max()
        alto  = (df_prov["nivel_riesgo_licit"] == "Alto").sum()  if "nivel_riesgo_licit" in df_prov.columns else 0
        medio = (df_prov["nivel_riesgo_licit"] == "Medio").sum() if "nivel_riesgo_licit" in df_prov.columns else 0
        print(f"\n   Score de riesgo promedio: {score_prom:.2f}/10  |  Máximo: {score_max:.2f}/10")
        print(f"   🔴 Contratos Alto riesgo: {alto}  |  🟡 Medio: {medio}")

    # ── 5. FLUJOS BORA→COMPRAR→TGN — LO QUE COBRÓ ────────────
    print("\n💰 DETALLE DE COBROS EN TGN (flujo completo)")
    df_cobros = pd.DataFrame()
    if "alerta" in df_prov.columns:
        df_cobros = df_prov[
            df_prov["alerta"].str.contains("TGN|FLUJO", na=False)
        ].copy()

        if df_cobros.empty:
            print("   Sin cobros en TGN registrados aún para este CUIT")
        else:
            for _, row in df_cobros.iterrows():
                org   = str(row.get(col_org, ""))[:50] if col_org else ""
                monto_a = row.get("_monto_adj_num", 0)
                monto_t = row.get("_monto_tgn_num", 0)
                fecha_v = str(row.get(col_fecha, ""))[:10] if col_fecha else ""
                link  = str(row.get("link_bora", ""))[:60]
                print(f"   {fecha_v}  {org:<50}")
                print(f"           Adj: ${monto_a:>12,.0f}  |  Cobrado TGN: ${monto_t:>12,.0f}")
                if link:
                    print(f"           {link}")

    # ── 6. CRUCE CON TGN DIRECTO (si viene de otro Excel) ──────
    if not df_tgn.empty and df_cobros.empty:
        cuit_norm = limpiar_cuit(cuit_busqueda)
        df_tgn_prov = df_tgn[
            df_tgn["cuit"].apply(lambda x: limpiar_cuit(str(x)) == cuit_norm)
        ]
        if not df_tgn_prov.empty:
            print("\n💰 PAGOS EN TGN (fuente directa)")
            for _, row in df_tgn_prov.iterrows():
                print(f"   Año {row.get('anio', '')}  |  "
                      f"Beneficiario: {str(row.get('beneficiario', ''))[:40]}  |  "
                      f"${parsear_monto(row.get('monto_pagado', 0)):>12,.0f}")

    # ── 7. EXPORTAR EXCEL ──────────────────────────────────────
    if exportar:
        cuit_limpio    = limpiar_cuit(cuit_busqueda)
        carpeta_salida = os.path.join(DATA_DIR, "perfiles")
        os.makedirs(carpeta_salida, exist_ok=True)
        hoy = datetime.now().strftime("%Y-%m-%d")
        archivo_salida = os.path.join(
            carpeta_salida, f"perfil_cuit_{cuit_limpio}_{hoy}.xlsx"
        )

        with pd.ExcelWriter(archivo_salida, engine="openpyxl") as writer:
            # Resumen
            resumen = pd.DataFrame([{
                "cuit":                    cuit_busqueda,
                "nombre":                  nombre_canonico,
                "total_contratos":         total_contratos,
                "monto_total_adjudicado":  monto_adj_total,
                "monto_total_cobrado_tgn": monto_tgn_total,
                "ratio_cobro_pct":         round(ratio_cobro, 1),
                "organismos_distintos":    organismos_distintos,
                "flujos_completos":        flujos_completos,
                "fecha_inicio":            str(fecha_inicio.date()) if fecha_inicio else "",
                "fecha_fin":               str(fecha_fin.date()) if fecha_fin else "",
                "generado_en":             datetime.now().strftime("%Y-%m-%d %H:%M"),
            }])
            resumen.to_excel(writer, sheet_name="📋 Resumen", index=False)

            # Por organismo
            if not df_por_organismo.empty:
                df_por_organismo.to_excel(writer, sheet_name="🏛️ Por Organismo", index=False)

            # Evolución temporal
            if not df_temporal.empty:
                df_temporal.to_excel(writer, sheet_name="📅 Evolución", index=False)

            # Red flags
            if not df_red_flags.empty:
                df_red_flags.to_excel(writer, sheet_name="🚨 Red Flags", index=False)

            # Detalle completo
            df_export = df_prov.drop(
                columns=["_monto_adj_num", "_monto_tgn_num", "_mes", "_archivo"],
                errors="ignore"
            )
            df_export.to_excel(writer, sheet_name="📄 Detalle Completo", index=False)

        print(f"\n  💾 Perfil exportado: {archivo_salida}")

    print(f"\n{'='*60}\n")

    return {
        "cuit":               cuit_busqueda,
        "nombre":             nombre_canonico,
        "total_contratos":    total_contratos,
        "monto_adj_total":    monto_adj_total,
        "monto_tgn_total":    monto_tgn_total,
        "ratio_cobro":        ratio_cobro,
        "organismos":         organismos_distintos,
        "flujos_completos":   flujos_completos,
        "df_por_organismo":   df_por_organismo,
        "df_temporal":        df_temporal,
        "df_red_flags":       df_red_flags,
        "df_detalle":         df_prov,
    }


# ─────────────────────────────────────────
# TOP N POR MONTO COBRADO EN TGN
# El ranking diferencial: no por quién ganó sino por quién cobró
# ─────────────────────────────────────────
def top_por_cobro_tgn(df_flujo, n=10):
    col_cuit  = next((c for c in ["cuit_proveedor", "cuit"] if c in df_flujo.columns), None)
    col_prov  = next((c for c in ["proveedor_adjudicado", "proveedor_nombre"] if c in df_flujo.columns), None)
    col_tgn   = next((c for c in ["monto_cobrado_tgn", "monto_pagado"] if c in df_flujo.columns), None)
    col_adj   = next((c for c in ["monto_adjudicado_bora", "monto_adjudicado"] if c in df_flujo.columns), None)

    if not col_cuit or not col_tgn:
        print("❌ No hay datos de TGN disponibles aún")
        return

    df_flujo["_tgn"] = df_flujo[col_tgn].apply(parsear_monto)
    df_flujo["_adj"] = df_flujo[col_adj].apply(parsear_monto) if col_adj else 0.0

    df_cobros = df_flujo[df_flujo["_tgn"] > 0]
    if df_cobros.empty:
        print("⚠️  Sin registros de cobros en TGN todavía")
        return

    grp_cols = {col_cuit: "first"}
    if col_prov:
        grp_cols[col_prov] = "first"
    grp_cols["_tgn"] = "sum"
    grp_cols["_adj"] = "sum"

    grp = (
        df_cobros.groupby(col_cuit)
        .agg(**{
            "monto_tgn": pd.NamedAgg(column="_tgn", aggfunc="sum"),
            "monto_adj": pd.NamedAgg(column="_adj", aggfunc="sum"),
            "contratos": pd.NamedAgg(column="_adj", aggfunc="count"),
            **({"nombre": pd.NamedAgg(column=col_prov, aggfunc="first")} if col_prov else {}),
        })
        .reset_index()
        .sort_values("monto_tgn", ascending=False)
    )

    print(f"\n💰 TOP {n} PROVEEDORES POR MONTO COBRADO EN TGN")
    print(f"   (Diferencial: quién cobró, no solo quién ganó)\n")
    print(f"  {'#':>3}  {'CUIT':<20}  {'Nombre':<40}  {'Contratos':>9}  {'Cobrado TGN':>18}")
    print("  " + "-" * 100)
    for i, (_, row) in enumerate(grp.head(n).iterrows(), 1):
        nombre = str(row.get("nombre", ""))[:40] if "nombre" in grp.columns else ""
        print(f"  {i:>3}. {str(row[col_cuit]):<20}  {nombre:<40}  "
              f"{int(row['contratos']):>9}  "
              f"${row['monto_tgn']:>17,.0f}")


# ─────────────────────────────────────────
# RANKING POR SCORE DE RIESGO
# ─────────────────────────────────────────
def ranking_por_riesgo(df_flujo, n=15):
    col_cuit  = next((c for c in ["cuit_proveedor", "cuit"] if c in df_flujo.columns), None)
    col_prov  = next((c for c in ["proveedor_adjudicado", "proveedor_nombre"] if c in df_flujo.columns), None)
    col_score = "indice_riesgo_licit"

    if not col_cuit or col_score not in df_flujo.columns:
        print("❌ No hay scores de riesgo calculados. Ejecutá diario.py primero.")
        return

    grp = (
        df_flujo[df_flujo[col_cuit].astype(str).str.strip() != ""]
        .groupby(col_cuit)
        .agg(
            score_promedio=(col_score, "mean"),
            score_maximo=(col_score, "max"),
            contratos=(col_score, "count"),
            **({"nombre": pd.NamedAgg(column=col_prov, aggfunc="first")} if col_prov else {}),
        )
        .reset_index()
        .sort_values("score_promedio", ascending=False)
    )

    print(f"\n🔴 TOP {n} PROVEEDORES POR SCORE DE RIESGO (Monteverde)\n")
    print(f"  {'#':>3}  {'CUIT':<20}  {'Nombre':<38}  {'Contratos':>9}  "
          f"{'Score Prom':>10}  {'Score Máx':>10}")
    print("  " + "-" * 100)
    for i, (_, row) in enumerate(grp.head(n).iterrows(), 1):
        nombre = str(row.get("nombre", ""))[:38] if "nombre" in grp.columns else ""
        nivel  = "🔴" if row["score_promedio"] >= 7 else ("🟡" if row["score_promedio"] >= 4 else "🟢")
        print(f"  {i:>3}. {str(row[col_cuit]):<20}  {nombre:<38}  "
              f"{int(row['contratos']):>9}  "
              f"{nivel} {row['score_promedio']:>6.2f}/10  "
              f"{row['score_maximo']:>8.2f}/10")


# ─────────────────────────────────────────
# BUSCAR POR NOMBRE DE EMPRESA
# ─────────────────────────────────────────
def buscar_por_nombre_listar(df_flujo, nombre_busqueda):
    col_cuit = next((c for c in ["cuit_proveedor", "cuit"] if c in df_flujo.columns), None)
    col_prov = next((c for c in ["proveedor_adjudicado", "proveedor_nombre"] if c in df_flujo.columns), None)

    if not col_prov:
        print("❌ No hay columna de nombre de proveedor")
        return

    nombre_norm = normalizar(nombre_busqueda)
    mask = df_flujo[col_prov].apply(lambda x: nombre_norm in normalizar(str(x)))
    df_match = df_flujo[mask].copy()

    if df_match.empty:
        print(f"❌ No se encontraron proveedores con '{nombre_busqueda}'")
        return

    print(f"\n🔍 PROVEEDORES que contienen '{nombre_busqueda}'\n")
    if col_cuit:
        resumen = (
            df_match.groupby(col_cuit)
            .agg(
                nombre=(col_prov, "first"),
                contratos=(col_prov, "count"),
            )
            .reset_index()
            .sort_values("contratos", ascending=False)
        )
        for _, row in resumen.iterrows():
            print(f"   CUIT: {row[col_cuit]:<20}  "
                  f"{str(row['nombre'])[:50]:<50}  "
                  f"×{int(row['contratos'])} contratos")
        print(f"\n   → Usá: python perfil_cuit.py CUIT para el perfil completo")
    else:
        for nombre in df_match[col_prov].value_counts().head(20).index:
            print(f"   {nombre}")


# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Perfil por CUIT — Monitor de Fenómenos Corruptivos"
    )
    parser.add_argument("cuit",          nargs="?",  help="CUIT del proveedor (con o sin guiones)")
    parser.add_argument("--buscar",      metavar="NOMBRE", help="Buscar por nombre de empresa")
    parser.add_argument("--top",         type=int,   metavar="N", help="Top N por monto cobrado en TGN")
    parser.add_argument("--ranking-riesgo", action="store_true", help="Ranking por score de riesgo")
    parser.add_argument("--no-exportar", action="store_true",    help="No generar Excel del perfil")
    args = parser.parse_args()

    print("🔄 Cargando datos históricos...")
    df_flujo, df_adj = cargar_historico()
    df_tgn           = cargar_tgn()

    if df_flujo.empty and df_adj.empty:
        sys.exit(1)

    if args.buscar:
        buscar_por_nombre_listar(df_flujo if not df_flujo.empty else df_adj, args.buscar)

    elif args.top:
        top_por_cobro_tgn(df_flujo, n=args.top)

    elif args.ranking_riesgo:
        ranking_por_riesgo(df_flujo)

    elif args.cuit:
        generar_perfil(
            args.cuit,
            df_flujo,
            df_adj,
            df_tgn,
            exportar=not args.no_exportar,
        )

    else:
        parser.print_help()
        print("\n💡 Ejemplos:")
        print("   python perfil_cuit.py 30-12345678-9")
        print("   python perfil_cuit.py --buscar 'TECHINT'")
        print("   python perfil_cuit.py --top 10")
        print("   python perfil_cuit.py --ranking-riesgo")
