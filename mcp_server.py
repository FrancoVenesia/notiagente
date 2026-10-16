"""Servidor MCP del agente de noticias.

Expone la base de noticias a Claude (app de escritorio / Claude Code) para que el análisis
lo haga Claude con tu plan, sin usar la API paga. El flujo típico:

    preparar_datos → listar_clusters → guardar_agrupamiento → stories_pendientes
    → (por cada story) leer_story → guardar_analisis → enviar_resumen

Registro en Claude Code:
    claude mcp add noticias -- python <ruta>/mcp_server.py
"""

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from mcp.server.mcpserver import MCPServer

from analyze import (ANALYSIS_SYSTEM, GROUPING_SYSTEM, Analysis, StoryGroup, apply_grouping,
                     build_analysis_prompt, grouping_input, pending_stories, save_analysis,
                     story_material)
from db import BASE, connect

MODEL_LABEL = "claude-app"   # así quedan identificados estos análisis en la base
HOURS = 24

INSTRUCTIONS = f"""Herramientas para analizar noticias económicas de varios medios.

Flujo diario:
1. preparar_datos: descarga RSS y texto completo.
2. listar_clusters y después guardar_agrupamiento: uní los clusters que son la misma noticia.
3. stories_pendientes: las noticias a analizar, de mayor a menor importancia.
4. Para cada una: leer_story y después guardar_analisis.
5. enviar_resumen: manda el resumen por Telegram.

Reglas de agrupamiento:
{GROUPING_SYSTEM}

Reglas de análisis:
{ANALYSIS_SYSTEM}

Sé breve en tus mensajes entre herramientas: el resultado vive en la base, no en el chat."""

server = MCPServer("noticias", instructions=INSTRUCTIONS)


def _since() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=HOURS)).isoformat(timespec="seconds")


def _run(script: str, *args: str) -> str:
    result = subprocess.run([sys.executable, str(BASE / script), *args], cwd=BASE,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(f"{script} falló:\n{output[-2000:]}")
    return output


@server.tool()
def preparar_datos() -> str:
    """Descarga las noticias nuevas de los RSS y extrae el texto completo de los artículos."""
    ingest = _run("ingest.py").splitlines()
    extract = _run("extract.py").splitlines()
    return "\n".join(ingest[-1:] + extract[-1:])


@server.tool()
def listar_clusters() -> str:
    """Lista las stories existentes y los clusters de las últimas 24 h que todavía no pertenecen
    a ninguna story, ordenados por cantidad de medios. Usalo para decidir el agrupamiento."""
    with connect() as conn:
        text, clusters, _ = grouping_input(conn, _since())
    return text if clusters else "No hay clusters nuevos para agrupar."


@server.tool()
def guardar_agrupamiento(stories: list[StoryGroup]) -> str:
    """Guarda el agrupamiento: cada story une uno o más cluster_ids que cubren el mismo hecho.
    Incluí solo noticias relevantes de economía; podés omitir clusters irrelevantes."""
    conn = connect()
    try:
        _, clusters, existing = grouping_input(conn, _since())
        created, assigned = apply_grouping(conn, stories, clusters, existing)
    finally:
        conn.close()
    return f"{created} stories nuevas, {assigned} clusters asignados."


@server.tool()
def stories_pendientes(maximo: int = 6) -> str:
    """Stories de importancia 3 o más que todavía no tienen análisis, de mayor a menor relevancia."""
    conn = connect()
    try:
        rows = pending_stories(conn, _since(), maximo, MODEL_LABEL)
    finally:
        conn.close()
    if not rows:
        return "No hay stories pendientes de análisis."
    return "\n".join(f"- story {r['id']} [importancia {r['importance']}, {r['fuentes']} medios]: "
                     f"{r['title']}" for r in rows)


@server.tool()
def leer_story(story_id: int) -> str:
    """Devuelve los artículos completos de una story, numerados [A1], [A2]..., más los titulares
    de otros medios. Los números [AN] son los que hay que usar en guardar_analisis."""
    conn = connect()
    try:
        articles, headlines = story_material(conn, story_id)
    finally:
        conn.close()
    if not articles:
        return "Esta story no tiene artículos con texto completo; salteala."
    return build_analysis_prompt(articles, headlines)


@server.tool()
def guardar_analisis(story_id: int, analisis: Analysis) -> str:
    """Guarda el análisis de una story: afirmaciones clasificadas por artículo, hechos confirmados
    (2+ artículos), hechos de fuente única, discrepancias, encuadre de cada artículo y patrones
    de los titulares. Usá los mismos números [AN] que devolvió leer_story."""
    conn = connect()
    try:
        articles, _ = story_material(conn, story_id)
        if not articles:
            return "La story no tiene artículos; no se guardó nada."
        total = conn.execute(
            """SELECT COUNT(DISTINCT a.source) FROM articles a
               JOIN story_clusters sc ON sc.cluster_id = a.cluster_id
               WHERE sc.story_id = ?""", (story_id,)).fetchone()[0]
        invalid = {c.articulo for c in analisis.afirmaciones} - set(range(1, len(articles) + 1))
        save_analysis(conn, story_id, total, articles, analisis, MODEL_LABEL)
    finally:
        conn.close()
    warning = f" Aviso: se ignoraron afirmaciones con artículos inexistentes {sorted(invalid)}." if invalid else ""
    saved = sum(c.articulo not in invalid for c in analisis.afirmaciones)
    return f"Análisis guardado ({saved} afirmaciones).{warning}"


@server.tool()
def enviar_resumen(enviar: bool = True) -> str:
    """Arma el resumen con las stories analizadas. Con enviar=True lo manda por Telegram;
    con enviar=False solo devuelve el texto para revisarlo."""
    if enviar:
        missing = [v for v in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.environ.get(v)]
        if missing:
            return (f"No se envió: falta {', '.join(missing)} en .env. "
                    "Usá enviar=false para ver el resumen.")
    return _run("digest.py", *(["--send"] if enviar else []))


if __name__ == "__main__":
    server.run()
