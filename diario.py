import pandas as pd
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import os
import time
import re

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9",
    "Connection": "keep-alive",
}

# ─────────────────────────────────────────
# UTILIDADES
# ─────────────────────────────────────────
def get_con_reintentos(url, intentos=3, timeout=60, espera=15, verify_ssl=False):
    for i in range(intentos):
        try:
            print(f"  🔄 Intento {i+1}: {url[:80]}...")
            r = requests.get(url, headers=HEADERS, timeout=timeout, verify=verify_ssl)
            r.raise_for_status()
            return r
        except Exception as e:
            print(f"  ⚠️ Error intento {i+1}: {e}")
            if i < intentos - 1:
                time.sleep(espera)
    raise Exception(f"❌ Fallaron todos los intentos: {url}")

def extraer_cuit(texto):
    if not texto:
        return ""
    m = re.search(r'\b(\d{2}-\d{7,8}-\d{1})\b', texto)
    if m:
        return m.group(1)
    m = re.search(r'\b(20|23|24|27|30|33|34)\d{9}\b', texto)
    if m:
        return m.group(0)
    return ""

def extraer_monto(texto):
    if not texto:
        return ""
    m = re.search(
        r'(?:MONTO TOTAL ADJUDICADO|TOTAL ADJUDICADO|IMPORTE ADJUDICADO|MONTO ADJUDICADO)'
        r'[^\$\d]*\$?\s*([\d\.,]+)',
        texto, re.IGNORECASE
    )
    if m:
        return "$" + m.group(1).strip()
    return ""

def extraer_proveedor(texto):
    if not texto:
        return ""
    patrones = [
        r'PROVEEDOR ADJUDICADO[:\s]+([A-ZÁÉÍÓÚÑ][^\n\r]{3,80}?)(?:\s*[,\.]?\s*CUIT|\s*$)',
        r'ADJUDICATARIO[:\s]+([A-ZÁÉÍÓÚÑ][^\n\r]{3,80}?)(?:\s*[,\.]?\s*CUIT|\s*$)',
        r'adjudicada?\s+(?:la\s+firma\s+|a\s+la\s+firma\s+|a\s+)([A-ZÁÉÍÓÚÑ][^\n\r]{3,80}?)(?:\s*[,\.]?\s*CUIT|\s*[,\.])',
        r'adjudicó[^\n\r]*?(?:la\s+firma|a)\s+([A-ZÁÉÍÓÚÑ][^\n\r]{3,80}?)(?:\s*[,\.]?\s*CUIT|\s*[,\.])',
        r'firma\s+([A-ZÁÉÍÓÚÑ][^\n\r]{3,80}?)\s*[,\.]?\s*(?:CUIT|C\.U\.I\.T)',
        r'([A-ZÁÉÍÓÚÑ][^\n\r]{3,60}?)\s+CUIT\s*[Nn][°º\.]\s*\d{2}-\d',
    ]
    for patron in patrones:
        m = re.search(patron, texto, re.IGNORECASE)
        if m:
            resultado = m.group(1).strip().rstrip(".,- ")
            if len(resultado) > 3:
                return resultado
    return ""

def normalizar_nombre(nombre):
    if not nombre:
        return ""
    n = nombre.upper().strip()
    for p in ["S.A.U.", "S.A.", "S.R.L.", "S.A.S.", "S.C.", "LTDA.", " SA ", " SRL ", " SE ", "S.E."]:
        n = n.replace(p, " ")
    return re.sub(r'\s+', ' ', n).strip()

def carpeta_mes():
    hoy = datetime.now()
    carpeta = os.path.join("data", hoy.strftime("%Y-%m"))
    os.makedirs(carpeta, exist_ok=True)
    return carpeta

# ─────────────────────────────────────────
# OBTENER TEXTO AVISO BORA
# Usa la URL de vista de texto plano del BORA
# que SÍ devuelve contenido sin JavaScript
# ─────────────────────────────────────────
def obtener_texto_aviso_bora(aviso_id, fecha_pub):
    fecha_raw = fecha_pub.replace("-", "")
    # El BORA tiene endpoint de texto plano (no JS)
    urls = [
        f"https://www.boletinoficial.gob.ar/pdf/aviso/tercera/{aviso_id}/{fecha_raw}",
        f"https://www.boletinoficial.gob.ar/busqueda/texto?ids={aviso_id}&fecha={fecha_raw}",
        f"https://www.boletinoficial.gob.ar/detalleAviso/tercera/{aviso_id}/{fecha_raw}",
    ]
    for url in urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=20, verify=False)
            if r.status_code != 200:
                continue
            content_type = r.headers.get("Content-Type", "")
            # Si es PDF usar pdfminer
            if "pdf" in content_type.lower():
                try:
                    import io
                    from pdfminer.high_level import extract_text as pdf_extract
                    texto = pdf_extract(io.BytesIO(r.content))
                    if texto and len(texto) > 30:
                        return texto
                except Exception:
                    pass
                continue
            # Si es HTML buscar el div con el texto
            soup = BeautifulSoup(r.text, "html.parser")
            for selector in [
                {"id": "cuerpoAviso"},
                {"id": "textoAviso"},
                {"id": "aviso"},
                {"class": "aviso-cuerpo"},
                {"class": "texto-aviso"},
            ]:
                div = soup.find("div", selector)
                if div:
                    texto = div.get_text(separator=" ", strip=True)
                    if len(texto) > 50:
                        return texto
            # Fallback: buscar párrafos con CUIT o ADJUDICAD
            texto_completo = soup.get_text(separator=" ", strip=True)
            if any(kw in texto_completo.upper() for kw in ["CUIT", "ADJUDIC", "PROVEEDOR"]):
                return texto_completo
        except Exception as e:
            print(f"  ⚠️ URL texto falló ({url[:60]}): {e}")
    return ""

# ─────────────────────────────────────────
# SCRAPER 1A: BORA - ÍNDICE SECCIÓN 3RA
# ─────────────────────────────────────────
def extraer_bora_licitaciones():
    url = "https://www.boletinoficial.gob.ar/seccion/tercera"
    print("\n📰 Extrayendo BORA - Sección Tercera (índice)...")
    try:
        response = get_con_reintentos(url)
        soup = BeautifulSoup(response.text, "html.parser")

        datos = []
        categoria_actual = ""
        for elem in soup.find_all(["h5", "a"]):
            if elem.name == "h5":
                categoria_actual = elem.text.strip()
            elif elem.name == "a" and "/detalleAviso/tercera/" in elem.get("href", ""):
                href = elem["href"]
                partes = href.strip("/").split("/")
                aviso_id  = partes[-2] if len(partes) >= 2 else ""
                fecha_raw = partes[-1] if len(partes) >= 1 else ""
                fecha_pub = f"{fecha_raw[:4]}-{fecha_raw[4:6]}-{fecha_raw[6:]}" if len(fecha_raw) == 8 else fecha_raw

                lineas = [l.strip() for l in elem.text.strip().split("\n") if l.strip()]
                organismo    = lineas[0] if len(lineas) > 0 else ""
                tipo_proceso = lineas[1] if len(lineas) > 1 else ""
                es_adj = "ADJUDICACION" in categoria_actual.upper()

                datos.append({
                    "fecha_extraccion":  datetime.now().strftime("%Y-%m-%d"),
                    "fecha_publicacion": fecha_pub,
                    "organismo":         organismo,
                    "tipo_proceso":      tipo_proceso,
                    "categoria":         categoria_actual,
                    "aviso_id":          aviso_id,
                    "es_adjudicacion":   es_adj,
                    "link":              "https://www.boletinoficial.gob.ar" + href,
                    "fuente":            "BORA Sección 3ra",
                })

        adj = sum(1 for d in datos if d["es_adjudicacion"])
        print(f"  ✅ {len(datos)} avisos ({adj} adjudicaciones, {len(datos)-adj} licitaciones)")
        return pd.DataFrame(datos)

    except Exception as e:
        print(f"  ❌ Error BORA índice: {e}")
        return pd.DataFrame()

# ─────────────────────────────────────────
# SCRAPER 1B: BORA - DETALLE ADJUDICACIONES
# ─────────────────────────────────────────
def extraer_bora_adjudicaciones(df_bora_indice):
    print("\n🏆 Extrayendo detalle Adjudicaciones BORA...")

    if df_bora_indice.empty:
        return pd.DataFrame()

    adjudicaciones = df_bora_indice[df_bora_indice["es_adjudicacion"] == True]
    print(f"  📋 {len(adjudicaciones)} adjudicaciones a procesar")

    datos = []
    for _, row in adjudicaciones.iterrows():
        try:
            time.sleep(1)
            texto = obtener_texto_aviso_bora(row["aviso_id"], row["fecha_publicacion"])

            cuit      = extraer_cuit(texto)
            proveedor = extraer_proveedor(texto)
            monto     = extraer_monto(texto)

            datos.append({
                "fecha_extraccion":      datetime.now().strftime("%Y-%m-%d"),
                "fecha_publicacion":     row["fecha_publicacion"],
                "organismo_contratante": row["organismo"],
                "tipo_proceso":          row["tipo_proceso"],
                "aviso_id":              row["aviso_id"],
                "link":                  row["link"],
                "proveedor_adjudicado":  proveedor,
                "cuit_proveedor":        cuit,
                "monto_adjudicado":      monto,
                "texto_muestra":         texto[:300] if texto else "SIN TEXTO",
                "fuente":                "BORA Adjudicaciones",
            })

            estado = f"✅ CUIT:{cuit}" if cuit else "⚠️ sin CUIT"
            print(f"  {estado} | {proveedor[:40] if proveedor else 'sin proveedor'} | {monto}")

        except Exception as e:
            print(f"  ❌ Error aviso {row['aviso_id']}: {e}")
            datos.append({
                "fecha_extraccion":      datetime.now().strftime("%Y-%m-%d"),
                "fecha_publicacion":     row.get("fecha_publicacion", ""),
                "organismo_contratante": row.get("organismo", ""),
                "tipo_proceso":          row.get("tipo_proceso", ""),
                "aviso_id":              row.get("aviso_id", ""),
                "link":                  row.get("link", ""),
                "proveedor_adjudicado":  "",
                "cuit_proveedor":        "",
                "monto_adjudicado":      "",
                "texto_muestra":         f"ERROR: {e}",
                "fuente":                "BORA Adjudicaciones",
            })

    con_cuit = sum(1 for d in datos if d["cuit_proveedor"])
    print(f"  ✅ {len(datos)} procesadas | {con_cuit} con CUIT extraído")
    return pd.DataFrame(datos)

# ─────────────────────────────────────────
# SCRAPER 2: COMPRAR.GOB.AR
# ─────────────────────────────────────────
def extraer_comprar():
    url = "https://comprar.gob.ar/Compras.aspx?qs=W1HXHGHtH10="
    print("\n🛒 Extrayendo Comprar.gob.ar...")
    try:
        response = get_con_reintentos(url, timeout=60)
        soup  = BeautifulSoup(response.text, "html.parser")
        tabla = soup.find("table", {"id": "ctl00_CPH1_GridListaPliegosAperturaProxima"})

        if not tabla:
            print("  ⚠️ Tabla no encontrada")
            return pd.DataFrame()

        rows  = tabla.find_all("tr")
        datos = []
        for row in rows[1:]:
            cols = row.find_all("td")
            if len(cols) > 4:
                link_tag  = cols[0].find("a") or cols[1].find("a") or cols[2].find("a")
                href      = link_tag["href"] if link_tag else ""
                link_real = "https://comprar.gob.ar" + href if href.startswith("/") else url

                datos.append({
                    "fecha_extraccion": datetime.now().strftime("%Y-%m-%d"),
                    "nro_proceso":      cols[0].text.strip(),
                    "nombre_proceso":   cols[1].text.strip(),
                    "tipo_proceso":     cols[2].text.strip(),
                    "fecha_apertura":   cols[3].text.strip(),
                    "estado":           cols[4].text.strip(),
                    "unidad_ejecutora": cols[5].text.strip() if len(cols) > 5 else "",
                    "link":             link_real,
                    "fuente":           "Comprar.gob.ar",
                })

        print(f"  ✅ {len(datos)} procesos extraídos")
        return pd.DataFrame(datos)

    except Exception as e:
        print(f"  ❌ Error Comprar: {e}")
        return pd.DataFrame()

# ─────────────────────────────────────────
# SCRAPER 3: PRESUPUESTO ABIERTO (TGN)
# ─────────────────────────────────────────
def extraer_pagos_tgn():
    anio = datetime.now().year
    print("\n💰 Extrayendo Pagos TGN (Presupuesto Abierto)...")
    urls = [
        f"https://www.presupuestoabierto.gob.ar/sici/rest-api/credito/ejecutado?anio={anio}&categoria=beneficiario&formato=json&limit=100",
        f"https://www.presupuestoabierto.gob.ar/sici/rest-api/credito/ejecutado?anio={anio}&limit=100",
    ]
    for url in urls:
        try:
            response = get_con_reintentos(url, intentos=2, timeout=30, espera=5)
            data  = response.json()
            items = data if isinstance(data, list) else data.get("data", data.get("items", data.get("results", [])))
            if not items:
                continue

            datos = []
            for item in items[:100]:
                cuit   = str(item.get("cuit", item.get("beneficiario_cuit", ""))).strip()
                nombre = str(item.get("desc_beneficiario", item.get("beneficiario", item.get("nombre", "")))).strip()
                monto  = item.get("monto_pagado", item.get("pagado", item.get("monto", 0)))
                if nombre and nombre not in ("nan", "None", ""):
                    datos.append({
                        "fecha_extraccion": datetime.now().strftime("%Y-%m-%d"),
                        "anio":             anio,
                        "cuit":             cuit,
                        "beneficiario":     nombre,
                        "monto_pagado":     monto,
                        "fuente":           "Presupuesto Abierto TGN",
                    })

            if datos:
                print(f"  ✅ {len(datos)} beneficiarios extraídos")
                return pd.DataFrame(datos)

        except Exception as e:
            print(f"  ⚠️ TGN url falló: {e}")

    print("  ⚠️ TGN no disponible, se omite del cruce")
    return pd.DataFrame()

# ─────────────────────────────────────────
# CRUCE: Licitaciones → Adjudicadas → Pagos
# ─────────────────────────────────────────
def cruzar_fuentes(df_adjudicaciones, df_comprar, df_tgn):
    print("\n🔗 Cruzando: Licitaciones → Adjudicadas → Pagos...")

    if df_adjudicaciones.empty:
        print("  ⚠️ Sin adjudicaciones para cruzar")
        return pd.DataFrame()

    # Índice TGN por CUIT
    tgn_idx = {}
    if not df_tgn.empty:
        for _, r in df_tgn.iterrows():
            if r.get("cuit"):
                tgn_idx[str(r["cuit"])] = r

    comprar_lista = df_comprar.to_dict("records") if not df_comprar.empty else []

    STOP_WORDS = {
        "NACIONAL", "GENERAL", "ARGENTINA", "PUBLICA", "ADMINISTRACION",
        "DIRECCION", "SECRETARIA", "MINISTERIO", "AGENCIA", "INSTITUTO",
        "FEDERAL", "REPUBLICA", "ESTADO", "SERVICIO", "OFICINA", "SOCIAL",
    }

    resultados = []
    for _, adj in df_adjudicaciones.iterrows():
        cuit      = str(adj.get("cuit_proveedor", "")).strip()
        proveedor = adj.get("proveedor_adjudicado", "")
        organismo = adj.get("organismo_contratante", "")

        # Paso 1: ¿Está en Comprar? (licitación activa del mismo organismo)
        comprar_matches = []
        if organismo:
            palabras = [
                p for p in normalizar_nombre(organismo).split()
                if len(p) > 3 and p not in STOP_WORDS
            ]
            for c in comprar_lista:
                unidad_norm   = normalizar_nombre(c.get("unidad_ejecutora", ""))
                coincidencias = sum(1 for p in palabras if p in unidad_norm)
                if coincidencias >= 2:
                    comprar_matches.append(c)

        # Paso 2: ¿Cobró en TGN? (pago al CUIT del adjudicado)
        tgn_match = tgn_idx.get(cuit) if cuit else None

        en_comprar = len(comprar_matches) > 0
        en_tgn     = tgn_match is not None

        # Determinar etapa del flujo
        if cuit and en_tgn:
            etapa = "💰 ADJUDICADO + COBRÓ"
        elif cuit and en_comprar:
            etapa = "📋 ADJUDICADO + EN COMPRAR"
        elif cuit:
            etapa = "✅ ADJUDICADO (sin pago aún)"
        else:
            etapa = "⚠️ SIN CUIT EXTRAÍDO"

        if en_comprar and en_tgn and cuit:
            alerta = "🚨 FLUJO COMPLETO: BORA→COMPRAR→TGN"
        elif en_tgn and cuit:
            alerta = "🔶 BORA + TGN (cobró)"
        elif en_comprar:
            alerta = "🔷 BORA + COMPRAR"
        else:
            alerta = "📋 SOLO BORA"

        resultados.append({
            # Adjudicación BORA
            "fecha":                    adj.get("fecha_extraccion"),
            "organismo_contratante":    organismo,
            "tipo_proceso_bora":        adj.get("tipo_proceso"),
            "link_bora":                adj.get("link"),
            # Proveedor adjudicado
            "proveedor_adjudicado":     proveedor,
            "cuit_proveedor":           cuit,
            "monto_adjudicado_bora":    adj.get("monto_adjudicado"),
            # Comprar
            "en_comprar":               "✅ SÍ" if en_comprar else "❌ NO",
            "procesos_comprar":         len(comprar_matches),
            "unidad_comprar":           comprar_matches[0].get("unidad_ejecutora", "") if comprar_matches else "",
            "nro_proceso_comprar":      comprar_matches[0].get("nro_proceso", "") if comprar_matches else "",
            # TGN
            "cobro_en_tgn":             "✅ SÍ" if en_tgn else "❌ NO",
            "beneficiario_tgn":         tgn_match["beneficiario"] if en_tgn else "",
            "monto_cobrado_tgn":        tgn_match["monto_pagado"] if en_tgn else "",
            # Resumen
            "etapa":                    etapa,
            "alerta":                   alerta,
        })

    df = pd.DataFrame(resultados)
    if not df.empty:
        orden = {
            "🚨 FLUJO COMPLETO: BORA→COMPRAR→TGN": 0,
            "🔶 BORA + TGN (cobró)":               1,
            "🔷 BORA + COMPRAR":                   2,
            "📋 SOLO BORA":                        3,
        }
        df["_orden"] = df["alerta"].map(orden).fillna(9)
        df = df.sort_values("_orden").drop(columns=["_orden"]).reset_index(drop=True)

    print(f"  ✅ {len(df)} registros en el flujo")
    if not df.empty:
        for alerta, count in df["alerta"].value_counts().items():
            print(f"     {alerta}: {count}")
    return df

# ─────────────────────────────────────────
# GUARDAR DOS EXCELS
# Excel 1: Operativo diario (todas las pestañas)
# Excel 2: Flujo Licitaciones→Adjudicadas→Pagos
# ─────────────────────────────────────────
def guardar_excels(df_cruce, df_adjudicaciones, df_licitaciones, df_comprar, df_tgn):
    carpeta = carpeta_mes()
    hoy     = datetime.now().strftime("%Y-%m-%d")

    # ── Excel 1: Reporte operativo completo ──
    archivo1 = os.path.join(carpeta, f"reporte_{hoy}.xlsx")
    with pd.ExcelWriter(archivo1, engine="openpyxl") as writer:
        if not df_cruce.empty:
            df_cruce.to_excel(writer, sheet_name="🚨 Flujo Completo", index=False)
        if not df_adjudicaciones.empty:
            df_adjudicaciones.to_excel(writer, sheet_name="🏆 Adjudicaciones", index=False)
        if not df_licitaciones.empty:
            df_licitaciones.to_excel(writer, sheet_name="📰 BORA Licitaciones", index=False)
        if not df_comprar.empty:
            df_comprar.to_excel(writer, sheet_name="🛒 Comprar", index=False)
        if not df_tgn.empty:
            df_tgn.to_excel(writer, sheet_name="💰 TGN", index=False)
    print(f"  ✅ Reporte completo: {archivo1}")

    # ── Excel 2: Solo el flujo Licitación→Adjudicación→Pago ──
    archivo2 = os.path.join(carpeta, f"flujo_licitaciones_{hoy}.xlsx")
    with pd.ExcelWriter(archivo2, engine="openpyxl") as writer:
        # Pestaña 1: Solo adjudicaciones con CUIT (el dato clave)
        if not df_adjudicaciones.empty:
            df_con_cuit = df_adjudicaciones[
                df_adjudicaciones["cuit_proveedor"].astype(bool)
            ].copy()
            if not df_con_cuit.empty:
                df_con_cuit.to_excel(writer, sheet_name="✅ Adjudicados con CUIT", index=False)

        # Pestaña 2: Flujo completo cruzado
        if not df_cruce.empty:
            df_cruce.to_excel(writer, sheet_name="🔗 Flujo Cruzado", index=False)

        # Pestaña 3: Los que ya cobraron (en TGN)
        if not df_cruce.empty:
            df_cobro = df_cruce[df_cruce["cobro_en_tgn"] == "✅ SÍ"].copy()
            if not df_cobro.empty:
                df_cobro.to_excel(writer, sheet_name="💰 Cobraron en TGN", index=False)

        # Pestaña 4: Licitaciones abiertas en Comprar (pendientes de adjudicar)
        if not df_comprar.empty:
            df_comprar.to_excel(writer, sheet_name="⏳ Licitaciones Abiertas", index=False)

    print(f"  ✅ Flujo licitaciones: {archivo2}")
    return archivo1, archivo2

# ─────────────────────────────────────────
# EJECUCIÓN PRINCIPAL
# ─────────────────────────────────────────
if __name__ == "__main__":
    print("🚀 Ciclo Integrado: BORA + Comprar + TGN")
    print(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    # Instalar pdfminer si no está
    try:
        import pdfminer
    except ImportError:
        print("📦 Instalando pdfminer.six...")
        os.system("pip install pdfminer.six --break-system-packages -q")

    df_bora_indice    = extraer_bora_licitaciones()
    df_adjudicaciones = extraer_bora_adjudicaciones(df_bora_indice)

    df_licitaciones = pd.DataFrame()
    if not df_bora_indice.empty:
        df_licitaciones = df_bora_indice[
            df_bora_indice["es_adjudicacion"] == False
        ].copy().reset_index(drop=True)

    df_comprar = extraer_comprar()
    df_tgn     = extraer_pagos_tgn()
    df_cruce   = cruzar_fuentes(df_adjudicaciones, df_comprar, df_tgn)

    guardar_excels(df_cruce, df_adjudicaciones, df_licitaciones, df_comprar, df_tgn)

    con_cuit = 0
    if not df_adjudicaciones.empty:
        con_cuit = df_adjudicaciones["cuit_proveedor"].astype(bool).sum()

    print("\n📊 RESUMEN FINAL:")
    print(f"   Licitaciones BORA:  {len(df_licitaciones)}")
    print(f"   Adjudicaciones:     {len(df_adjudicaciones)} ({con_cuit} con CUIT)")
    print(f"   Procesos Comprar:   {len(df_comprar)}")
    print(f"   Beneficiarios TGN:  {len(df_tgn)}")
    print(f"   Flujo cruzado:      {len(df_cruce)}")
