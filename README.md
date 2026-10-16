# Agente de noticias económicas

Junta noticias de varios medios, separa **hechos verificables** de **interpretaciones**, compara cómo encuadra cada medio la misma noticia y te manda un resumen por Telegram.

```
RSS ──► ingest.py ──► extract.py ──► analyze.py ──────────────► digest.py ──► Telegram
        (dedup)       (texto         1. une clusters en stories   (formato
                       completo)     2. afirmaciones + hechos      HTML)
                                        vs. encuadres (Claude)
```

## Modo sin costo: Claude vía MCP (recomendado)

El análisis lo hace Claude desde la app con tu plan, sin la API paga. Para eso, `mcp_server.py` expone la base como herramientas:

| Herramienta | Qué hace |
|---|---|
| `preparar_datos` | Corre `ingest.py` y `extract.py` |
| `listar_clusters` / `guardar_agrupamiento` | Claude une los clusters que cubren la misma noticia |
| `stories_pendientes` | Noticias a analizar, de mayor a menor importancia |
| `leer_story` / `guardar_analisis` | Artículos numerados [AN] → afirmaciones, hechos, discrepancias, encuadres |
| `enviar_resumen` | Corre `digest.py` (`--send` si `enviar=true`) |

- **Registro:** está en `.mcp.json` a nivel de proyecto. `.claude/settings.json` lo habilita y permite sus herramientas sin pedir confirmación.
- **Tarea programada:** "Resumen económico diario" en la sección *Scheduled* de la app. Corre todos los días a las 8:00 si la app está abierta; si está cerrada, corre al abrirla.
- **Consumo:** se estima en ~1% del límite semanal del plan Pro por corrida, con 6 noticias.
- Los análisis quedan guardados con `model = 'claude-app'`.

## Modo API (opcional, pago)

## Puesta en marcha

```bash
pip install -r requirements.txt
cp .env.example .env        # completá ANTHROPIC_API_KEY y los datos de Telegram
python run.py --no-send     # corre todo y muestra el resumen en consola
python run.py               # corre todo y lo envía por Telegram
```

**Telegram:**
1. Hablale a [@BotFather](https://t.me/BotFather), usá `/newbot` y copiá el token en `TELEGRAM_BOT_TOKEN`.
2. Escribile cualquier cosa a tu bot.
3. Corré `python digest.py --get-chat-id` y copiá el número en `TELEGRAM_CHAT_ID`.

## Scripts

| Script | Qué hace |
|---|---|
| `ingest.py` | Lee los RSS de `feeds.json`, deduplica por URL y por títulos parecidos (`cluster_id`). `--show 24` lista lo agrupado. |
| `extract.py` | Extrae el texto completo con trafilatura. Los links de Google News se omiten porque redirigen con JavaScript. |
| `analyze.py` | **Paso 1:** Claude une clusters que son la misma noticia ("stories") y les asigna importancia de 1 a 5. **Paso 2:** para las más importantes, extrae afirmaciones de cada artículo y las clasifica como hecho, declaración, interpretación, opinión o predicción. Después cruza fuentes (hechos confirmados, discrepancias, datos de un solo medio) y describe el encuadre de cada medio. Opciones: `--dry-run` (no llama a la API), `--simple-grouping` (agrupa sin LLM), `--top N`. |
| `digest.py` | Arma el resumen. `--send` lo envía por Telegram y marca las noticias como enviadas. |
| `run.py` | Corre todo en orden. |
| `experiment_subjectivity.py` | Experimento: reglas léxicas y TF-IDF + regresión logística contra las etiquetas del LLM. `--demo` usa una muestra de prueba. |

## Base de datos (`news.db`)

- `articles`: cada nota, con `cluster_id` (casi duplicados) y `full_text`.
- `stories` + `story_clusters`: noticias unificadas por el LLM, con el análisis en JSON.
- `claims`: cada afirmación, con su tipo y a quién se atribuye. Es el dataset para el experimento.

## Costo y modelo

`NEWS_MODEL` en `.env` define el modelo; por defecto es `claude-opus-5-5`, el más preciso. Cada story analizada envía entre 5k y 30k caracteres. Si querés gastar menos, probá `claude-sonnet-5-5` (mitad de precio) o `claude-haiku-5-5` (mucho más barato) y compará la calidad. El script muestra los tokens usados en cada llamada.

**Comparar modelos sobre las mismas noticias:**
```bash
python run.py --no-send                                    # primera pasada con NEWS_MODEL (Haiku)
python analyze.py --compare --model claude-opus-5-5 --top 5  # re-analiza esas mismas stories con Opus
```
Cada análisis queda guardado en la tabla `analyses` (story, modelo, JSON y tokens), y en `claims` cada afirmación tiene su `model`. El resumen sigue usando el análisis original. Con `python experiment_subjectivity.py --model claude-opus-5-5` corrés el experimento con las etiquetas de un solo modelo.

Con Opus y Sonnet se activa el *fallback* del lado del servidor: si el modelo rechaza una solicitud, la API la reintenta automáticamente con otro modelo.

## Programarlo todos los días (Windows)

```bash
schtasks /create /tn "NoticiasEconomia" /tc daily /st 08:00 /tr "python C:\ruta\al\proyecto\run.py"
```

Para tener más datos conviene correr `ingest.py` cada hora y `run.py` una vez a la mañana.
