def extraer_licitaciones_scraper():
    url = "https://comprar.gob.ar/Compras.aspx?qs=W1HXHGHtH10="
    try:
        response = get_con_reintentos(url, intentos=3, timeout=60, espera=15, verify_ssl=False)
        soup = BeautifulSoup(response.text, "html.parser")
        tabla = soup.find("table", {"id": "ctl00_CPH1_GridLicitaciones"})
        if not tabla: return pd.DataFrame()
        
        rows = tabla.find_all("tr")
        datos = []
        for row in rows[1:21]: # Procesamos 20 para probar
            cols = row.find_all("td")
            if len(cols) > 4:
                # CLAVE: Buscamos el link específico del detalle
                link_tag = cols[2].find("a")
                href = link_tag["href"] if link_tag else ""
                
                # Construimos el link real
                link_real = "https://comprar.gob.ar" + href if href.startswith("/") else url
                
                datos.append({
                    "fecha": datetime.now().strftime("%Y-%m-%d"),
                    "nro_proceso": cols[1].text.strip(),
                    "detalle": cols[2].text.strip(),
                    "tipo_proceso": cols[3].text.strip(),
                    "fecha_apertura": cols[4].text.strip(),
                    "link": link_real, # <--- AHORA SÍ ES EL LINK INDIVIDUAL
                    "fuente": "Scraper Comprar.gob.ar",
                })
        return pd.DataFrame(datos)
    except Exception as e:
        print(f"❌ Error en scraper: {e}")
        return pd.DataFrame()
