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

# --- FUNCIONES DE TRAZABILIDAD ---

def extraer_cuit_profundo(url):
    if "comprar.gob.ar" not in url or "W1HXHGHtH10=" in url:
        return "n/a"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10, verify=False)
        soup = BeautifulSoup(resp.text, "html.parser")
        texto = soup.get_text()
        match = re.search(r'\b(20|23|24|27|30|33)-?\d{8}-?\d\b', texto)
        return match.group(0) if match else "n/a"
    except:
        return "n/a"

def generar_link_tgn(cuit):
    if not cuit or cuit == "n/a":
        return "n/a"
    cuit_clean = cuit.replace("-", "")
    return f"https://www.tesoreria.gob.ar/pagos/consultas/beneficiario?cuit={cuit_clean}"

def get_con_reintentos(url, intentos=3, timeout=30, espera=5, verify_ssl=False):
    for i in range(1, intentos + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout, verify=verify_ssl)
            resp.raise_for_status()
            return resp
        except:
            time.sleep(espera)
    return None

# --- FUENTES DE DATOS ---

def extraer_licitaciones_scraper():
    url = "https://comprar.gob.ar/Compras.aspx?qs=W1HXHGHtH10="
    try:
        resp = get_con_reintentos(url)
        if not resp: return pd.DataFrame()
        soup = BeautifulSoup(resp.text, "html.parser")
        tabla = soup.find("table", {"id": "ctl00_CPH1_GridLicitaciones"}) or soup.find("table")
        if not tabla: return pd.DataFrame()
        rows = tabla.find_all("tr")
        datos = []
        for row in rows[1:15]:
            cols = row.find_all("td")
            if len(cols) > 4:
                link_tag = cols[2].find("a")
                datos.append({
                    "fecha": datetime.now().strftime("%Y-%m-%d"),
                    "nro_proceso": cols[1].text.strip(),
                    "detalle": cols[2].text.strip(),
                    "tipo_proceso": cols[3].text.strip(),
                    "link": "https://comprar.gob.ar" + link_tag["href"] if link_tag else url,
                    "fuente": "Scraper Comprar.gob.ar",
                })
        return pd.DataFrame(datos)
    except:
        return pd.DataFrame()

def extraer_api_datos_gob():
    # Función simplificada para evitar NameError
    print("🔁 Intentando API datos.gob.ar...")
    return pd.DataFrame()

def extraer_boletin_oficial():
    # Función simplificada para evitar NameError
    print("🔁 Intentando Boletín Oficial...")
    return pd.DataFrame()

def extraer_argentinacompra():
    print("🔁 Intentando ArgentinaCompra...")
    return pd.DataFrame()

# --- ORQUESTADOR ---

def extraer_licitaciones():
    fuentes = [
        ("Comprar.gob.ar", extraer_licitaciones_scraper),
        ("API datos.gob.ar", extraer_api_datos_gob),
        ("Boletín Oficial", extraer_boletin_oficial),
        ("ArgentinaCompra", extraer_argentinacompra),
    ]
    for nombre, funcion in fuentes:
        df = funcion()
        if not df.empty:
            print(f"✅ Datos obtenidos de: {nombre}")
            return df
    return pd.DataFrame()

# --- PROCESO PRINCIPAL ---

def ejecutar_robot():
    start_time = datetime.now()
    print(f"\n--- INICIO PROCESO INTEGRADO BORA-COMPRAR-TGN ---")
    
    directorio_mes = obtener_directorio_mes_actual()
    df_portal = extraer_licitaciones()

    if df_portal.empty:
        print("⚠️ No se obtuvieron datos de ninguna fuente.")
        df_portal = pd.DataFrame([{"fecha": datetime.now().strftime("%Y-%m-%d"), "detalle": "Sin datos hoy", "fuente": "ninguna"}])
    else:
        print("🔗 Relacionando con CUITs y Tesorería...")
        # Limitar a los primeros 5 para que el Action no tarde mucho en esta prueba
        df_portal["cuit_proveedor"] = df_portal["link"].head(5).apply(extraer_cuit_profundo)
        df_portal["cuit_proveedor"] = df_portal["cuit_proveedor"].fillna("n/a")
        df_portal["link_tesoreria"] = df_portal["cuit_proveedor"].apply(generar_link_tgn)

    print("🧠 Aplicando Matriz de Análisis XAI...")
    try:
        # Intentamos pasar el directorio para el archivado mensual
        analizar_boletin(df_portal, directorio_mes)
    except Exception as e:
        print(f"⚠️ Error en análisis: {e}. Intentando modo simple.")
        analizar_boletin(df_portal)

    print(f"✨ Fin. Tiempo: {(datetime.now() - start_time).seconds}s")

if __name__ == "__main__":
    ejecutar_robot()
