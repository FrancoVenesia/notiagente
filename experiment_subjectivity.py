"""Experimento: ¿qué tan cerca llega un clasificador clásico a las etiquetas del LLM?

Usa como "silver standard" las afirmaciones que Claude ya clasificó (tabla claims) y compara:
  1. Reglas léxicas en español (verbos de atribución, conectores causales, tiempos futuros, adjetivos
     valorativos). Sin entrenamiento.
  2. TF-IDF (palabras + n-gramas de caracteres) + regresión logística, con validación cruzada.

Evalúa dos tareas:
  - 5 clases: hecho / declaracion / interpretacion / opinion / prediccion
  - binaria:  objetivo (hecho, declaracion) vs. subjetivo (interpretacion, opinion, prediccion)

Uso:
    python experiment_subjectivity.py          # con las afirmaciones de news.db
    python experiment_subjectivity.py --demo   # con una muestra de ejemplo (para probar el código)

Ojo: las etiquetas del LLM no son verdad absoluta. Para conclusiones serias, etiquetá a mano
una muestra (p. ej. 200 afirmaciones) y medí también el acuerdo LLM-humano.
"""

import argparse
import re
import sys
import unicodedata
from collections import Counter

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.pipeline import FeatureUnion, make_pipeline

from db import connect

LABELS = ["hecho", "declaracion", "interpretacion", "opinion", "prediccion"]
OBJECTIVE = {"hecho", "declaracion"}


def strip_accents(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


# ---------- baseline 1: reglas ----------

ATTRIBUTION = re.compile(
    r"\b(dijo|afirmo|sostuvo|senalo|aseguro|advirtio|explico|indico|declaro|considero|"
    r"remarco|destaco|reconocio|admitio|planteo|opino|segun|sostiene|afirma|asegura|dice)\b"
    r"|[«“\"]")
PREDICTION = re.compile(
    r"\b(preve|proyecta|proyecto|espera|esperan|estima|anticipa|podria|podrian|deberia|"
    r"pronostica|se espera|perspectiva|proximo|proximos|proxima|el ano que viene|"
    r"20(2[7-9]|3\d))\b")
# Futuro simple: se busca con acentos ("pagará"), si no "para" o "ahora" darían falsos positivos
FUTURE = re.compile(r"\b\w+(rá|rán|remos)\b")
OPINION = re.compile(
    r"\b(preocupante|alarmante|positivo|negativo|historico|grave|excelente|buena|bueno|mala|"
    r"malo|exitoso|fracaso|elogio|critico|celebro|lamentable|acertado|equivocado|"
    r"fuerte|debil|impresionante|polemico|controvertido|optimismo|pesimismo)\b")
INTERPRETATION = re.compile(
    r"\b(debido a|a raiz de|como consecuencia|impulsad[oa]s?|explicad[oa]s?|"
    r"lo que refleja|refleja|en medio de|pese a|a pesar de|en un contexto|tras|"
    r"producto de|por efecto de|responde a|se explica)\b")


def rule_classify(text: str) -> str:
    t = strip_accents(text)
    if ATTRIBUTION.search(t):
        return "declaracion"  # "X dijo que pagará" es una declaración, no una predicción
    if PREDICTION.search(t) or FUTURE.search(text.lower()):
        return "prediccion"
    if OPINION.search(t):
        return "opinion"
    if INTERPRETATION.search(t):
        return "interpretacion"
    return "hecho"


# ---------- baseline 2: TF-IDF + regresión logística ----------

def tfidf_model():
    features = FeatureUnion([
        ("words", TfidfVectorizer(preprocessor=strip_accents, ngram_range=(1, 2), min_df=1)),
        ("chars", TfidfVectorizer(preprocessor=strip_accents, analyzer="char_wb",
                                  ngram_range=(3, 5), min_df=1)),
    ])
    return make_pipeline(features, LogisticRegression(max_iter=2000, class_weight="balanced"))


# ---------- datos ----------

DEMO = [  # Oraciones de ejemplo escritas a mano, solo para probar el código
    ("La inflación mayorista fue de 3,2% en septiembre", "hecho"),
    ("El INDEC publicó el índice de precios mayoristas", "hecho"),
    ("El riesgo país cerró en 629 puntos", "hecho"),
    ("El Banco Central compró 150 millones de dólares", "hecho"),
    ("El dólar blue cerró a $1.555", "hecho"),
    ("El superávit primario alcanzó $1,4 billones en septiembre", "hecho"),
    ("Los bonos cayeron por sexto día consecutivo", "hecho"),
    ("La canasta básica subió 2,3% en el mes", "hecho"),
    ("Caputo dijo que la Argentina pagará por adelantado al FMI", "declaracion"),
    ("Según el FMI, el país debe reducir los subsidios energéticos", "declaracion"),
    ("Georgieva afirmó que el programa está en marcha", "declaracion"),
    ("El ministro aseguró que no habrá devaluación", "declaracion"),
    ("Galperin sostuvo que algo fuerte está cambiando en el país", "declaracion"),
    ("El presidente señaló que el riesgo país bajará", "declaracion"),
    ("La suba fue impulsada por el aumento del petróleo", "interpretacion"),
    ("El alza se explica por la demanda de cobertura antes de las elecciones", "interpretacion"),
    ("Los bonos caen en medio de la volatilidad global", "interpretacion"),
    ("Pese al superávit, los mercados siguen desconfiando", "interpretacion"),
    ("La caída de la actividad refleja el ajuste del gasto", "interpretacion"),
    ("Tras el dato, las tasas reaccionaron al alza", "interpretacion"),
    ("Es un dato preocupante para la economía", "opinion"),
    ("El FMI elogió las reformas del gobierno", "opinion"),
    ("Fue una semana excelente para el Banco Central", "opinion"),
    ("La política cambiaria es un fracaso", "opinion"),
    ("Se trata de un logro histórico en materia fiscal", "opinion"),
    ("La medida es acertada pero tardía", "opinion"),
    ("El FMI prevé una recuperación en 2027", "prediccion"),
    ("La inflación podría bajar en los próximos meses", "prediccion"),
    ("Los analistas esperan una suba de tasas", "prediccion"),
    ("El PBI crecerá 4% el año que viene", "prediccion"),
    ("Se espera que el dólar se mantenga estable", "prediccion"),
    ("La consultora estima que el déficit comercial aumentará", "prediccion"),
]


def load_claims(model: str | None) -> tuple[list[str], list[str]]:
    conn = connect()
    if model:
        rows = conn.execute("SELECT text, type FROM claims WHERE model = ?", (model,)).fetchall()
    else:
        rows = conn.execute("SELECT text, type FROM claims").fetchall()
    conn.close()
    return [r["text"] for r in rows], [r["type"] for r in rows]


def report(name: str, y_true: list[str], y_pred: list[str]) -> None:
    print(f"\n### {name} — 5 clases")
    present = [l for l in LABELS if l in set(y_true) | set(y_pred)]
    print(classification_report(y_true, y_pred, labels=present, zero_division=0))
    print("Matriz de confusión (filas = LLM, columnas = predicción):")
    print("      " + " ".join(f"{l[:6]:>7}" for l in present))
    for label, row in zip(present, confusion_matrix(y_true, y_pred, labels=present)):
        print(f"{label[:6]:>6} " + " ".join(f"{v:>7}" for v in row))

    to_bin = lambda ys: ["objetivo" if y in OBJECTIVE else "subjetivo" for y in ys]
    print(f"\n### {name} — binaria")
    print(classification_report(to_bin(y_true), to_bin(y_pred), zero_division=0))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--model", help="usar solo las etiquetas de este modelo (p. ej. claude-haiku-5-5)")
    args = parser.parse_args()

    if args.demo:
        texts, labels = [t for t, _ in DEMO], [l for _, l in DEMO]
    else:
        texts, labels = load_claims(args.model)
    if len(texts) < 20:
        sys.exit(f"Solo hay {len(texts)} afirmaciones etiquetadas. Corré python analyze.py "
                 "unos días para juntar más, o probá con --demo.")

    print(f"{len(texts)} afirmaciones · distribución: {dict(Counter(labels))}")

    report("Reglas léxicas", labels, [rule_classify(t) for t in texts])

    min_class = min(Counter(labels).values())
    folds = max(2, min(5, min_class))
    cv = StratifiedKFold(n_splits=folds, shuffle=True, random_state=42)
    pred = cross_val_predict(tfidf_model(), np.array(texts, dtype=object), labels, cv=cv)
    report(f"TF-IDF + regresión logística ({folds}-fold CV)", labels, list(pred))


if __name__ == "__main__":
    main()
