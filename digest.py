"""Arma el resumen del día y lo envía por Telegram.

Uso:
    python digest.py                 # muestra el resumen en consola (no envía)
    python digest.py --send          # lo envía al chat configurado en .env
    python digest.py --get-chat-id   # muestra tu chat_id (escribile antes algo al bot)
"""

import argparse
import html
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

from db import connect, load_env

load_env()
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
TELEGRAM_LIMIT = 4096
MAX_BULLETS = 3


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def bullets(items: list[dict], render) -> list[str]:
    lines = [f"• {render(i)}" for i in items[:MAX_BULLETS]]
    if len(items) > MAX_BULLETS:
        lines.append(f"  <i>(+{len(items) - MAX_BULLETS} más)</i>")
    return lines


def format_story(n: int, title: str, a: dict) -> str:
    lines = [f"<b>{n}. {esc(title)}</b>  <i>({a['total_medios']} medios)</i>",
             esc(a["resumen_neutral"])]

    if a["hechos_confirmados"]:
        lines += ["", "✅ <b>Confirmado por varios medios</b>"]
        lines += bullets(a["hechos_confirmados"],
                         lambda h: f"{esc(h['texto'])} <i>({esc(', '.join(h['medios']))})</i>")
    if a["discrepancias"]:
        lines += ["", "⚠️ <b>Discrepancias</b>"]
        lines += bullets(a["discrepancias"], lambda d: f"{esc(d['tema'])}: " + "; ".join(
            f"{esc(v['medio'])} → {esc(v['dice'])}" for v in d["versiones"]))
    if a["hechos_fuente_unica"]:
        lines += ["", "🔎 <b>Lo informa un solo medio</b>"]
        lines += bullets(a["hechos_fuente_unica"],
                         lambda h: f"{esc(h['texto'])} <i>({esc(', '.join(h['medios']))})</i>")
    if a["encuadres"]:
        lines += ["", "🗞 <b>Cómo lo cuenta cada medio</b>"]
        lines += [f'• <a href="{esc(e["url"])}">{esc(e["medio"])}</a>: {esc(e["enfasis"])} '
                  f'<i>— {esc(e["tono"])}</i>' for e in a["encuadres"]]
    return "\n".join(lines)


def build_digest(conn, hours: int, limit: int) -> tuple[list[str], list[int]]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    analyzed = conn.execute(
        """SELECT id, title, analysis FROM stories
           WHERE analysis IS NOT NULL AND sent_at IS NULL AND created_at >= ?
           ORDER BY importance DESC, json_extract(analysis, '$.total_medios') DESC
           LIMIT ?""", (since, limit)).fetchall()
    others = conn.execute(
        """SELECT title FROM stories
           WHERE analysis IS NULL AND sent_at IS NULL AND created_at >= ? AND importance >= 3
           ORDER BY importance DESC LIMIT 5""", (since,)).fetchall()

    today = datetime.now().strftime("%d/%m")
    messages = [f"📊 <b>Resumen económico — {today}</b>\n"
                f"{len(analyzed)} noticias analizadas, separando hechos de interpretaciones."]
    for n, s in enumerate(analyzed, 1):
        text = format_story(n, s["title"], json.loads(s["analysis"]))
        if len(text) > TELEGRAM_LIMIT:
            # Cada línea cierra sus propias etiquetas, así que cortar en un salto es seguro
            text = text[:text.rfind("\n", 0, TELEGRAM_LIMIT)]
        messages.append(text)
    if others:
        messages.append("📌 <b>Otros temas del día</b>\n" +
                        "\n".join(f"• {esc(o['title'])}" for o in others))
    return messages, [s["id"] for s in analyzed]


def telegram(method: str, **payload) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("Falta TELEGRAM_BOT_TOKEN en .env (ver .env.example).")
    resp = requests.post(TELEGRAM_API.format(token=token, method=method), json=payload, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data.get('description')}")
    return data["result"]


def send(messages: list[str]) -> None:
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        sys.exit("Falta TELEGRAM_CHAT_ID en .env. Obtenelo con: python digest.py --get-chat-id")
    for text in messages:
        telegram("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                 disable_web_page_preview=True)


def get_chat_id() -> None:
    updates = telegram("getUpdates")
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"] for u in updates if "message" in u}
    if not chats:
        print("No hay mensajes. Escribile algo a tu bot en Telegram y volvé a correr esto.")
    for chat_id, chat in chats.items():
        print(f"TELEGRAM_CHAT_ID={chat_id}   ({chat.get('first_name') or chat.get('title')})")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--get-chat-id", action="store_true")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--limit", type=int, default=6, help="máximo de noticias analizadas")
    args = parser.parse_args()

    if args.get_chat_id:
        get_chat_id()
        return

    conn = connect()
    messages, story_ids = build_digest(conn, args.hours, args.limit)
    if not story_ids:
        print("No hay noticias analizadas sin enviar. Corré antes: python analyze.py")
    elif args.send:
        send(messages)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.executemany("UPDATE stories SET sent_at = ? WHERE id = ?",
                         [(now, i) for i in story_ids])
        conn.commit()
        print(f"Enviado: {len(messages)} mensajes, {len(story_ids)} noticias.")
    else:
        print("\n\n".join(messages))
    conn.close()


if __name__ == "__main__":
    main()
