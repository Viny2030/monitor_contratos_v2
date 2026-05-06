from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime

app = FastAPI(title="Monitor XAI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

@app.get("/")
def home():
    return {"status": "ok", "mensaje": "Monitor XAI activo", "timestamp": datetime.now().isoformat()}

@app.get("/api/status")
def status():
    return {"status": "activo", "version": "2.0", "timestamp": datetime.now().isoformat()}

@app.get("/resumen")
def resumen():
    return {"total": 0, "mensaje": "Sin datos aún"}
