from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import pandas as pd
import os

@asynccontextmanager
async def lifespan(app: FastAPI):
    import threading
    def correr_diario():
        try:
            print("\n🚀 Iniciando ciclo diario automático...")
            from diario import (extraer_bora_licitaciones, extraer_bora_adjudicaciones,
                extraer_comprar, extraer_pagos_tgn, cruzar_fuentes, guardar_excels)
            df_bora    = extraer_bora_licitaciones()
            df_adj     = extraer_bora_adjudicaciones(df_bora)
            df_licit   = (df_bora[df_bora["es_adjudicacion"] == False].copy().reset_index(drop=True)
                          if not df_bora.empty else pd.DataFrame())
            df_comprar = extraer_comprar()
            df_tgn     = extraer_pagos_tgn()
            df_cruce   = cruzar_fuentes(df_adj, df_comprar, df_tgn)
            guardar_excels(df_cruce, df_adj, df_licit, df_comprar, df_tgn)
            print("✅ Ciclo diario automático completado.\n")
        except Exception as e:
            print(f"⚠️ Ciclo diario automático falló: {e}\n")
    threading.Thread(target=correr_diario, daemon=True).start()
    yield

app = FastAPI(title="Monitor XAI - Ph.D. Monteverde", description="Algoritmos contra la Corrupción", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

DATA_DIR = "/app/data" if os.path.exists("/app") else "data"
os.makedirs(DATA_DIR, exist_ok=True)
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")
_df_cache = None

def buscar_todos_los_xlsx(base_dir):
    archivos = []
    for root, dirs, files in os.walk(base_dir):
        for f in files:
            if f.startswith("reporte_202") and f.endswith(".xlsx"):
                archivos.append(os.path.join(root, f))
    archivos.sort(key=lambda x: os.path.getmtime(x), reverse=True)
    return archivos

def etiqueta_archivo(ruta):
    partes = ruta.replace("\\", "/").split("/")
    return f"{partes[-2]} / {partes[-1]}" if len(partes) >= 3 else partes[-1]

def leer_hoja(ruta, hojas_preferidas):
    try:
        xl = pd.ExcelFile(ruta)
        hoja = next((h for h in hojas_preferidas if h in xl.sheet_names), None)
        if not hoja:
            return []
        df = xl.parse(hoja).fillna("").astype(str)
        return df.to_dict(orient="records")
    except Exception as e:
        print(f"Error leyendo {ruta}: {e}")
        return []

def cargar_ultimo_reporte():
    global _df_cache
    if _df_cache is not None and not _df_cache.empty:
        return _df_cache
    archivos = buscar_todos_los_xlsx(DATA_DIR)
    if not archivos:
        return pd.DataFrame()
    try:
        xl = pd.ExcelFile(archivos[0])
        hojas = ["🚨 Flujo Completo", "🔗 Flujo Cruzado", "Sheet1"]
        hoja = next((h for h in hojas if h in xl.sheet_names), xl.sheet_names[0])
        return xl.parse(hoja)
    except Exception as e:
        print(f"Error cargando reporte: {e}")
        return pd.DataFrame()

def set_cache(df):
    global _df_cache
    _df_cache = df

def guardar_excels_con_fecha(df_cruce, df_adj, df_licit, df_comprar, df_tgn, fecha_str):
    from analisis import analizar_adjudicaciones
    mes_str = fecha_str[:7]
    carpeta = os.path.join(DATA_DIR, mes_str)
    os.makedirs(carpeta, exist_ok=True)
    df_cruce_con_riesgo = pd.DataFrame()
    if not df_cruce.empty:
        try:
            df_cruce_con_riesgo = analizar_adjudicaciones(df_cruce, df_tgn)
        except Exception:
            df_cruce_con_riesgo = df_cruce.copy()
    archivo1 = os.path.join(carpeta, f"reporte_{fecha_str}.xlsx")
    with pd.ExcelWriter(archivo1, engine="openpyxl") as writer:
        df_out = df_cruce_con_riesgo if not df_cruce_con_riesgo.empty else df_cruce
        if not df_out.empty:
            df_out.to_excel(writer, sheet_name="🚨 Flujo Completo", index=False)
        if not df_adj.empty:
            df_adj.to_excel(writer, sheet_name="🏆 Adjudicaciones", index=False)
        if not df_licit.empty:
            df_licit.to_excel(writer, sheet_name="📰 BORA Licitaciones", index=False)
        if not df_comprar.empty:
            df_comprar.to_excel(writer, sheet_name="🛒 Comprar", index=False)
        if not df_tgn.empty:
            df_tgn.to_excel(writer, sheet_name="💰 TGN", index=False)
    archivo2 = os.path.join(carpeta, f"flujo_licitaciones_{fecha_str}.xlsx")
    with pd.ExcelWriter(archivo2, engine="openpyxl") as writer:
        if not df_adj.empty:
            df_con_cuit = df_adj[df_adj["cuit_proveedor"].astype(bool)].copy()
            if not df_con_cuit.empty:
                df_con_cuit.to_excel(writer, sheet_name="✅ Adjudicados con CUIT", index=False)
        df_flujo = df_cruce_con_riesgo if not df_cruce_con_riesgo.empty else df_cruce
        if not df_flujo.empty:
            df_flujo.to_excel(writer, sheet_name="🔗 Flujo Cruzado", index=False)
        if not df_comprar.empty:
            df_comprar.to_excel(writer, sheet_name="⏳ Licitaciones Abiertas", index=False)
    return archivo1, archivo2

# ─── PÁGINAS ───────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    archivos = buscar_todos_los_xlsx(DATA_DIR)
    df = cargar_ultimo_reporte()
    total = len(df) if not df.empty else 0
    indice_prom = (round(df["indice_fenomeno_corruptivo"].mean(), 2)
                   if not df.empty and "indice_fenomeno_corruptivo" in df.columns else 0)
    alto_riesgo = (len(df[df["nivel_riesgo_teorico"] == "Alto"])
                   if not df.empty and "nivel_riesgo_teorico" in df.columns else 0)
    tipo_counts = (df["tipo_decision"].value_counts().to_dict()
                   if not df.empty and "tipo_decision" in df.columns else {})
    riesgo_counts = (df["nivel_riesgo_teorico"].value_counts().to_dict()
                     if not df.empty and "nivel_riesgo_teorico" in df.columns else {})
    tabla = []
    if not df.empty:
        cols = ["nro_proceso", "detalle", "tipo_decision", "indice_fenomeno_corruptivo", "nivel_riesgo_teorico"]
        tabla = df[[c for c in cols if c in df.columns]].head(50).fillna("n/a").to_dict(orient="records")
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "total": total, "indice_prom": indice_prom,
        "alto_riesgo": alto_riesgo, "total_reportes": len(archivos),
        "tipo_counts": tipo_counts, "riesgo_counts": riesgo_counts,
        "tabla": tabla, "sin_datos": df.empty,
        "ultimo_reporte": etiqueta_archivo(archivos[0]) if archivos else "Sin reportes — ejecute Análisis en Vivo",
    })

@app.get("/analisis-vivo", response_class=HTMLResponse)
async def analisis_vivo(request: Request):
    return templates.TemplateResponse("analisis.html", {"request": request})

@app.get("/documentacion", response_class=HTMLResponse)
async def documentacion(request: Request):
    from analisis import MATRIZ_TEORICA
    escenarios = [{"nombre": k, "transferencia": v["transferencia"], "peso": v["peso"]} for k, v in MATRIZ_TEORICA.items()]
    return templates.TemplateResponse("documentacion.html", {"request": request, "escenarios": escenarios})

@app.get("/licitaciones", response_class=HTMLResponse)
async def licitaciones(request: Request):
    return templates.TemplateResponse("licitaciones.html", {"request": request})

# ─── API STATUS ────────────────────────────────────────────────────

@app.get("/api/status")
def status():
    archivos = buscar_todos_los_xlsx(DATA_DIR)
    return {"status": "activo", "version": "1.0.0", "data_dir": DATA_DIR,
            "reportes_en_disco": len(archivos), "cache_activo": _df_cache is not None and not _df_cache.empty}

@app.get("/api/reportes")
def listar_reportes():
    archivos = buscar_todos_los_xlsx(DATA_DIR)
    return {"total": len(archivos), "reportes": [etiqueta_archivo(r) for r in archivos]}

@app.get("/api/dias-disponibles")
def dias_disponibles():
    inicio = datetime(2026, 2, 23).date()
    hoy    = datetime.now().date()
    dias_todos = []
    dia = inicio
    while dia <= hoy:
        dias_todos.append(dia.strftime("%Y-%m-%d"))
        dia += timedelta(days=1)
    dias_con_datos = []
    for d in dias_todos:
        carpeta = os.path.join(DATA_DIR, d[:7])
        if os.path.exists(os.path.join(carpeta, f"reporte_{d}.xlsx")) or \
           os.path.exists(os.path.join(carpeta, f"flujo_licitaciones_{d}.xlsx")):
            dias_con_datos.append(d)
    return {"dias_todos": dias_todos, "dias": dias_con_datos,
            "total_dias": len(dias_todos), "con_datos": len(dias_con_datos)}

# ─── API DATOS LICITACIONES POR FECHA ──────────────────────────────

@app.get("/api/licitaciones/datos")
def datos_licitaciones(fecha: str = None):
    """Lee los Excel del día indicado (o el más reciente) y devuelve todas las hojas como JSON."""
    if fecha:
        try:
            datetime.strptime(fecha, "%Y-%m-%d")
            fecha_str = fecha
        except ValueError:
            raise HTTPException(status_code=400, detail="Formato inválido. Use YYYY-MM-DD")
    else:
        archivos = buscar_todos_los_xlsx(DATA_DIR)
        if not archivos:
            return {"fecha": None, "flujo": [], "bora_licitaciones": [],
                    "bora_adjudicaciones": [], "comprar": [], "tgn": [], "sin_datos": True}
        fecha_str = os.path.basename(archivos[0]).replace("reporte_", "").replace(".xlsx", "")

    carpeta = os.path.join(DATA_DIR, fecha_str[:7])
    reporte = os.path.join(carpeta, f"reporte_{fecha_str}.xlsx")
    flujo   = os.path.join(carpeta, f"flujo_licitaciones_{fecha_str}.xlsx")

    if not os.path.exists(reporte) and not os.path.exists(flujo):
        return {"fecha": fecha_str, "flujo": [], "bora_licitaciones": [],
                "bora_adjudicaciones": [], "comprar": [], "tgn": [], "sin_datos": True}

    flujo_data   = leer_hoja(reporte, ["🚨 Flujo Completo", "🔗 Flujo Cruzado"]) if os.path.exists(reporte) else []
    bora_licit   = leer_hoja(reporte, ["📰 BORA Licitaciones"])                  if os.path.exists(reporte) else []
    bora_adj     = leer_hoja(reporte, ["🏆 Adjudicaciones"])                     if os.path.exists(reporte) else []
    comprar_data = leer_hoja(reporte, ["🛒 Comprar"])                            if os.path.exists(reporte) else []
    tgn_data     = leer_hoja(reporte, ["💰 TGN"])                                if os.path.exists(reporte) else []

    # Fallback al archivo de flujo si faltan hojas en el reporte
    if not comprar_data and os.path.exists(flujo):
        comprar_data = leer_hoja(flujo, ["⏳ Licitaciones Abiertas"])
    if not bora_adj and os.path.exists(flujo):
        bora_adj = leer_hoja(flujo, ["✅ Adjudicados con CUIT"])

    return {
        "fecha": fecha_str,
        "flujo": flujo_data,
        "bora_licitaciones": bora_licit,
        "bora_adjudicaciones": bora_adj,
        "comprar": comprar_data,
        "tgn": tgn_data,
        "sin_datos": False,
        "totales": {
            "flujo": len(flujo_data), "licit": len(bora_licit),
            "adj": len(bora_adj), "comprar": len(comprar_data), "tgn": len(tgn_data),
        }
    }

# ─── API MONITOR XAI ───────────────────────────────────────────────

@app.post("/api/analisis")
def ejecutar_analisis():
    try:
        import diario
        from analisis import analizar_boletin
        df_nuevo = diario.extraer_comprar()
        if df_nuevo is None or df_nuevo.empty:
            raise HTTPException(status_code=404, detail="No se pudieron obtener datos del portal.")
        df_res, path_excel, _ = analizar_boletin(df_nuevo)
        set_cache(df_res)
        return {"status": "ok", "reporte": os.path.basename(path_excel) if path_excel else "guardado_en_memoria",
                "total_procesos": len(df_res),
                "indice_promedio": round(df_res["indice_fenomeno_corruptivo"].mean(), 2) if not df_res.empty else 0}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/marco-teorico")
def marco_teorico():
    from analisis import MATRIZ_TEORICA
    return {"escenarios": [{"escenario": k, "transferencia": v.get("transferencia")} for k, v in MATRIZ_TEORICA.items()]}

@app.get("/api/descargar-articulo")
def descargar_articulo():
    from fastapi.responses import FileResponse
    ruta = "articulo_monteverde_español.docx"
    if not os.path.exists(ruta):
        raise HTTPException(status_code=404, detail="Artículo no disponible")
    return FileResponse(path=ruta, filename="articulo_monteverde_español.docx",
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

# ─── API LICITACIONES ──────────────────────────────────────────────

@app.post("/api/licitaciones/ejecutar")
def ejecutar_licitaciones(fecha: str = None):
    try:
        from diario import (extraer_bora_licitaciones, extraer_bora_adjudicaciones,
                            extraer_comprar, extraer_pagos_tgn, cruzar_fuentes)
        if fecha:
            try:
                datetime.strptime(fecha, "%Y-%m-%d")
                fecha_str = fecha
            except ValueError:
                raise HTTPException(status_code=400, detail="Formato de fecha inválido. Use YYYY-MM-DD")
        else:
            fecha_str = datetime.now().strftime("%Y-%m-%d")

        df_bora    = extraer_bora_licitaciones()
        df_adj     = extraer_bora_adjudicaciones(df_bora)
        df_licit   = (df_bora[df_bora["es_adjudicacion"] == False].copy().reset_index(drop=True)
                      if not df_bora.empty else pd.DataFrame())
        df_comprar = extraer_comprar()
        df_tgn     = extraer_pagos_tgn()
        df_cruce   = cruzar_fuentes(df_adj, df_comprar, df_tgn)
        archivo1, archivo2 = guardar_excels_con_fecha(df_cruce, df_adj, df_licit, df_comprar, df_tgn, fecha_str)

        con_cuit       = int(df_adj["cuit_proveedor"].astype(bool).sum()) if not df_adj.empty else 0
        flujo_completo = (int((df_cruce["alerta"] == "🚨 FLUJO COMPLETO: BORA→COMPRAR→TGN").sum())
                          if not df_cruce.empty else 0)
        riesgo_alto    = int((df_cruce["nivel_riesgo_licit"] == "Alto").sum())  if "nivel_riesgo_licit" in df_cruce.columns else 0
        riesgo_medio   = int((df_cruce["nivel_riesgo_licit"] == "Medio").sum()) if "nivel_riesgo_licit" in df_cruce.columns else 0

        return {
            "status": "ok", "fecha": fecha_str,
            "licitaciones_bora": len(df_licit), "adjudicaciones": len(df_adj),
            "adjudicaciones_con_cuit": con_cuit, "comprar": len(df_comprar),
            "tgn": len(df_tgn), "flujo_cruzado": len(df_cruce),
            "flujo_completo": flujo_completo, "riesgo_alto": riesgo_alto, "riesgo_medio": riesgo_medio,
            "archivo_reporte": os.path.basename(archivo1), "archivo_flujo": os.path.basename(archivo2),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))