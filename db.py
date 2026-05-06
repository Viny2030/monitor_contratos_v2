# -*- coding: utf-8 -*-
"""
db.py — Módulo de base de datos PostgreSQL
Monitor de Fenómenos Corruptivos — Ph.D. Monteverde
"""
import os
import logging
import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

logger = logging.getLogger(__name__)

# ── Conexión ─────────────────────────────────────────────────────────────────

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:IeNhJzpwXifkYtWorZPgZAURnUKycBJw@interchange.proxy.rlwy.net:36645/railway"
)

def get_conn():
    url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url, cursor_factory=RealDictCursor)


# ── Inicialización de tablas ─────────────────────────────────────────────────

def init_db():
    """Crea todas las tablas necesarias si no existen."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS contratos (
                    id                      SERIAL PRIMARY KEY,
                    fecha                   DATE,
                    fecha_extraccion        DATE,
                    organismo_contratante   TEXT,
                    tipo_proceso_bora       TEXT,
                    proveedor_adjudicado    TEXT,
                    cuit_proveedor          TEXT,
                    monto_adjudicado_bora   TEXT,
                    en_comprar              TEXT,
                    procesos_comprar        INTEGER DEFAULT 0,
                    unidad_comprar          TEXT,
                    nro_proceso_comprar     TEXT,
                    cobro_en_tgn            TEXT,
                    beneficiario_tgn        TEXT,
                    monto_cobrado_tgn       TEXT,
                    etapa                   TEXT,
                    alerta                  TEXT,
                    link_bora               TEXT,
                    indicadores_riesgo      TEXT,
                    score_riesgo_licit      NUMERIC,
                    indice_riesgo_licit     NUMERIC,
                    nivel_riesgo_licit      TEXT,
                    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS licitaciones (
                    id                  SERIAL PRIMARY KEY,
                    fecha_extraccion    DATE,
                    fecha_publicacion   DATE,
                    organismo           TEXT,
                    tipo_proceso        TEXT,
                    categoria           TEXT,
                    aviso_id            TEXT,
                    es_adjudicacion     BOOLEAN,
                    link                TEXT,
                    fuente              TEXT,
                    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS donaciones_consultas (
                    id          SERIAL PRIMARY KEY,
                    nombre      VARCHAR(100),
                    apellido    VARCHAR(100),
                    email       VARCHAR(254),
                    pais        VARCHAR(30),
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS estadisticas_acceso (
                    id          SERIAL PRIMARY KEY,
                    seccion     VARCHAR(100) NOT NULL,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_contratos_fecha
                    ON contratos (fecha DESC);
                CREATE INDEX IF NOT EXISTS idx_contratos_cuit
                    ON contratos (cuit_proveedor);
                CREATE INDEX IF NOT EXISTS idx_contratos_organismo
                    ON contratos (organismo_contratante);
                CREATE INDEX IF NOT EXISTS idx_contratos_riesgo
                    ON contratos (nivel_riesgo_licit);
                CREATE INDEX IF NOT EXISTS idx_ea_ts
                    ON estadisticas_acceso (created_at);
            """)
            conn.commit()
        conn.close()
        logger.info("✅ DB inicializada correctamente")
        print("✅ DB inicializada correctamente")
    except Exception as e:
        logger.error(f"❌ Error inicializando DB: {e}")
        print(f"❌ Error inicializando DB: {e}")


# ── Guardar contratos ─────────────────────────────────────────────────────────

def guardar_contratos(df: pd.DataFrame) -> int:
    """
    Inserta los contratos del día en PostgreSQL.
    Evita duplicados por fecha + link_bora.
    Retorna cantidad de filas insertadas.
    """
    if df is None or df.empty:
        print("⚠️  Sin contratos para guardar en DB")
        return 0

    def val(row, col):
        v = row.get(col, "")
        if pd.isna(v):
            return None
        return str(v).strip() or None

    def num(row, col):
        v = row.get(col, None)
        try:
            return float(v) if v and str(v).strip() else None
        except Exception:
            return None

    rows = []
    for _, row in df.iterrows():
        rows.append((
            val(row, "fecha"),
            val(row, "fecha_extraccion") or val(row, "fecha"),
            val(row, "organismo_contratante"),
            val(row, "tipo_proceso_bora"),
            val(row, "proveedor_adjudicado"),
            val(row, "cuit_proveedor"),
            val(row, "monto_adjudicado_bora"),
            val(row, "en_comprar"),
            int(row.get("procesos_comprar", 0) or 0),
            val(row, "unidad_comprar"),
            val(row, "nro_proceso_comprar"),
            val(row, "cobro_en_tgn"),
            val(row, "beneficiario_tgn"),
            val(row, "monto_cobrado_tgn"),
            val(row, "etapa"),
            val(row, "alerta"),
            val(row, "link_bora"),
            val(row, "indicadores_riesgo"),
            num(row, "score_riesgo_licit"),
            num(row, "indice_riesgo_licit"),
            val(row, "nivel_riesgo_licit"),
        ))

    sql = """
        INSERT INTO contratos (
            fecha, fecha_extraccion, organismo_contratante, tipo_proceso_bora,
            proveedor_adjudicado, cuit_proveedor, monto_adjudicado_bora,
            en_comprar, procesos_comprar, unidad_comprar, nro_proceso_comprar,
            cobro_en_tgn, beneficiario_tgn, monto_cobrado_tgn,
            etapa, alerta, link_bora,
            indicadores_riesgo, score_riesgo_licit, indice_riesgo_licit, nivel_riesgo_licit
        )
        VALUES %s
        ON CONFLICT DO NOTHING
    """

    try:
        conn = get_conn()
        with conn.cursor() as cur:
            execute_values(cur, sql, rows)
            insertados = cur.rowcount
        conn.commit()
        conn.close()
        print(f"✅ {insertados} contratos guardados en PostgreSQL")
        return insertados
    except Exception as e:
        print(f"❌ Error guardando contratos en DB: {e}")
        logger.error(f"Error guardando contratos: {e}")
        return 0


# ── Leer contratos ────────────────────────────────────────────────────────────

def leer_contratos(dias: int = 90) -> pd.DataFrame:
    """
    Lee los contratos de los últimos N días desde PostgreSQL.
    Retorna DataFrame listo para usar en el dashboard.
    """
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT *
                FROM contratos
                WHERE fecha >= CURRENT_DATE - INTERVAL '%s days'
                ORDER BY fecha DESC, id DESC
            """, (dias,))
            rows = cur.fetchall()
        conn.close()

        if not rows:
            print(f"⚠️  Sin contratos en los últimos {dias} días")
            return pd.DataFrame()

        df = pd.DataFrame([dict(r) for r in rows])
        print(f"✅ {len(df)} contratos leídos desde PostgreSQL")
        return df

    except Exception as e:
        print(f"❌ Error leyendo contratos de DB: {e}")
        logger.error(f"Error leyendo contratos: {e}")
        return pd.DataFrame()


def leer_ultimo_reporte() -> pd.DataFrame:
    """Lee solo los contratos del último día disponible."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT *
                FROM contratos
                WHERE fecha = (SELECT MAX(fecha) FROM contratos)
                ORDER BY id DESC
            """)
            rows = cur.fetchall()
        conn.close()

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame([dict(r) for r in rows])
        print(f"✅ Último reporte: {len(df)} contratos del {df['fecha'].iloc[0]}")
        return df

    except Exception as e:
        print(f"❌ Error leyendo último reporte: {e}")
        return pd.DataFrame()


def get_stats() -> dict:
    """KPIs para el endpoint /resumen y estadísticas del monitor."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:

            cur.execute("SELECT COUNT(*) AS total FROM contratos")
            total = cur.fetchone()["total"]

            cur.execute("""
                SELECT COUNT(*) AS total FROM contratos
                WHERE fecha = (SELECT MAX(fecha) FROM contratos)
            """)
            hoy = cur.fetchone()["total"]

            cur.execute("""
                SELECT COUNT(*) AS total FROM contratos
                WHERE nivel_riesgo_licit = 'Alto'
            """)
            alto_riesgo = cur.fetchone()["total"]

            cur.execute("""
                SELECT MAX(fecha) AS ultima FROM contratos
            """)
            ultima_fecha = cur.fetchone()["ultima"]

            cur.execute("""
                SELECT COUNT(*) AS total FROM contratos
                WHERE alerta LIKE '%FLUJO COMPLETO%'
            """)
            flujos_completos = cur.fetchone()["total"]

            # Estadísticas de acceso
            cur.execute("SELECT COUNT(*) AS total FROM estadisticas_acceso")
            visitas = cur.fetchone()["total"]

            cur.execute("""
                SELECT seccion, COUNT(*) AS v
                FROM estadisticas_acceso
                GROUP BY seccion ORDER BY v DESC
            """)
            por_seccion = [dict(r) for r in cur.fetchall()]

        conn.close()

        return {
            "total_contratos": total,
            "contratos_hoy": hoy,
            "alto_riesgo": alto_riesgo,
            "flujos_completos": flujos_completos,
            "ultima_fecha": str(ultima_fecha) if ultima_fecha else None,
            "visitas_totales": visitas,
            "visitas_por_seccion": por_seccion,
        }

    except Exception as e:
        logger.error(f"Error get_stats: {e}")
        return {"error": str(e)}


def registrar_visita(seccion: str):
    """Registra una visita a una sección."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO estadisticas_acceso (seccion) VALUES (%s)", (seccion,)
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"tracking error: {e}")


def registrar_donacion(nombre: str, apellido: str, email: str, pais: str):
    """Registra una consulta de donación."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO donaciones_consultas
                   (nombre, apellido, email, pais) VALUES (%s,%s,%s,%s) RETURNING id""",
                (nombre.strip(), apellido.strip(), email.strip(), pais)
            )
            row = cur.fetchone()
        conn.commit()
        conn.close()
        return row["id"]
    except Exception as e:
        logger.warning(f"donacion registro error: {e}")
        return None


# ── Test directo ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("🔧 Inicializando base de datos...")
    init_db()
    print("\n📊 Stats actuales:")
    stats = get_stats()
    for k, v in stats.items():
        if k != "visitas_por_seccion":
            print(f"  {k}: {v}")