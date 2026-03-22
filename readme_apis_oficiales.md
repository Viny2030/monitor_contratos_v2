# APIs Oficiales — monitor_contratos_v2

Módulo adicional. **No modifica ningún archivo existente.**

## Archivos nuevos

| Archivo | Descripción |
|---|---|
| `apis_oficiales.py` | Módulo con todas las funciones de API |
| `test_apis_oficiales.py` | Script de verificación |

---

## APIs cubiertas

### 1. BORA — Normativa (argentina.gob.ar/normativa)
Alternativa más estable al scraping del HTML de boletinoficial.gob.ar.

```python
from apis_oficiales import obtener_bora_normativa_api

# Normas de hoy, sección tercera (licitaciones/adjudicaciones)
df = obtener_bora_normativa_api(seccion="tercera")

# Buscar por texto
df = obtener_bora_normativa_api(texto="licitacion", seccion="tercera")

# Rango de fechas
df = obtener_bora_normativa_api(fecha_desde="2026-03-01", fecha_hasta="2026-03-22")
```

---

### 2. COMPR.AR — API CKAN (datos.gob.ar)
Convocatorias y adjudicaciones estructuradas. Años disponibles: **2015–2020**.

```python
from apis_oficiales import obtener_comprar_api

# Adjudicaciones 2020
df = obtener_comprar_api(anio=2020, tipo="adjudicaciones")

# Convocatorias filtradas por organismo
df = obtener_comprar_api(anio=2019, tipo="convocatorias", organismo="SALUD")
```

**Limitación:** CKAN solo tiene hasta 2020. Para años recientes (2021+) seguí usando el scraping actual de `diario.py`.

---

### 3. CONTRAT.AR — Estándar OCDS (obra pública)
Datos de obra pública en formato abierto estructurado.

```python
from apis_oficiales import obtener_contrat_ocds_api

df = obtener_contrat_ocds_api()
df = obtener_contrat_ocds_api(organismo="VIALIDAD")
```

---

### 4. TGN — Endpoint /ejecucion (complemento al /credito)
El `/credito` ya está en `diario.py`. El `/ejecucion` agrega el campo **`cuit_beneficiario`** que mejora el cruce.

```python
from apis_oficiales import obtener_tgn_ejecucion_api

df = obtener_tgn_ejecucion_api()
df = obtener_tgn_ejecucion_api(anio=2026, jurisdiccion="MINISTERIO DE OBRAS PUBLICAS")
```

---

### 5. CUIT — Validación vía AFIP SOA público

```python
from apis_oficiales import validar_cuit_api, validar_cuits_lote

# CUIT individual
info = validar_cuit_api("30-50000427-3")
# → {"razon_social": "...", "domicilio_fiscal": "...", "estado_afip": "ACTIVO", ...}

# Lote de CUITs (de df_adjudicaciones existente)
import pandas as pd
df_adj = pd.read_excel("data/2026-03/reporte_2026-03-22.xlsx", sheet_name="🏆 Adjudicaciones")
cuits = df_adj["cuit_proveedor"].dropna().unique().tolist()
df_validados = validar_cuits_lote(cuits)
```

---

### 6. SIPRO — Proveedores del Estado (datos.gob.ar)

```python
from apis_oficiales import obtener_sipro_api

# Buscar por nombre
df = obtener_sipro_api(nombre="TECHINT")

# Buscar por CUIT
df = obtener_sipro_api(cuit="30-50000427-3")
```

---

## Función combinada

```python
from apis_oficiales import obtener_todo_api

# Corre todas las APIs y devuelve un dict de DataFrames
resultados = obtener_todo_api()

df_bora   = resultados["bora_normativa"]
df_adj    = resultados["comprar_adj"]
df_conv   = resultados["comprar_conv"]
df_obras  = resultados["contrat_ocds"]
df_pagos  = resultados["tgn_ejecucion"]
```

---

## Cómo agregar al flujo existente (sin modificar diario.py)

```python
# En tu script separado o en un nuevo archivo enriquecedor.py:
import pandas as pd
from apis_oficiales import obtener_todo_api, validar_cuits_lote

# 1. Leer el reporte que ya generó diario.py
df_adj = pd.read_excel("data/2026-03/reporte_2026-03-22.xlsx",
                       sheet_name="🏆 Adjudicaciones")

# 2. Validar los CUITs extraídos
cuits = df_adj["cuit_proveedor"].dropna().unique().tolist()
df_cuits = validar_cuits_lote(cuits)

# 3. Enriquecer el reporte
df_enriquecido = df_adj.merge(
    df_cuits[["cuit_original", "razon_social", "estado_afip"]],
    left_on="cuit_proveedor", right_on="cuit_original", how="left"
)

# 4. Guardar separado (sin pisar el original)
df_enriquecido.to_excel("data/2026-03/adjudicaciones_enriquecidas.xlsx", index=False)
```

---

## Test rápido

```bash
# Verificar todas las APIs
python test_apis_oficiales.py

# Solo una
python test_apis_oficiales.py --solo bora
python test_apis_oficiales.py --solo tgn
python test_apis_oficiales.py --solo cuit --cuit 30-50000427-3
python test_apis_oficiales.py --solo sipro --nombre "LICITACION"
python test_apis_oficiales.py --solo compat   # verifica compatibilidad con diario.py
```

---

## Variables de entorno (opcionales)

```bash
# .env o Railway Variables
TGN_TOKEN=707cb8c8-83e6-4c4d-a202-3e49c14eda89   # ya configurada en diario.py
```

---

## Estado de cada API

| Fuente | Tipo | Datos en tiempo real | Requiere token |
|---|---|---|---|
| BORA normativa | JSON endpoint | ✅ Sí (del día) | No |
| COMPR.AR CKAN | API CKAN | ⚠️ Histórico (hasta 2020) | No |
| CONTRAT.AR OCDS | API CKAN | ⚠️ Histórico | No |
| TGN /ejecucion | REST API | ✅ Año en curso | Sí (ya configurado) |
| CUIT AFIP SOA | REST público | ✅ Tiempo real | No |
| SIPRO | API CKAN + CSV | ⚠️ Actualización mensual | No |
