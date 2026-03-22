"""
test_apis_oficiales.py
======================
Script de verificación independiente.
Corre cada API, muestra resultado y un diagnóstico.
NO modifica ni importa nada del proyecto existente.

Uso:
    python test_apis_oficiales.py
    python test_apis_oficiales.py --solo bora
    python test_apis_oficiales.py --cuit 30-50000427-3
"""

import sys
import argparse
import pandas as pd
from datetime import datetime

# Importamos solo el módulo nuevo
from apis_oficiales import (
    obtener_bora_normativa_api,
    obtener_comprar_api,
    obtener_contrat_ocds_api,
    obtener_tgn_ejecucion_api,
    validar_cuit_api,
    validar_cuits_lote,
    obtener_sipro_api,
    obtener_todo_api,
)

SEP = "─" * 55


def _titulo(texto):
    print(f"\n{SEP}")
    print(f"  {texto}")
    print(SEP)


def _resultado(df, columnas_muestra=None):
    if df is None or df.empty:
        print("  ⚠️  Sin datos")
        return
    print(f"  ✅ {len(df)} registros | columnas: {list(df.columns)}")
    cols = columnas_muestra or df.columns[:5].tolist()
    cols = [c for c in cols if c in df.columns]
    print(df[cols].head(3).to_string(index=False))


def test_bora():
    _titulo("1. BORA — Normativa sección tercera (hoy)")
    df = obtener_bora_normativa_api(seccion="tercera")
    _resultado(df, ["fecha_publicacion", "tipo_norma", "organismo", "titulo"])
    return df


def test_bora_busqueda(texto="licitacion"):
    _titulo(f"1b. BORA — búsqueda por texto '{texto}'")
    df = obtener_bora_normativa_api(texto=texto, seccion="tercera", limit=20)
    _resultado(df, ["fecha_publicacion", "organismo", "titulo"])
    return df


def test_comprar_adj(anio=2020):
    _titulo(f"2. COMPR.AR — Adjudicaciones {anio}")
    df = obtener_comprar_api(anio=anio, tipo="adjudicaciones", limit=20)
    _resultado(df, ["nro_proceso", "tipo_proceso", "organismo", "monto", "cuit_proveedor"])
    return df


def test_comprar_conv(anio=2020):
    _titulo(f"2b. COMPR.AR — Convocatorias {anio}")
    df = obtener_comprar_api(anio=anio, tipo="convocatorias", limit=20)
    _resultado(df, ["nro_proceso", "nombre_proceso", "tipo_proceso", "organismo"])
    return df


def test_contrat_ocds():
    _titulo("3. CONTRAT.AR — Obra pública OCDS")
    df = obtener_contrat_ocds_api(limit=20)
    _resultado(df, ["ocid", "titulo", "organismo", "monto_contrato", "proveedor"])
    return df


def test_tgn_ejecucion():
    _titulo("4. TGN — /ejecucion (con CUIT beneficiario)")
    df = obtener_tgn_ejecucion_api()
    _resultado(df, ["anio", "jurisdiccion", "cuit_beneficiario", "beneficiario", "monto_pagado"])
    if not df.empty and "cuit_beneficiario" in df.columns:
        con_cuit = df["cuit_beneficiario"].notna().sum()
        print(f"\n  📊 Registros con CUIT beneficiario: {con_cuit}/{len(df)}")
    return df


def test_cuit(cuit="30-50000427-3"):
    _titulo(f"5. CUIT — validación de {cuit}")
    info = validar_cuit_api(cuit)
    for k, v in info.items():
        print(f"  {k:<25}: {v}")
    return info


def test_sipro(nombre="TECHINT"):
    _titulo(f"6. SIPRO — búsqueda proveedor '{nombre}'")
    df = obtener_sipro_api(nombre=nombre, limit=10)
    _resultado(df, ["cuit", "razon_social", "domicilio", "rubro", "estado_sipro"])
    return df


def test_compatibilidad_diario():
    """
    Verifica que las columnas de las APIs son compatibles
    con el esquema que usa diario.py para el cruce de fuentes.
    """
    _titulo("7. Test de compatibilidad con diario.py")

    schema_diario = {
        "df_adjudicaciones": ["organismo_contratante", "cuit_proveedor",
                              "proveedor_adjudicado", "monto_adjudicado", "link"],
        "df_comprar":        ["nro_proceso", "nombre_proceso", "tipo_proceso",
                              "unidad_ejecutora", "link"],
        "df_tgn":            ["cuit", "beneficiario", "monto_pagado"],
    }

    df_comp = obtener_comprar_api(anio=2020, tipo="adjudicaciones", limit=5)
    df_tgn  = obtener_tgn_ejecucion_api()

    for nombre_schema, cols_requeridas in schema_diario.items():
        if nombre_schema == "df_comprar":
            df_test = df_comp
        elif nombre_schema == "df_tgn":
            df_test = df_tgn
        else:
            continue

        if df_test.empty:
            print(f"  ⚠️  {nombre_schema}: sin datos para verificar")
            continue

        faltantes = [c for c in cols_requeridas if c not in df_test.columns]
        if faltantes:
            print(f"  🟡 {nombre_schema}: faltan columnas {faltantes}")
            print(f"     Columnas disponibles: {list(df_test.columns)}")
        else:
            print(f"  ✅ {nombre_schema}: todas las columnas requeridas presentes")


def test_todo():
    _titulo("COMPLETO — Todas las APIs")
    resultados = obtener_todo_api()
    print()
    for nombre, df in resultados.items():
        n = len(df) if not df.empty else 0
        icono = "✅" if n > 0 else "⚠️ "
        print(f"  {icono} {nombre:<25} {n:>6} registros")
    return resultados


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Test de APIs oficiales para monitor_contratos_v2"
    )
    parser.add_argument("--solo", choices=["bora","comprar","contrat","tgn","cuit","sipro","compat","todo"],
                        help="Correr solo una API específica")
    parser.add_argument("--cuit", default="30-50000427-3",
                        help="CUIT a validar (default: 30-50000427-3)")
    parser.add_argument("--nombre", default="TECHINT",
                        help="Nombre para buscar en SIPRO")
    parser.add_argument("--anio", type=int, default=2020,
                        help="Año para COMPR.AR (2015-2020)")
    args = parser.parse_args()

    print(f"\n🧪 TEST APIs Oficiales — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    if args.solo == "bora":
        test_bora()
        test_bora_busqueda()
    elif args.solo == "comprar":
        test_comprar_adj(args.anio)
        test_comprar_conv(args.anio)
    elif args.solo == "contrat":
        test_contrat_ocds()
    elif args.solo == "tgn":
        test_tgn_ejecucion()
    elif args.solo == "cuit":
        test_cuit(args.cuit)
    elif args.solo == "sipro":
        test_sipro(args.nombre)
    elif args.solo == "compat":
        test_compatibilidad_diario()
    elif args.solo == "todo":
        test_todo()
    else:
        # Corre todo en orden
        test_bora()
        test_comprar_adj(args.anio)
        test_contrat_ocds()
        test_tgn_ejecucion()
        test_cuit(args.cuit)
        test_sipro(args.nombre)
        test_compatibilidad_diario()

    print(f"\n{SEP}")
    print("  Fin del test")
    print(SEP + "\n")


if __name__ == "__main__":
    main()
