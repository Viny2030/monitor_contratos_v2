import os
import re
import time
import shutil
import warnings
import requests
import urllib3
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime
from analisis import analizar_boletin

# Suprimir warnings de SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

DATA_DIR = os.path.join(os.getcwd(), "data")

def obtener_directorio_mes_actual():
    ahora = datetime.now()
    mes_carpeta = ahora.strftime("%Y-%m")
    ruta_mes = os.path.join(DATA_DIR, mes_carpeta)
    if not os.path.exists(ruta_mes):
        os.makedirs(ruta_mes)
    return ruta_mes

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
}

# --- NUEVAS FUNCIONES DE TRAZABILIDAD ---

def extraer_cuit_profundo(url):
    """Navega al link para extraer el CUIT del adjudicatario."""
    if "comprar.gob.ar" not in url or url.endswith("W1HXHGHtH10="):
        return "n/a"
    try:
        # Timeout corto para no ralentizar el workflow de GitHub
        resp = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        soup = BeautifulSoup(resp.text, "html.parser")
        texto = soup.get_text()
        # Regex para CUIT argentino
        match = re.search(r'\b(20|23|24|27|30|33)-?\d{8}-?\d\b', texto)
        return match.group(0) if match else "n/a"
    except:
        return "n/a"

def generar_link_tgn(cuit):
    """Genera link de consulta en Tesorería General de la Nación."""
    if not cuit or cuit == "n/a":
        return "n/a"
    cuit_clean = cuit.replace("-", "")
    return f"https://www.tesoreria.gob.ar/pagos/consultas/beneficiario?cuit={cuit_clean}"

# --- SCRAPERS EXISTENTES (Mantenidos) ---

def get_con_reintentos(url, intentos=3, timeout=60, espera=10, verify_ssl=False):
    ultimo_error = None
    for i in range(1, intentos + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout, verify=verify_ssl)
            resp.raise_for_status()
            return resp
        except Exception as e:
            ultimo_error = e
            time.sleep(espera)
    raise ultimo_error

def extraer_licitaciones_scraper():
    url = "https://comprar.gob.ar/Compras.aspx?qs=W1HXHGHtH10="
    try:
        response = get_con_reintentos(url, verify_ssl=False)
        soup = BeautifulSoup(response.text, "html.parser")
        tabla = soup.find("table", {"id": "ctl00_CPH1_GridLicitaciones"}) or soup.find("table")
        if not tabla: return pd.DataFrame()
        rows = tabla.find_all("tr")
        datos = []
        for row in rows[1:21]: # Limitamos a 20 para eficiencia en GitHub Actions
            cols = row.find_all("td")
            if len(cols) > 4:
                link_tag = cols[2].find("a")
                datos.append({
                    "fecha": datetime.now().strftime("%Y-%m-%d"),
                    "nro_proceso": cols[1].text.strip(),
                    "detalle": cols[2].text.strip(),
                    "tipo_proceso": cols[3].text.strip(),
                    "fecha_apertura": cols[4].text.strip(),
                    "link": "https://comprar.gob.ar" + link_tag["href"] if link_tag else url,
                    "fuente": "Scraper Comprar.gob.ar",
                })
        return pd.DataFrame(datos)
    except:
        return pd.DataFrame()

# ... (Mantener extraer_api_datos_gob, extraer_boletin_oficial y extraer_argentinacompra igual) ...

def extraer_licitaciones():
    fuentes = [
        ("Comprar.gob.ar (scraper)", extraer_licitaciones_scraper),
        ("API datos.gob.ar", extraer_api_datos_gob),
        ("Boletín Oficial", extraer_boletin_oficial),
        ("ArgentinaCompra", extraer_argentinacompra),
    ]
    for nombre, funcion in fuentes:
        df = funcion()
        if not df.empty: return df
    return pd.DataFrame()

# ==========================================
# PROCESO PRINCIPAL (ACTUALIZADO)
# ==========================================
def ejecutar_robot():
    start_time = datetime.now()
    print(f"\n--- INICIO PROCESO INTEGRADO BORA-COMPRAR-TGN ---")

    directorio_mes = obtener_directorio_mes_actual()
    df_portal = extraer_licitaciones()

    if df_portal.empty:
        df_portal = pd.DataFrame([{"fecha": datetime.now().strftime("%Y-%m-%d"), "detalle": "Sin datos", "fuente": "ninguna"}])
    else:
        df_portal["detalle"] = df_portal["detalle"].fillna("Sin descripción")
        
        # --- CRUCE DE DATOS ---
        print("🔗 Paso 1: Extrayendo CUITs de adjudicados...")
        df_portal["cuit_proveedor"] = df_portal["link"].apply(extraer_cuit_profundo)
        
        print("🔗 Paso 2: Generando trazabilidad con Tesorería (TGN)...")
        df_portal["link_tesoreria"] = df_portal["cuit_proveedor"].apply(generar_link_tgn)

    print("🧠 Aplicando Matriz de Análisis XAI...")
    try:
        df_final, path_excel, _ = analizar_boletin(df_portal, directorio_mes)
    except:
        df_final, path_excel, _ = analizar_boletin(df_portal)

    print(f"✨ Proceso finalizado. Tiempo: {(datetime.now() - start_time).seconds}s")

if __name__ == "__main__":
    ejecutar_robot()
