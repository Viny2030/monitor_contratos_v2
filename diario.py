import pandas as pd
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import os
import time

# ─────────────────────────────────────────
# FUNCIÓN DE REQUEST CON REINTENTOS
# ─────────────────────────────────────────
def get_con_reintentos(url, intentos=3, timeout=60, espera=15, verify_ssl=False):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-AR,es;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }
    for i in range(intentos):
        try:
            print(f"🔄 Intento {i+1} de {intentos}: {url}")
            response = requests.get(url, headers=headers, timeout=timeout, verify=verify_ssl)
            response.raise_for_status()
            return response
        except Exception as e:
            print(f"⚠️ Error en intento {i+1}: {e}")
            if i < intentos - 1:
                time.sleep(espera)
    raise Exception(f"❌ Fallaron todos los intentos para: {url}")

# ─────────────────────────────────────────
# SCRAPER COMPRAR.GOB.AR
# ─────────────────────────────────────────
def extraer_licitaciones_scraper():
    url = "https://comprar.gob.ar/Compras.aspx?qs=W1HXHGHtH10="
    try:
        response = get_con_reintentos(url, intentos=3, timeout=60, espera=15, verify_ssl=False)
        
        print(f"✅ Status code: {response.status_code}")
        print(f"✅ Largo del HTML: {len(response.text)}")
        
        soup = BeautifulSoup(response.text, "html.parser")
        tabla = soup.find("table", {"id": "ctl00_CPH1_GridListaPliegosAperturaProxima"})
        
        print(f"✅ Tabla encontrada: {tabla is not None}")
        
        if not tabla:
            todas = soup.find_all("table")
            print(f"⚠️ Tablas en la página: {len(todas)}")
            for t in todas:
                print(f"   - id: {t.get('id')} | class: {t.get('class')}")
            return pd.DataFrame()
        
        rows = tabla.find_all("tr")
        print(f"✅ Filas encontradas: {len(rows)}")

        # Ver estructura de columnas
        encabezados = rows[0].find_all("th")
        for i, h in enumerate(encabezados):
            print(f"   Columna {i}: {h.text.strip()}")

        if len(rows) > 1:
            cols_test = rows[1].find_all("td")
            for i, c in enumerate(cols_test):
                print(f"   Col {i}: {c.text.strip()[:50]}")
        
        datos = []
        for row in rows[1:21]:
            cols = row.find_all("td")
            if len(cols) > 4:
                link_tag = cols[2].find("a")
                href = link_tag["href"] if link_tag else ""
                link_real = "https://comprar.gob.ar" + href if href.startswith("/") else url
                datos.append({
                    "fecha": datetime.now().strftime("%Y-%m-%d"),
                    "nro_proceso": cols[1].text.strip(),
                    "detalle": cols[2].text.strip(),
                    "tipo_proceso": cols[3].text.strip(),
                    "fecha_apertura": cols[4].text.strip(),
                    "link": link_real,
                    "fuente": "Scraper Comprar.gob.ar",
                })
        
        print(f"✅ Registros extraídos: {len(datos)}")
        return pd.DataFrame(datos)
        
    except Exception as e:
        print(f"❌ Error en scraper: {e}")
        import traceback
        traceback.print_exc()
        return pd.DataFrame()

# ─────────────────────────────────────────
# EJECUCIÓN PRINCIPAL
# ─────────────────────────────────────────
if __name__ == "__main__":
    print("🚀 Iniciando extracción...")
    
    df = extraer_licitaciones_scraper()
    
    print(f"📊 Total registros obtenidos: {len(df)}")
    
    if not df.empty:
        print(df.head())
        os.makedirs("data", exist_ok=True)
        archivo = f"data/reporte_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
        df.to_excel(archivo, index=False)
        print(f"✅ Archivo guardado: {archivo}")
    else:
        print("⚠️ No hay datos para guardar, el DataFrame está vacío")
