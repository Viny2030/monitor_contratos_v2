from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd
import os

app = FastAPI(
    title="Monitor XAI - Ph.D. Monteverde",
    description="Algoritmos contra la Corrupción",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Ruta de datos compatible con Railway y local
DATA_DIR = "/app/data" if os.path.exists("/app") else "data"
os.makedirs(DATA_DIR, exist_ok=True)

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Cache en memoria para Railway (persiste mientras el contenedor esté vivo)
_df_cache = None


def buscar_todos_los_xlsx(base_dir):
    archivos = []
    for root, dirs, files in os.walk(base_dir):
        for f in files:
            if f.endswith(".xlsx") or f.endswith(".csv"):
                archivos.append(os.path.join(root, f))
    archivos.sort(key=lambda x: os.path.getmtime(x), reverse=True)
    return archivos


def etiqueta_archivo(ruta):
    partes = ruta.replace("\\", "/").split("/")
    if len(partes) >= 3:
        return f"{partes[-2]} / {partes[-1]}"
    return partes[-1]


def cargar_ultimo_reporte():
    global _df_cache

    # Prioridad 1: cache en memoria (resultado del último análisis ejecutado)
    if _df_cache is not None and not _df_cache.empty:
        return _df_cache

    # Prioridad 2: archivos en disco
    archivos = buscar_todos_los_xlsx(DATA_DIR)
    if not archivos:
        return pd.DataFrame()

    ruta = archivos[0]
    try:
        if ruta.endswith(".csv"):
            return pd.read_csv(ruta)
        xl = pd.ExcelFile(ruta)
        hoja = "Sheet1" if "Sheet1" in xl.sheet_names else xl.sheet_names[0]
        return xl.parse(hoja)
    except Exception as e:
        print(f"Error cargando reporte: {e}")
        return pd.DataFrame()


def set_cache(df):
    global _df_cache
    _df_cache = df


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    archivos = buscar_todos_los_xlsx(DATA_DIR)
    df = cargar_ultimo_reporte()

    total = len(df) if not df.empty else 0
    indice_prom = (
        round(df["indice_fenomeno_corruptivo"].mean(), 2)
        if not df.empty and "indice_fenomeno_corruptivo" in df.columns
        else 0
    )
    alto_riesgo = (
        len(df[df["nivel_riesgo_teorico"] == "Alto"])
        if not df.empty and "nivel_riesgo_teorico" in df.columns
        else 0
    )
    tipo_counts = (
        df["tipo_decision"].value_counts().to_dict()
        if not df.empty and "tipo_decision" in df.columns
        else {}
    )
    riesgo_counts = (
        df["nivel_riesgo_teorico"].value_counts().to_dict()
        if not df.empty and "nivel_riesgo_teorico" in df.columns
        else {}
    )

    tabla = []
    if not df.empty:
        cols = ["nro_proceso", "detalle", "tipo_decision", "indice_fenomeno_corruptivo", "nivel_riesgo_teorico"]
        cols_existentes = [c for c in cols if c in df.columns]
        tabla = df[cols_existentes].head(50).fillna("n/a").to_dict(orient="records")

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "total": total,
        "indice_prom": indice_prom,
        "alto_riesgo": alto_riesgo,
        "total_reportes": len(archivos),
        "tipo_counts": tipo_counts,
        "riesgo_counts": riesgo_counts,
        "tabla": tabla,
        "sin_datos": df.empty,
        "ultimo_reporte": (
            etiqueta_archivo(archivos[0])
            if archivos
            else "Sin reportes — ejecute Análisis en Vivo"
        ),
    })


@app.get("/analisis-vivo", response_class=HTMLResponse)
async def analisis_vivo(request: Request):
    return templates.TemplateResponse("analisis.html", {"request": request})


@app.get("/documentacion", response_class=HTMLResponse)
async def documentacion(request: Request):
    from analisis import MATRIZ_TEORICA
    escenarios = [
        {"nombre": k, "transferencia": v["transferencia"], "peso": v["peso"]}
        for k, v in MATRIZ_TEORICA.items()
    ]
    return templates.TemplateResponse("documentacion.html", {
        "request": request,
        "escenarios": escenarios,
    })


@app.get("/licitaciones", response_class=HTMLResponse)
async def licitaciones(request: Request):
    return templates.TemplateResponse("licitaciones.html", {"request": request})


@app.get("/api/status")
def status():
    archivos = buscar_todos_los_xlsx(DATA_DIR)
    return {
        "status": "activo",
        "version": "1.0.0",
        "data_dir": DATA_DIR,
        "reportes_en_disco": len(archivos),
        "cache_activo": _df_cache is not None and not _df_cache.empty,
    }


@app.get("/api/reportes")
def listar_reportes():
    archivos = buscar_todos_los_xlsx(DATA_DIR)
    return {
        "total": len(archivos),
        "reportes": [etiqueta_archivo(r) for r in archivos],
    }


@app.post("/api/analisis")
def ejecutar_analisis():
    try:
        import diario
        from analisis import analizar_boletin

        df_nuevo = diario.extraer_licitaciones()

        if df_nuevo is None or df_nuevo.empty:
            raise HTTPException(
                status_code=404,
                detail="No se pudieron obtener datos del portal. El sitio comprar.gob.ar puede no estar accesible desde este entorno.",
            )

        df_res, path_excel, _ = analizar_boletin(df_nuevo)

        # Guardar en cache para que el dashboard lo muestre en esta sesión
        set_cache(df_res)

        return {
            "status": "ok",
            "reporte": os.path.basename(path_excel) if path_excel else "guardado_en_memoria",
            "total_procesos": len(df_res),
            "indice_promedio": (
                round(df_res["indice_fenomeno_corruptivo"].mean(), 2)
                if not df_res.empty
                else 0
            ),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/marco-teorico")
def marco_teorico():
    from analisis import MATRIZ_TEORICA
    return {
        "escenarios": [
            {"escenario": k, "transferencia": v.get("transferencia")}
            for k, v in MATRIZ_TEORICA.items()
        ]
    }


@app.get("/api/descargar-articulo")
def descargar_articulo():
    from fastapi.responses import FileResponse
    ruta = "articulo_monteverde_español.docx"
    if not os.path.exists(ruta):
        raise HTTPException(status_code=404, detail="Artículo no disponible")
    return FileResponse(
        path=ruta,
        filename="articulo_monteverde_español.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
