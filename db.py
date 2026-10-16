"""Base de datos compartida (SQLite) y carga de configuración desde .env."""

import os
import sqlite3
from pathlib import Path

BASE = Path(__file__).parent
DB_PATH = BASE / "news.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id            INTEGER PRIMARY KEY,
    url           TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    summary       TEXT,
    source        TEXT NOT NULL,
    topic         TEXT NOT NULL,
    published_at  TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    cluster_id    INTEGER,
    full_text     TEXT,
    text_status   TEXT            -- NULL = pendiente, 'ok', 'error', 'skip'
);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at);
CREATE INDEX IF NOT EXISTS idx_articles_cluster ON articles(cluster_id);

-- Una "story" une varios clusters que el LLM reconoce como la misma noticia.
CREATE TABLE IF NOT EXISTS stories (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    importance  INTEGER NOT NULL,   -- 1 (menor) a 5 (muy relevante)
    created_at  TEXT NOT NULL,
    analysis    TEXT,               -- JSON con hechos, discrepancias y encuadres
    analyzed_at TEXT,
    sent_at     TEXT
);
CREATE TABLE IF NOT EXISTS story_clusters (
    story_id    INTEGER NOT NULL REFERENCES stories(id),
    cluster_id  INTEGER NOT NULL,
    PRIMARY KEY (story_id, cluster_id)
);

-- Cada afirmación extraída de un artículo, clasificada por tipo.
CREATE TABLE IF NOT EXISTS claims (
    id            INTEGER PRIMARY KEY,
    story_id      INTEGER NOT NULL REFERENCES stories(id),
    article_id    INTEGER NOT NULL REFERENCES articles(id),
    text          TEXT NOT NULL,
    type          TEXT NOT NULL,    -- hecho | declaracion | interpretacion | opinion | prediccion
    attributed_to TEXT,
    model         TEXT
);
CREATE INDEX IF NOT EXISTS idx_claims_story ON claims(story_id);

-- Historial de análisis por modelo, para comparar (p. ej. Haiku vs. Opus sobre la misma story).
-- stories.analysis guarda el último, que es el que usa el resumen.
CREATE TABLE IF NOT EXISTS analyses (
    story_id      INTEGER NOT NULL REFERENCES stories(id),
    model         TEXT NOT NULL,
    analysis      TEXT NOT NULL,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (story_id, model)
);
"""

# Columnas agregadas después de la Fase 1, para bases ya creadas
MIGRATIONS = [
    "ALTER TABLE articles ADD COLUMN full_text TEXT",
    "ALTER TABLE articles ADD COLUMN text_status TEXT",
    "ALTER TABLE claims ADD COLUMN model TEXT",
]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # la columna ya existe o la tabla todavía no
    conn.executescript(SCHEMA)
    return conn


def load_env(path: Path = BASE / ".env") -> None:
    """Carga variables KEY=valor de .env sin pisar las que ya existen en el entorno."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
