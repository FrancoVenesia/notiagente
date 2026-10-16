"""Pipeline completo: ingesta → texto completo → análisis → resumen por Telegram.

Uso:
    python run.py            # todo, y envía el resumen
    python run.py --no-send  # todo, pero muestra el resumen en consola
"""

import subprocess
import sys

STEPS = [
    ["ingest.py"],
    ["extract.py"],
    ["analyze.py"],
]


def main() -> None:
    sys.stderr.reconfigure(encoding="utf-8")
    send = "--no-send" not in sys.argv
    for step in STEPS + [["digest.py"] + (["--send"] if send else [])]:
        print(f"\n=== {' '.join(step)} ===", flush=True)
        result = subprocess.run([sys.executable, *step])
        if result.returncode != 0:
            sys.exit(f"Falló {step[0]} (código {result.returncode})")


if __name__ == "__main__":
    main()
