"""Fase 1: ingesta de noticias desde RSS, deduplicación y guardado en SQLite.

Uso:
    python ingest.py                 # descarga todos los feeds y guarda lo nuevo
    python ingest.py --show 24       # muestra las noticias agrupadas de las últimas 24 h
"""

import argparse
import calendar
import html
import json
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests

from db import BASE, connect

FEEDS_PATH = BASE / "feeds.json"

USER_AGENT = "Mozilla/5.0 (compatible; news-agent/0.1)"
MAX_AGE = timedelta(days=3)          # ignora artículos más viejos
DEDUP_WINDOW = timedelta(hours=72)   # ventana para buscar duplicados
JACCARD_MIN = 0.5
CONTAINMENT_MIN = 0.5   # palabras compartidas / palabras del título más corto
CONTAINMENT_SHARED = 4  # ...siempre que compartan al menos esta cantidad
RATIO_MIN = 0.8

STOPWORDS = set("""
a al ante bajo con contra de del desde durante e el en entre hacia hasta la las le les lo los
mas más mediante o para pero por que qué se sin sobre su sus tras un una unos unas y ya es son
fue como cómo este esta estos estas ese esa eso hay muy tras cual cuál cuando donde ha han
""".split())

TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|ref$|outputType$|mc_)")

# ---------- normalización ----------

def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAMS.match(k)]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"),
                       urlencode(query), ""))


def clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def normalize_title(title: str) -> str:
    text = unicodedata.normalize("NFKD", title.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"(\d)[,.](\d)", r"\1.\2", text)  # "3,2%" y "3.2%" -> "3.2%"
    return re.sub(r"[^a-z0-9%. ]+|(?<!\d)\.|\.(?!\d)", " ", text)


def title_tokens(title: str) -> set[str]:
    return {w for w in normalize_title(title).split() if len(w) > 2 and w not in STOPWORDS}


def is_similar(a: str, b: str) -> bool:
    ta, tb = title_tokens(a), title_tokens(b)
    if ta and tb:
        shared = len(ta & tb)
        if shared / len(ta | tb) >= JACCARD_MIN:
            return True
        if shared >= CONTAINMENT_SHARED and shared / min(len(ta), len(tb)) >= CONTAINMENT_MIN:
            return True
    return SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio() >= RATIO_MIN


# ---------- descarga ----------

def fetch_feed(feed: dict, topic: str) -> list[dict]:
    try:
        resp = requests.get(feed["url"], headers={"User-Agent": USER_AGENT}, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  ✗ {feed['name']}: {e}", file=sys.stderr)
        return []

    parsed = feedparser.parse(resp.content)
    now = datetime.now(timezone.utc)
    items = []
    for entry in parsed.entries:
        link, title = entry.get("link"), clean_html(entry.get("title", ""))
        if not link or not title:
            continue

        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        published = (datetime.fromtimestamp(calendar.timegm(stamp), timezone.utc)
                     if stamp else now)
        if now - published > MAX_AGE:
            continue

        source = feed["name"]
        # Google News agrega " - Medio" al título y trae el medio real en entry.source
        if "news.google.com" in feed["url"]:
            real = entry.get("source", {}).get("title")
            if real:
                source = f"{real} (vía Google News)"
                title = re.sub(rf"\s+-\s+{re.escape(real)}$", "", title)

        items.append({
            "url": canonical_url(link),
            "title": title,
            "summary": clean_html(entry.get("summary", ""))[:1000],
            "source": source,
            "topic": topic,
            "published_at": published.isoformat(timespec="seconds"),
        })
    print(f"  ✓ {feed['name']}: {len(items)} artículos recientes")
    return items


# ---------- base de datos ----------

def ingest() -> None:
    feeds = json.loads(FEEDS_PATH.read_text(encoding="utf-8"))
    jobs = [(f, topic) for topic, lst in feeds.items() for f in lst]

    print(f"Descargando {len(jobs)} feeds...")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = pool.map(lambda job: fetch_feed(*job), jobs)
    items = sorted((i for batch in results for i in batch), key=lambda i: i["published_at"])

    conn = connect()
    since = (datetime.now(timezone.utc) - DEDUP_WINDOW).isoformat(timespec="seconds")
    recent = [dict(r) for r in conn.execute(
        "SELECT id, title, cluster_id FROM articles WHERE published_at >= ?", (since,))]

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new, dupes_url, grouped = 0, 0, 0
    for item in items:
        cur = conn.execute(
            """INSERT OR IGNORE INTO articles
               (url, title, summary, source, topic, published_at, fetched_at)
               VALUES (:url, :title, :summary, :source, :topic, :published_at, :fetched_at)""",
            {**item, "fetched_at": fetched_at})
        if cur.rowcount == 0:
            dupes_url += 1
            continue

        article_id = cur.lastrowid
        match = next((r for r in recent if is_similar(item["title"], r["title"])), None)
        cluster_id = match["cluster_id"] if match else article_id
        if match:
            grouped += 1
        conn.execute("UPDATE articles SET cluster_id = ? WHERE id = ?", (cluster_id, article_id))
        recent.append({"id": article_id, "title": item["title"], "cluster_id": cluster_id})
        new += 1

    conn.commit()
    conn.close()
    print(f"\nNuevos: {new} (de ellos {grouped} agrupados con una noticia ya existente) · "
          f"ya guardados antes: {dupes_url}")


def show(hours: int) -> None:
    conn = connect()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT cluster_id,
                  COUNT(*)                         AS n,
                  COUNT(DISTINCT source)           AS fuentes,
                  MAX(published_at)                AS ultima,
                  GROUP_CONCAT(DISTINCT source)    AS medios,
                  (SELECT title FROM articles a2 WHERE a2.cluster_id = a.cluster_id
                   ORDER BY a2.url LIKE '%news.google.com%', a2.id LIMIT 1) AS titulo,
                  (SELECT url   FROM articles a2 WHERE a2.cluster_id = a.cluster_id
                   ORDER BY a2.url LIKE '%news.google.com%', a2.id LIMIT 1) AS url
           FROM articles a
           WHERE published_at >= ?
           GROUP BY cluster_id
           ORDER BY fuentes DESC, ultima DESC""", (since,)).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM articles WHERE published_at >= ?",
                         (since,)).fetchone()[0]
    conn.close()

    print(f"{total} artículos → {len(rows)} noticias únicas en las últimas {hours} h\n")
    for r in rows[:30]:
        print(f"[{r['fuentes']} fuente(s)] {r['titulo']}")
        print(f"    {r['medios']}")
        print(f"    {r['url']}\n")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--show", type=int, metavar="HORAS",
                        help="mostrar noticias agrupadas de las últimas N horas")
    args = parser.parse_args()
    show(args.show) if args.show else ingest()
