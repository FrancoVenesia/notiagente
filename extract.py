"""Extrae el texto completo de los artículos recientes con trafilatura.

Uso:
    python extract.py              # artículos de las últimas 24 h sin texto
    python extract.py --hours 48
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests
import trafilatura

from db import connect

USER_AGENT = "Mozilla/5.0 (compatible; news-agent/0.1)"
MIN_CHARS = 300   # menos que esto suele ser un paywall o una página vacía


def fetch_text(url: str) -> tuple[str, str | None]:
    """Devuelve (estado, texto)."""
    # Los links de Google News redirigen vía JavaScript; no se pueden resolver con un GET.
    # Esas notas casi siempre tienen otra versión directa en el mismo cluster.
    if "news.google.com" in url:
        return "skip", None
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        resp.raise_for_status()
    except requests.RequestException:
        return "error", None
    text = trafilatura.extract(resp.text, include_comments=False, include_tables=False,
                               favor_precision=True)
    if not text or len(text) < MIN_CHARS:
        return "error", None
    return "ok", text


def extract(hours: int) -> None:
    conn = connect()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT id, url, source FROM articles WHERE published_at >= ? AND text_status IS NULL",
        (since,)).fetchall()
    print(f"Extrayendo texto de {len(rows)} artículos...")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda r: fetch_text(r["url"]), rows))

    stats: dict[str, int] = {}
    for row, (status, text) in zip(rows, results):
        conn.execute("UPDATE articles SET full_text = ?, text_status = ? WHERE id = ?",
                     (text, status, row["id"]))
        stats[status] = stats.get(status, 0) + 1
    conn.commit()
    conn.close()
    print("  " + " · ".join(f"{k}: {v}" for k, v in sorted(stats.items())))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=int, default=24)
    extract(parser.parse_args().hours)
