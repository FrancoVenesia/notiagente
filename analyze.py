"""Fase 2: análisis con Claude.

1. Une clusters que son la misma noticia aunque los títulos difieran ("stories").
2. Para las stories más importantes, extrae afirmaciones de cada artículo, las clasifica
   (hecho / declaración / interpretación / opinión / predicción), cruza fuentes y
   describe el encuadre de cada medio.

Uso:
    python analyze.py                # agrupar + analizar (últimas 24 h)
    python analyze.py --dry-run      # muestra qué se enviaría, sin llamar a la API
    python analyze.py --top 5        # analizar solo las 5 stories más importantes
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from db import connect, load_env

load_env()
MODEL = os.environ.get("NEWS_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("NEWS_EFFORT", "medium")
# Modelos que aceptan el fallback del lado del servidor ante un rechazo
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}

MAX_CLUSTERS = 80          # clusters candidatos que se mandan para agrupar
MAX_ARTICLES = 6           # artículos completos por story (uno por medio)
MAX_ARTICLE_CHARS = 12000  # recorte por artículo; se avisa en el prompt
MIN_IMPORTANCE = 3

ClaimType = Literal["hecho", "declaracion", "interpretacion", "opinion", "prediccion"]


# ---------- esquemas de salida ----------

class StoryGroup(BaseModel):
    existing_story_id: int | None = Field(
        description="id de una story ya existente si estos clusters la continúan; si no, null")
    title: str = Field(description="título neutral y descriptivo de la noticia")
    importance: int = Field(description="1 = menor/rutinaria, 5 = muy relevante para la economía")
    cluster_ids: list[int]


class Grouping(BaseModel):
    stories: list[StoryGroup]


class Claim(BaseModel):
    articulo: int = Field(description="número N del artículo [AN] de donde sale")
    texto: str = Field(description="la afirmación, parafraseada en una oración")
    tipo: ClaimType
    atribuida_a: str | None = Field(description="quién la dice, si es una declaración o cita")


class CrossFact(BaseModel):
    texto: str
    articulos: list[int]


class Version(BaseModel):
    articulo: int
    dice: str


class Discrepancy(BaseModel):
    tema: str
    versiones: list[Version]


class Framing(BaseModel):
    articulo: int
    enfasis: str = Field(description="qué destaca primero o pone en el título")
    tono: str = Field(description="p. ej. neutral, favorable al gobierno, crítico, alarmista")
    omite: str = Field(description="datos relevantes que otros incluyen y este no; vacío si nada")


class Analysis(BaseModel):
    resumen_neutral: str = Field(description="2-3 oraciones solo con hechos confirmados")
    afirmaciones: list[Claim]
    hechos_confirmados: list[CrossFact] = Field(description="hechos presentes en 2+ artículos")
    hechos_fuente_unica: list[CrossFact] = Field(description="hechos relevantes de un solo artículo")
    discrepancias: list[Discrepancy]
    encuadres: list[Framing]
    encuadre_titulares: str = Field(
        description="patrones en cómo titulan los distintos medios (incluye la lista de titulares)")


GROUPING_SYSTEM = """Sos un editor de noticias económicas. Recibís clusters de artículos
(cada uno ya agrupa notas casi idénticas) y tenés que unir los que cubren el MISMO hecho
noticioso aunque lo titulen distinto (por ejemplo, distintas notas sobre un mismo informe del FMI).

Reglas:
- Un cluster puede estar en una sola story. Podés dejar afuera clusters irrelevantes.
- No unas noticias distintas solo porque comparten tema (dos datos de inflación distintos
  son stories distintas).
- Si unos clusters continúan una story existente, usá su existing_story_id.
- importance: 5 = dato o decisión de alto impacto (inflación, tasas, FMI, dólar con movimientos
  fuertes); 1 = rutinario (calendarios de pago, cotización diaria sin novedad)."""

ANALYSIS_SYSTEM = """Sos un analista de medios. Recibís varias notas sobre la misma noticia,
cada una marcada [AN], y una lista de titulares de otros medios.

Tu trabajo es separar lo verificable de lo interpretativo:
- hecho: dato verificable (cifras, fechas, decisiones tomadas, eventos ocurridos).
- declaracion: algo que alguien dijo. El hecho es que lo dijo, no que sea cierto.
- interpretacion: relación causal, contexto o lectura que hace el medio ("impulsada por…").
- opinion: valoración o juicio, incluidos adjetivos cargados ("elogió", "preocupante").
- prediccion: proyecciones o expectativas sobre el futuro.

Reglas:
- Extraé como máximo 10 afirmaciones por artículo, las más relevantes.
- hechos_confirmados solo con hechos presentes en 2 o más artículos.
- Si dos artículos dan cifras o versiones distintas de lo mismo, va en discrepancias.
- Un encuadre por artículo [AN]. Describí, no juzgues al medio.
- No agregues información que no esté en los textos.
- Si un texto dice [recortado], no infieras lo que falta."""


# ---------- llamadas a la API ----------

def call_claude(client: anthropic.Anthropic, system: str, user: str, schema: type[BaseModel]):
    kwargs = dict(
        model=MODEL,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=schema,
        output_config={"effort": EFFORT},
    )
    if MODEL in FALLBACK_MODELS:
        response = client.beta.messages.parse(
            **kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    else:
        response = client.messages.parse(**kwargs)

    if response.stop_reason == "refusal":
        raise RuntimeError(f"el modelo rechazó la solicitud: {response.stop_details}")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("la respuesta se cortó por max_tokens")
    u = response.usage
    print(f"    tokens: {u.input_tokens} entrada / {u.output_tokens} salida")
    return response.parsed_output, u


# ---------- paso 1: agrupar clusters en stories ----------

def candidate_clusters(conn, since: str) -> list[dict]:
    rows = conn.execute(
        """SELECT a.cluster_id,
                  COUNT(DISTINCT a.source) AS fuentes,
                  MAX(a.published_at)      AS ultima
           FROM articles a
           WHERE a.published_at >= ?
             AND a.cluster_id NOT IN (SELECT cluster_id FROM story_clusters)
           GROUP BY a.cluster_id
           ORDER BY fuentes DESC, ultima DESC
           LIMIT ?""", (since, MAX_CLUSTERS)).fetchall()
    clusters = []
    for r in rows:
        titles = [t[0] for t in conn.execute(
            "SELECT DISTINCT title FROM articles WHERE cluster_id = ? LIMIT 3", (r["cluster_id"],))]
        clusters.append({"cluster_id": r["cluster_id"], "fuentes": r["fuentes"], "titulos": titles})
    return clusters


def grouping_input(conn, since: str) -> tuple[str, list[dict], list[dict]]:
    """Texto con stories existentes y clusters sin asignar, para que un LLM los agrupe."""
    clusters = candidate_clusters(conn, since)
    existing = [dict(r) for r in conn.execute(
        "SELECT id, title FROM stories WHERE created_at >= ?", (since,))]

    lines = ["Stories existentes:"] + [f"- id {s['id']}: {s['title']}" for s in existing]
    if not existing:
        lines.append("(ninguna)")
    lines.append("\nClusters nuevos:")
    for c in clusters:
        lines.append(f"- cluster {c['cluster_id']} ({c['fuentes']} medios): " + " | ".join(c["titulos"]))
    return "\n".join(lines), clusters, existing


def apply_grouping(conn, groups: list[StoryGroup], clusters: list[dict],
                   existing: list[dict]) -> tuple[int, int]:
    """Guarda las stories propuestas. Devuelve (stories nuevas, clusters asignados)."""
    valid_ids = {c["cluster_id"] for c in clusters}
    existing_ids = {s["id"] for s in existing}
    used: set[int] = set()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    created = 0
    for story in groups:
        ids = [i for i in story.cluster_ids if i in valid_ids and i not in used]
        if not ids:
            continue
        used.update(ids)
        if story.existing_story_id in existing_ids:
            story_id = story.existing_story_id
            # Hay artículos nuevos: el análisis anterior queda desactualizado
            conn.execute("UPDATE stories SET analysis = NULL WHERE id = ?", (story_id,))
        else:
            story_id = conn.execute(
                "INSERT INTO stories (title, importance, created_at) VALUES (?, ?, ?)",
                (story.title, max(1, min(5, story.importance)), now)).lastrowid
            created += 1
        conn.executemany("INSERT OR IGNORE INTO story_clusters VALUES (?, ?)",
                         [(story_id, i) for i in ids])
    conn.commit()
    return created, len(used)


def group_stories(client, conn, since: str, dry_run: bool) -> None:
    prompt, clusters, existing = grouping_input(conn, since)
    if not clusters:
        print("No hay clusters nuevos para agrupar.")
        return
    print(f"Agrupando {len(clusters)} clusters ({len(prompt):,} caracteres)...")
    if dry_run:
        print(prompt[:3000] + ("\n[...]" if len(prompt) > 3000 else ""))
        return

    result, _ = call_claude(client, GROUPING_SYSTEM, prompt, Grouping)
    created, assigned = apply_grouping(conn, result.stories, clusters, existing)
    print(f"  {created} stories nuevas, {assigned} clusters asignados")


def group_simple(conn, since: str, dry_run: bool) -> None:
    """Sin LLM: una story por cluster, con importancia según cuántos medios lo cubren."""
    clusters = candidate_clusters(conn, since)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for c in clusters:
        importance = 5 if c["fuentes"] >= 8 else 4 if c["fuentes"] >= 4 else 3 if c["fuentes"] >= 2 else 1
        story_id = conn.execute(
            "INSERT INTO stories (title, importance, created_at) VALUES (?, ?, ?)",
            (c["titulos"][0], importance, now)).lastrowid
        conn.execute("INSERT INTO story_clusters VALUES (?, ?)", (story_id, c["cluster_id"]))
    if not dry_run:
        conn.commit()
    print(f"Agrupamiento simple: {len(clusters)} stories (una por cluster)")


# ---------- paso 2: hechos vs. interpretaciones ----------

def story_material(conn, story_id: int) -> tuple[list[dict], list[str]]:
    """Artículos completos (uno por medio) y titulares del resto de los medios."""
    rows = conn.execute(
        """SELECT a.* FROM articles a
           JOIN story_clusters sc ON sc.cluster_id = a.cluster_id
           WHERE sc.story_id = ?
           ORDER BY a.text_status = 'ok' DESC, LENGTH(a.full_text) DESC, a.id""",
        (story_id,)).fetchall()  # orden estable: los números [AN] tienen que repetirse
    articles, seen_sources, headlines = [], set(), []
    for r in rows:
        source = r["source"].replace(" (vía Google News)", "")
        if r["text_status"] == "ok" and source not in seen_sources and len(articles) < MAX_ARTICLES:
            seen_sources.add(source)
            articles.append(dict(r) | {"medio": source})
        elif source not in seen_sources:
            headlines.append(f"{source}: {r['title']}")
    return articles, list(dict.fromkeys(headlines))


def build_analysis_prompt(articles: list[dict], headlines: list[str]) -> str:
    parts = []
    for i, a in enumerate(articles, 1):
        text = a["full_text"]
        if len(text) > MAX_ARTICLE_CHARS:
            text = text[:MAX_ARTICLE_CHARS] + "\n[recortado]"
        parts.append(f"[A{i}] {a['medio']} — {a['title']}\n{text}")
    parts.append("Titulares de otros medios:\n" + ("\n".join(f"- {h}" for h in headlines) or "(ninguno)"))
    return "\n\n---\n\n".join(parts)


def pending_stories(conn, since: str, top: int, model: str, compare: bool = False) -> list:
    # Normal: stories sin analizar. compare: stories ya analizadas por otro modelo pero no por este.
    pending = ("s.analysis IS NOT NULL AND NOT EXISTS "
               "(SELECT 1 FROM analyses an WHERE an.story_id = s.id AND an.model = :model)"
               if compare else "s.analysis IS NULL")
    return conn.execute(
        f"""SELECT s.id, s.title, s.importance,
                  (SELECT COUNT(DISTINCT a.source) FROM articles a
                   JOIN story_clusters sc ON sc.cluster_id = a.cluster_id
                   WHERE sc.story_id = s.id) AS fuentes
           FROM stories s
           WHERE {pending} AND s.created_at >= :since AND s.importance >= :min_imp
           ORDER BY s.importance DESC, fuentes DESC
           LIMIT :top""",
        {"model": model, "since": since, "min_imp": MIN_IMPORTANCE, "top": top}).fetchall()


def save_analysis(conn, story_id: int, total_medios: int, articles: list[dict], result: Analysis,
                  model: str, tokens: tuple[int, int] | None = None, compare: bool = False) -> None:
    """Guarda afirmaciones y análisis. Con compare=True no pisa el análisis que usa el resumen."""
    by_index = {i: a for i, a in enumerate(articles, 1)}
    conn.execute("DELETE FROM claims WHERE story_id = ? AND model = ?", (story_id, model))
    conn.executemany(
        """INSERT INTO claims (story_id, article_id, text, type, attributed_to, model)
           VALUES (?,?,?,?,?,?)""",
        [(story_id, by_index[c.articulo]["id"], c.texto, c.tipo, c.atribuida_a, model)
         for c in result.afirmaciones if c.articulo in by_index])

    # Guardamos el análisis con nombres de medios en vez de números de artículo
    def medios(idx: list[int]) -> list[str]:
        return [by_index[i]["medio"] for i in idx if i in by_index]

    analysis = {
        "resumen_neutral": result.resumen_neutral,
        "hechos_confirmados": [{"texto": h.texto, "medios": medios(h.articulos)}
                               for h in result.hechos_confirmados],
        "hechos_fuente_unica": [{"texto": h.texto, "medios": medios(h.articulos)}
                                for h in result.hechos_fuente_unica],
        "discrepancias": [{"tema": d.tema,
                           "versiones": [{"medio": (medios([v.articulo]) or ["?"])[0], "dice": v.dice}
                                         for v in d.versiones]}
                          for d in result.discrepancias],
        "encuadres": [{"medio": by_index[e.articulo]["medio"], "url": by_index[e.articulo]["url"],
                       "enfasis": e.enfasis, "tono": e.tono, "omite": e.omite}
                      for e in result.encuadres if e.articulo in by_index],
        "encuadre_titulares": result.encuadre_titulares,
        "total_medios": total_medios,
        "modelo": model,
    }
    payload = json.dumps(analysis, ensure_ascii=False)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tokens_in, tokens_out = tokens or (None, None)
    conn.execute("INSERT OR REPLACE INTO analyses VALUES (?, ?, ?, ?, ?, ?)",
                 (story_id, model, payload, tokens_in, tokens_out, now))
    if not compare:
        conn.execute("UPDATE stories SET analysis = ?, analyzed_at = ? WHERE id = ?",
                     (payload, now, story_id))
    conn.commit()


def analyze_stories(client, conn, since: str, top: int, dry_run: bool, compare: bool) -> None:
    stories = pending_stories(conn, since, top, MODEL, compare)
    print(f"\nAnalizando {len(stories)} stories{' (comparación)' if compare else ''}...")

    for s in stories:
        articles, headlines = story_material(conn, s["id"])
        print(f"  • [{s['importance']}] {s['title']} — {len(articles)} artículos, "
              f"{len(headlines)} titulares")
        if not articles:
            print("    (sin texto completo disponible, se omite)")
            continue
        prompt = build_analysis_prompt(articles, headlines)
        if dry_run:
            print(f"    prompt: {len(prompt):,} caracteres")
            continue

        try:
            result, usage = call_claude(client, ANALYSIS_SYSTEM, prompt, Analysis)
        except (RuntimeError, anthropic.APIStatusError) as e:
            print(f"    ✗ {e}")
            continue

        save_analysis(conn, s["id"], s["fuentes"], articles, result, MODEL,
                      (usage.input_tokens, usage.output_tokens), compare)
        print(f"    ✓ {len(result.afirmaciones)} afirmaciones, "
              f"{len(result.hechos_confirmados)} hechos confirmados, "
              f"{len(result.discrepancias)} discrepancias")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--top", type=int, default=10, help="máximo de stories a analizar")
    parser.add_argument("--dry-run", action="store_true", help="no llama a la API")
    parser.add_argument("--simple-grouping", action="store_true",
                        help="agrupar sin LLM (una story por cluster)")
    parser.add_argument("--model", help="pisa NEWS_MODEL de .env (p. ej. claude-opus-5-5)")
    parser.add_argument("--compare", action="store_true",
                        help="re-analizar con --model las stories que ya analizó otro modelo, "
                             "sin agrupar de nuevo ni cambiar el resumen")
    args = parser.parse_args()

    global MODEL
    if args.model:
        MODEL = args.model

    since = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).isoformat(timespec="seconds")
    if not args.dry_run and not (os.environ.get("ANTHROPIC_API_KEY")
                                 or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("Falta ANTHROPIC_API_KEY: copiá .env.example a .env y completala "
                 "(o probá sin API con --dry-run).")
    client = None if args.dry_run else anthropic.Anthropic()
    print(f"Modelo: {MODEL} · effort: {EFFORT}")
    conn = connect()
    try:
        if args.compare:
            pass  # se comparan las mismas stories, sin re-agrupar
        elif args.simple_grouping:
            group_simple(conn, since, args.dry_run)
        else:
            group_stories(client, conn, since, args.dry_run)
        analyze_stories(client, conn, since, args.top, args.dry_run, args.compare)
    except anthropic.AuthenticationError:
        sys.exit("Falta una API key válida: definí ANTHROPIC_API_KEY en .env (ver .env.example).")
    finally:
        if args.dry_run:
            conn.rollback()  # el dry-run no deja nada escrito
        conn.close()


if __name__ == "__main__":
    main()
