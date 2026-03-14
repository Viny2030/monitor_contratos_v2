"""
perfil_organismo.py — Perfil por Organismo Contratante
Monitor de Fenómenos Corruptivos — Ph.D. Monteverde (2020)

Genera el perfil completo de un organismo a partir de los datos
acumulados en los Excel diarios (data/YYYY-MM/).

Incluye:
  - Estadísticas generales (totales, montos, períodos)
  - Proveedores más frecuentes
  - Evolución temporal de adjudicaciones
  - Concentración de proveedores (índice HHI adaptado)
  - Red flags históricas detectadas
  - Flujos completos BORA→Comprar→TGN

Uso:
    python perfil_organismo.py "MINISTERIO DE DEFENSA"
    python perfil_organismo.py --lista          # muestra todos los organismos
    python perfil_organismo.py --top 10         # top 10 por monto adjudicado
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
# Levanta todos los Excel de data/YYYY-MM/
# ─────────────────────────────────────────
def cargar_historico(sheet="🚨 Flujo Completo"):
    """
    Lee todos los archivos reporte_*.xlsx de las carpetas mensuales
    y los consolida en un único DataFrame.
    """
    patron = os.path.join(DATA_DIR, "**", "reporte_*.xlsx")
    archivos = sorted(glob.glob(patron, recursive=True))

    if not archivos:
        print(f"⚠️  No se encontraron archivos en {DATA_DIR}")
        print("    Ejecutá primero: python diario.py")
        return pd.DataFrame()

    dfs = []
    for archivo in archivos:
        try:
            xl = pd.ExcelFile(archivo, engine="openpyxl")
            # Buscar la pestaña del flujo completo
            pestana = sheet if sheet in xl.sheet_names else xl.sheet_names[0]
            df = pd.read_excel(xl, sheet_name=pestana, engine="openpyxl")
            df["_archivo"] = os.path.basename(archivo)
            dfs.append(df)
        except Exception as e:
            print(f"  ⚠️  Error leyendo {archivo}: {e}")

    if not dfs:
        return pd.DataFrame()

    df_total = pd.concat(dfs, ignore_index=True)

    # Normalizar columna fecha
    for col_fecha in ["fecha", "fecha_extraccion", "fecha_publicacion"]:
        if col_fecha in df_total.columns:
            df_total[col_fecha] = pd.to_datetime(df_total[col_fecha], errors="coerce")
            break

    print(f"  ✅ {len(df_total)} registros históricos cargados ({len(archivos)} archivos)")
    return df_total


def cargar_adjudicaciones():
    """Lee la pestaña de adjudicaciones con CUIT para cruce de proveedores."""
    patron = os.path.join(DATA_DIR, "**", "reporte_*.xlsx")
    archivos = sorted(glob.glob(patron, recursive=True))
    dfs = []
    for archivo in archivos:
        try:
            xl = pd.ExcelFile(archivo, engine="openpyxl")
            if "🏆 Adjudicaciones" in xl.sheet_names:
                df = pd.read_excel(xl, sheet_name="🏆 Adjudicaciones", engine="openpyxl")
                dfs.append(df)
        except Exception:
            pass
    return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()


# ─────────────────────────────────────────
# NORMALIZACIÓN DE NOMBRES
# ─────────────────────────────────────────
def normalizar(texto):
    if not isinstance(texto, str):
        return ""
    t = texto.upper().strip()
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    # Quitar formas jurídicas
    for p in ["S.A.U.", "S.A.", "S.R.L.", "S.A.S.", "S.C.", " SA ", " SRL "]:
        t = t.replace(p, " ")
    return re.sub(r"\s+", " ", t).strip()


def buscar_organismo(df, nombre_busqueda):
    """
    Filtra el DataFrame por organismo usando coincidencia parcial normalizada.
    Soporta múltiples nombres de columna posibles.
    """
    col = None
    for c in ["organismo_contratante", "organismo", "unidad_ejecutora"]:
        if c in df.columns:
            col = c
            break
    if col is None:
        return pd.DataFrame(), None

    busqueda_norm = normalizar(nombre_busqueda)
    mask = df[col].apply(
        lambda x: busqueda_norm in normalizar(str(x))
    )
    df_filtrado = df[mask].copy()

    # Nombre canónico = el más frecuente en los datos
    nombre_canonico = (
        df_filtrado[col].value_counts().index[0]
        if not df_filtrado.empty else nombre_busqueda
    )
    return df_filtrado, nombre_canonico


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
# ÍNDICE DE CONCENTRACIÓN (HHI adaptado)
# Mide cuánto se concentra el gasto en pocos proveedores.
# HHI = suma de (participación_i)^2 × 10000
# HHI > 2500 → alta concentración (riesgo)
# HHI 1500-2500 → concentración moderada
# HHI < 1500 → competitivo
# ─────────────────────────────────────────
def calcular_hhi(series_montos):
    total = series_montos.sum()
    if total == 0:
        return 0.0
    participaciones = series_montos / total
    return round((participaciones ** 2).sum() * 10_000, 1)


def interpretar_hhi(hhi):
    if hhi > 2500:
        return "🔴 Alta concentración (riesgo de captura)"
    if hhi > 1500:
        return "🟡 Concentración moderada"
    return "🟢 Mercado competitivo"


# ─────────────────────────────────────────
# PERFIL PRINCIPAL
# ─────────────────────────────────────────
def generar_perfil(nombre_busqueda, df_historico, df_adjudicaciones=None, exportar=True):
    """
    Genera el perfil completo de un organismo.

    Parámetros
    ----------
    nombre_busqueda : str — nombre parcial del organismo
    df_historico    : DataFrame — flujo cruzado histórico
    df_adjudicaciones : DataFrame opcional — adjudicaciones con CUIT
    exportar        : bool — guardar Excel del perfil

    Retorna
    -------
    dict con todas las secciones del perfil
    """
    print(f"\n{'='*60}")
    print(f"🏛️  PERFIL DE ORGANISMO: {nombre_busqueda.upper()}")
    print(f"{'='*60}")

    df_org, nombre_canonico = buscar_organismo(df_historico, nombre_busqueda)

    if df_org.empty:
        print(f"❌ No se encontró el organismo '{nombre_busqueda}' en el histórico.")
        print("   Usá --lista para ver los organismos disponibles.")
        return {}

    print(f"  Nombre canónico: {nombre_canonico}")
    print(f"  Registros encontrados: {len(df_org)}\n")

    # ── Columnas de monto ──
    col_monto = None
    for c in ["monto_adjudicado_bora", "monto_adjudicado", "monto_ars"]:
        if c in df_org.columns:
            col_monto = c
            break

    df_org["_monto_num"] = df_org[col_monto].apply(parsear_monto) if col_monto else 0.0

    # ── Columna fecha ──
    col_fecha = None
    for c in ["fecha", "fecha_extraccion", "fecha_publicacion"]:
        if c in df_org.columns:
            col_fecha = c
            break

    # ── 1. ESTADÍSTICAS GENERALES ──────────────────────────────
    total_registros   = len(df_org)
    monto_total       = df_org["_monto_num"].sum()
    monto_promedio    = df_org["_monto_num"].mean()

    fecha_inicio = fecha_fin = None
    if col_fecha:
        fechas_validas = df_org[col_fecha].dropna()
        if not fechas_validas.empty:
            fecha_inicio = fechas_validas.min()
            fecha_fin    = fechas_validas.max()

    col_cuit = "cuit_proveedor" if "cuit_proveedor" in df_org.columns else None
    cuits_unicos = df_org[col_cuit].nunique() if col_cuit else 0

    flujos_completos = 0
    if "alerta" in df_org.columns:
        flujos_completos = df_org["alerta"].str.contains(
            "FLUJO COMPLETO", na=False
        ).sum()

    cobraron_tgn = 0
    if "cobro_en_tgn" in df_org.columns:
        cobraron_tgn = df_org["cobro_en_tgn"].isin(["✅ SÍ", True, "True"]).sum()

    print("📊 ESTADÍSTICAS GENERALES")
    print(f"   Adjudicaciones totales:  {total_registros}")
    print(f"   Monto total adjudicado:  ${monto_total:>15,.0f} ARS")
    print(f"   Monto promedio:          ${monto_promedio:>15,.0f} ARS")
    print(f"   Proveedores distintos:   {cuits_unicos} CUITs")
    print(f"   Cobraron en TGN:         {cobraron_tgn}")
    print(f"   Flujos BORA→Comprar→TGN: {flujos_completos}")
    if fecha_inicio and fecha_fin:
        print(f"   Período:                 {fecha_inicio.date()} → {fecha_fin.date()}")

    # ── 2. PROVEEDORES MÁS FRECUENTES ─────────────────────────
    print("\n🏢 TOP 10 PROVEEDORES (por frecuencia)")
    df_proveedores = pd.DataFrame()
    if col_cuit:
        col_proveedor = next(
            (c for c in ["proveedor_adjudicado", "proveedor_nombre", "beneficiario_tgn"]
             if c in df_org.columns), None
        )
        grp = df_org.groupby(col_cuit).agg(
            cantidad=("_monto_num", "count"),
            monto_total=("_monto_num", "sum"),
        ).reset_index()
        grp.columns = ["cuit", "cantidad", "monto_total"]

        if col_proveedor:
            nombres = (
                df_org.dropna(subset=[col_proveedor])
                .groupby(col_cuit)[col_proveedor]
                .first()
                .reset_index()
            )
            nombres.columns = ["cuit", "nombre"]
            grp = grp.merge(nombres, on="cuit", how="left")
        else:
            grp["nombre"] = ""

        grp = grp.sort_values("cantidad", ascending=False).reset_index(drop=True)
        df_proveedores = grp.copy()

        # HHI de concentración
        hhi = calcular_hhi(grp["monto_total"])
        interpretacion_hhi = interpretar_hhi(hhi)

        for i, row in grp.head(10).iterrows():
            pct = (row["monto_total"] / monto_total * 100) if monto_total > 0 else 0
            nombre_display = str(row.get("nombre", ""))[:45] or row["cuit"]
            print(f"   {i+1:2}. {nombre_display:<46} "
                  f"CUIT: {row['cuit']:<15} "
                  f"× {row['cantidad']:>3} adj  "
                  f"${row['monto_total']:>12,.0f}  ({pct:.1f}%)")

        print(f"\n   📐 Índice de Concentración (HHI): {hhi} — {interpretacion_hhi}")

    # ── 3. EVOLUCIÓN TEMPORAL ──────────────────────────────────
    print("\n📅 EVOLUCIÓN MENSUAL")
    df_temporal = pd.DataFrame()
    if col_fecha:
        df_org["_mes"] = df_org[col_fecha].dt.to_period("M")
        df_temporal = (
            df_org.groupby("_mes")
            .agg(
                adjudicaciones=("_monto_num", "count"),
                monto=("_monto_num", "sum"),
            )
            .reset_index()
        )
        df_temporal["_mes"] = df_temporal["_mes"].astype(str)

        for _, row in df_temporal.iterrows():
            barra = "█" * min(int(row["adjudicaciones"] / max(df_temporal["adjudicaciones"].max(), 1) * 20), 20)
            print(f"   {row['_mes']}  {barra:<20}  "
                  f"{int(row['adjudicaciones']):>3} adj  "
                  f"${row['monto']:>12,.0f}")

    # ── 4. RED FLAGS HISTÓRICAS ────────────────────────────────
    print("\n🚨 RED FLAGS HISTÓRICAS")
    df_red_flags = pd.DataFrame()
    if "indicadores_riesgo" in df_org.columns:
        flags_series = df_org["indicadores_riesgo"].dropna()
        flags_series = flags_series[flags_series != "✅ Sin alertas"]

        if flags_series.empty:
            print("   ✅ Sin red flags detectadas en el período")
        else:
            # Descomponer los indicadores concatenados con " | "
            todos_flags = []
            for flags_str in flags_series:
                todos_flags.extend([f.strip() for f in str(flags_str).split("|")])

            conteo_flags = pd.Series(todos_flags).value_counts()
            for flag, count in conteo_flags.items():
                print(f"   {flag:<50}  ×{count}")

            df_red_flags = conteo_flags.reset_index()
            df_red_flags.columns = ["indicador", "frecuencia"]

    # Riesgo promedio
    if "indice_riesgo_licit" in df_org.columns:
        riesgo_prom = df_org["indice_riesgo_licit"].mean()
        alto  = (df_org["nivel_riesgo_licit"] == "Alto").sum()  if "nivel_riesgo_licit" in df_org.columns else 0
        medio = (df_org["nivel_riesgo_licit"] == "Medio").sum() if "nivel_riesgo_licit" in df_org.columns else 0
        print(f"\n   Índice de riesgo promedio: {riesgo_prom:.2f}/10")
        print(f"   🔴 Riesgo Alto:  {alto}  |  🟡 Riesgo Medio: {medio}")

    # ── 5. FLUJOS COMPLETOS BORA→COMPRAR→TGN ──────────────────
    print("\n💰 FLUJOS COMPLETOS (BORA→COMPRAR→TGN) — quiénes cobraron")
    if "alerta" in df_org.columns:
        df_flujos = df_org[
            df_org["alerta"].str.contains("FLUJO COMPLETO|TGN", na=False)
        ].copy()

        if df_flujos.empty:
            print("   Sin flujos completos detectados aún")
        else:
            cols_mostrar = [c for c in [
                col_cuit, "proveedor_adjudicado", "_monto_num",
                "monto_cobrado_tgn", col_fecha, "alerta"
            ] if c and c in df_flujos.columns]

            for _, row in df_flujos.head(10).iterrows():
                cuit_val = row.get(col_cuit, "")
                prov_val = row.get("proveedor_adjudicado", "")[:40] if "proveedor_adjudicado" in df_flujos.columns else ""
                monto_adj = row.get("_monto_num", 0)
                monto_tgn = row.get("monto_cobrado_tgn", "")
                alerta    = row.get("alerta", "")
                print(f"   CUIT: {cuit_val:<15}  {prov_val:<42}"
                      f"  Adj: ${monto_adj:>10,.0f}  {alerta}")

    # ── 6. EXPORTAR PERFIL A EXCEL ─────────────────────────────
    if exportar:
        nombre_archivo = re.sub(r"[^\w]", "_", nombre_busqueda.lower())[:40]
        carpeta_salida = os.path.join(DATA_DIR, "perfiles")
        os.makedirs(carpeta_salida, exist_ok=True)
        hoy = datetime.now().strftime("%Y-%m-%d")
        archivo_salida = os.path.join(carpeta_salida, f"perfil_organismo_{nombre_archivo}_{hoy}.xlsx")

        with pd.ExcelWriter(archivo_salida, engine="openpyxl") as writer:

            # Resumen general
            resumen = pd.DataFrame([{
                "organismo":               nombre_canonico,
                "total_adjudicaciones":    total_registros,
                "monto_total_ars":         monto_total,
                "monto_promedio_ars":      monto_promedio,
                "proveedores_distintos":   cuits_unicos,
                "cobraron_en_tgn":         cobraron_tgn,
                "flujos_completos":        flujos_completos,
                "fecha_inicio":            str(fecha_inicio.date()) if fecha_inicio else "",
                "fecha_fin":               str(fecha_fin.date()) if fecha_fin else "",
                "hhi_concentracion":       hhi if col_cuit else 0,
                "interpretacion_hhi":      interpretacion_hhi if col_cuit else "",
                "generado_en":             datetime.now().strftime("%Y-%m-%d %H:%M"),
            }])
            resumen.to_excel(writer, sheet_name="📋 Resumen", index=False)

            # Proveedores frecuentes
            if not df_proveedores.empty:
                df_proveedores.to_excel(writer, sheet_name="🏢 Proveedores", index=False)

            # Evolución temporal
            if not df_temporal.empty:
                df_temporal.drop(columns=["_mes"], errors="ignore")
                df_temporal.to_excel(writer, sheet_name="📅 Evolución Mensual", index=False)

            # Red flags
            if not df_red_flags.empty:
                df_red_flags.to_excel(writer, sheet_name="🚨 Red Flags", index=False)

            # Detalle completo filtrado
            df_export = df_org.drop(columns=["_monto_num", "_mes", "_archivo"], errors="ignore")
            df_export.to_excel(writer, sheet_name="📄 Detalle Completo", index=False)

        print(f"\n  💾 Perfil exportado: {archivo_salida}")

    print(f"\n{'='*60}\n")

    return {
        "nombre":           nombre_canonico,
        "total":            total_registros,
        "monto_total":      monto_total,
        "cuits_unicos":     cuits_unicos,
        "cobraron_tgn":     cobraron_tgn,
        "flujos_completos": flujos_completos,
        "hhi":              hhi if col_cuit else 0,
        "df_proveedores":   df_proveedores,
        "df_temporal":      df_temporal,
        "df_red_flags":     df_red_flags,
        "df_detalle":       df_org,
    }


# ─────────────────────────────────────────
# LISTAR TODOS LOS ORGANISMOS
# ─────────────────────────────────────────
def listar_organismos(df_historico):
    col = next(
        (c for c in ["organismo_contratante", "organismo", "unidad_ejecutora"]
         if c in df_historico.columns), None
    )
    if col is None:
        print("❌ No se encontró columna de organismo")
        return

    conteo = df_historico[col].value_counts()
    print(f"\n🏛️  ORGANISMOS DISPONIBLES ({len(conteo)} únicos)\n")
    print(f"  {'#':>4}  {'Organismo':<60}  {'Adj':>5}")
    print("  " + "-" * 75)
    for i, (org, count) in enumerate(conteo.items(), 1):
        print(f"  {i:>4}. {str(org)[:60]:<60}  {count:>5}")


# ─────────────────────────────────────────
# TOP N POR MONTO
# ─────────────────────────────────────────
def top_organismos_por_monto(df_historico, n=10):
    col_org = next(
        (c for c in ["organismo_contratante", "organismo"] if c in df_historico.columns), None
    )
    col_monto = next(
        (c for c in ["monto_adjudicado_bora", "monto_adjudicado"] if c in df_historico.columns), None
    )
    if not col_org:
        print("❌ No se encontró columna de organismo")
        return

    df_historico["_monto_num"] = df_historico[col_monto].apply(parsear_monto) if col_monto else 0.0

    grp = df_historico.groupby(col_org).agg(
        adjudicaciones=("_monto_num", "count"),
        monto_total=("_monto_num", "sum"),
    ).reset_index().sort_values("monto_total", ascending=False)

    print(f"\n🏆 TOP {n} ORGANISMOS POR MONTO ADJUDICADO\n")
    print(f"  {'#':>3}  {'Organismo':<55}  {'Adj':>5}  {'Monto Total ARS':>18}")
    print("  " + "-" * 90)
    for i, (_, row) in enumerate(grp.head(n).iterrows(), 1):
        print(f"  {i:>3}. {str(row[col_org])[:55]:<55}  "
              f"{int(row['adjudicaciones']):>5}  "
              f"${row['monto_total']:>17,.0f}")


# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Perfil por organismo — Monitor de Fenómenos Corruptivos"
    )
    parser.add_argument("organismo", nargs="?", help="Nombre (parcial) del organismo")
    parser.add_argument("--lista",   action="store_true", help="Listar todos los organismos")
    parser.add_argument("--top",     type=int, metavar="N", help="Top N organismos por monto")
    parser.add_argument("--no-exportar", action="store_true", help="No generar Excel del perfil")
    args = parser.parse_args()

    print("🔄 Cargando datos históricos...")
    df_hist = cargar_historico()

    if df_hist.empty:
        sys.exit(1)

    if args.lista:
        listar_organismos(df_hist)

    elif args.top:
        top_organismos_por_monto(df_hist, n=args.top)

    elif args.organismo:
        df_adj = cargar_adjudicaciones()
        generar_perfil(
            args.organismo,
            df_hist,
            df_adj,
            exportar=not args.no_exportar,
        )

    else:
        parser.print_help()
        print("\n💡 Ejemplos:")
        print("   python perfil_organismo.py 'MINISTERIO DE DEFENSA'")
        print("   python perfil_organismo.py --lista")
        print("   python perfil_organismo.py --top 10")
