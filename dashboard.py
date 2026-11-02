"""Genera el panel HTML que muestra, paso a paso, cómo el agente analiza cada noticia.

    python dashboard.py                  # escribe panel.html con los datos de news.db
    python dashboard.py --salida otro.html

El HTML es autocontenido (datos incrustados, sin dependencias externas salvo fuentes).
Para ver datos nuevos: python sync_db.py pull && python dashboard.py
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

from db import BASE, connect
from digest import format_story

TEMPLATE = BASE / "panel_template.html"
CLAIM_TYPES = ["hecho", "declaracion", "interpretacion", "opinion", "prediccion"]
MIN_OUTLET_CLAIMS = 30   # medios con menos afirmaciones no entran al perfil comparativo


def source_name(source: str) -> str:
    return source.replace(" (vía Google News)", "").strip()


def tone_category(tone: str) -> str:
    """Agrupa el tono libre que escribe el modelo ("neutral, informativo", "alarmista"...)."""
    t = (tone or "").lower()
    if "alarm" in t:
        return "alarmista"
    if "crític" in t or "critic" in t:
        return "neutral" if "matic" in t else "crítico"
    if (any(k in t for k in ("favorable", "optimis", "elogi", "oficial", "tranquiliz"))
            or re.search(r"\bpositiv", t)):  # \b: que "expositivo" no cuente como positivo
        return "favorable"
    if any(k in t for k in ("caut", "prudent", "escéptic", "esceptic", "pesimis", "preocup")):
        return "cauteloso"
    if any(k in t for k in ("neutral", "informativ", "descriptiv", "técnic", "tecnic",
                            "explicativ", "analític", "analitic", "servicio")):
        return "neutral"
    return "otro"


def build_data(conn) -> dict:
    q = lambda sql, *p: conn.execute(sql, p).fetchall()

    articles = {r["id"]: r for r in q(
        "SELECT id, source, title, url, published_at, cluster_id, text_status FROM articles")}
    stories = q("""SELECT id, title, importance, created_at, analysis, analyzed_at, sent_at
                   FROM stories WHERE analysis IS NOT NULL ORDER BY created_at DESC, importance DESC""")
    story_clusters = defaultdict(list)
    for r in q("SELECT story_id, cluster_id FROM story_clusters"):
        story_clusters[r["story_id"]].append(r["cluster_id"])
    by_cluster = defaultdict(list)
    for a in articles.values():
        by_cluster[a["cluster_id"]].append(a)
    claims_by_story = defaultdict(list)
    for r in q("SELECT story_id, article_id, text, type, attributed_to FROM claims"):
        claims_by_story[r["story_id"]].append(r)

    out_stories, days = {}, defaultdict(list)
    outlet_types: dict[str, Counter] = defaultdict(Counter)
    outlet_tones: dict[str, Counter] = defaultdict(Counter)
    daily_types: dict[str, Counter] = defaultdict(Counter)

    for s in stories:
        analysis = json.loads(s["analysis"])
        day = s["created_at"][:10]
        clusters, story_articles = [], []
        for cid in story_clusters[s["id"]]:
            members = sorted(by_cluster[cid], key=lambda a: a["published_at"])
            clusters.append({"id": cid, "n": len(members),
                             "titles": list(dict.fromkeys(a["title"] for a in members))[:4]})
            story_articles += members
        claimed = {c["article_id"] for c in claims_by_story[s["id"]]}

        claims = []
        for c in claims_by_story[s["id"]]:
            medio = source_name(articles[c["article_id"]]["source"]) if c["article_id"] in articles else "?"
            claims.append({"a": c["article_id"], "m": medio, "t": c["text"], "k": c["type"],
                           "by": c["attributed_to"]})
            outlet_types[medio][c["type"]] += 1
            daily_types[day][c["type"]] += 1

        for e in analysis.get("encuadres", []):
            e["cat"] = tone_category(e.get("tono", ""))
            outlet_tones[e["medio"]][e["cat"]] += 1

        out_stories[s["id"]] = {
            "id": s["id"], "title": s["title"], "importance": s["importance"], "day": day,
            "sent": bool(s["sent_at"]), "model": analysis.get("modelo", ""),
            "analysis": analysis,
            "clusters": sorted(clusters, key=lambda c: -c["n"]),
            "articles": [{"id": a["id"], "m": source_name(a["source"]), "t": a["title"], "u": a["url"],
                          "gn": "vía Google News" in a["source"], "txt": a["text_status"] == "ok",
                          "used": a["id"] in claimed}
                         for a in sorted(story_articles, key=lambda a: (a["id"] not in claimed,
                                                                         a["text_status"] != "ok"))],
            "claims": claims,
            "telegram": format_story(1, s["title"], analysis),
        }
        days[day].append(s["id"])

    outlets = []
    for medio, counts in outlet_types.items():
        total = sum(counts.values())
        if total >= MIN_OUTLET_CLAIMS:
            outlets.append({"m": medio, "total": total, "types": {k: counts[k] for k in CLAIM_TYPES},
                            "tones": dict(outlet_tones[medio])})
    outlets.sort(key=lambda o: -(o["types"]["hecho"] + o["types"]["declaracion"]) / o["total"])

    n_articles = len(articles)
    funnel = {
        "articulos": n_articles,
        "medios": len({source_name(a["source"]) for a in articles.values()}),
        "medios_rss": len({a["source"] for a in articles.values() if "vía Google News" not in a["source"]}),
        "con_texto": sum(a["text_status"] == "ok" for a in articles.values()),
        "clusters": len({a["cluster_id"] for a in articles.values()}),
        "stories": q("SELECT COUNT(*) FROM stories")[0][0],
        "analizadas": len(stories),
        "afirmaciones": sum(len(v) for v in claims_by_story.values()),
        "enviadas": sum(bool(s["sent_at"]) for s in stories),
    }
    published = [a["published_at"] for a in articles.values()]
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "period": [min(published)[:10], max(published)[:10]] if published else ["", ""],
        "funnel": funnel,
        "types": CLAIM_TYPES,
        "days": [{"d": d, "ids": ids} for d, ids in sorted(days.items(), reverse=True)],
        "daily": [{"d": d, **{k: c[k] for k in CLAIM_TYPES}} for d, c in sorted(daily_types.items())],
        "outlets": outlets,
        "stories": out_stories,
    }


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--salida", default=str(BASE / "panel.html"))
    args = parser.parse_args()

    conn = connect()
    data = build_data(conn)
    conn.close()
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = re.sub(r"</(script)", r"<\\/\1", payload, flags=re.I)  # no cerrar el <script> por accidente
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", payload)
    with open(args.salida, "w", encoding="utf-8") as f:
        f.write(html)
    f = data["funnel"]
    print(f"Panel generado: {args.salida} ({len(html) / 1e6:.2f} MB) · "
          f"{f['analizadas']} noticias analizadas, {f['afirmaciones']} afirmaciones")


if __name__ == "__main__":
    main()
