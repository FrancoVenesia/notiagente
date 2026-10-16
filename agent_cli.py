"""Las mismas herramientas que mcp_server.py, pero por línea de comandos.

Las usa la rutina en la nube de Claude Code, que no puede cargar servidores MCP locales.

    python agent_cli.py reglas                         # reglas de agrupamiento y análisis
    python agent_cli.py preparar                       # ingesta + texto completo
    python agent_cli.py listar-clusters
    python agent_cli.py guardar-agrupamiento grupos.json    # {"stories": [StoryGroup, ...]}
    python agent_cli.py stories-pendientes [--maximo 6]
    python agent_cli.py leer-story 12
    python agent_cli.py guardar-analisis 12 analisis.json  # un objeto Analysis
    python agent_cli.py enviar-resumen [--no-enviar]
    python agent_cli.py esquema analisis|grupos        # JSON Schema esperado
"""

import argparse
import json
import sys
from pathlib import Path

import mcp_server as tools
from analyze import Analysis, Grouping

tools.MODEL_LABEL = "claude-cloud"   # identifica en la base los análisis hechos en la nube


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("reglas")
    sub.add_parser("preparar")
    sub.add_parser("listar-clusters")
    p = sub.add_parser("guardar-agrupamiento"); p.add_argument("archivo")
    p = sub.add_parser("stories-pendientes"); p.add_argument("--maximo", type=int, default=6)
    p = sub.add_parser("leer-story"); p.add_argument("story_id", type=int)
    p = sub.add_parser("guardar-analisis"); p.add_argument("story_id", type=int); p.add_argument("archivo")
    p = sub.add_parser("enviar-resumen"); p.add_argument("--no-enviar", action="store_true")
    p = sub.add_parser("esquema"); p.add_argument("tipo", choices=["analisis", "grupos"])
    args = parser.parse_args()

    if args.cmd == "reglas":
        print(tools.INSTRUCTIONS)
    elif args.cmd == "preparar":
        print(tools.preparar_datos())
    elif args.cmd == "listar-clusters":
        print(tools.listar_clusters())
    elif args.cmd == "guardar-agrupamiento":
        grouping = Grouping.model_validate_json(Path(args.archivo).read_text(encoding="utf-8"))
        print(tools.guardar_agrupamiento(grouping.stories))
    elif args.cmd == "stories-pendientes":
        print(tools.stories_pendientes(args.maximo))
    elif args.cmd == "leer-story":
        print(tools.leer_story(args.story_id))
    elif args.cmd == "guardar-analisis":
        analysis = Analysis.model_validate_json(Path(args.archivo).read_text(encoding="utf-8"))
        print(tools.guardar_analisis(args.story_id, analysis))
    elif args.cmd == "enviar-resumen":
        print(tools.enviar_resumen(not args.no_enviar))
    elif args.cmd == "esquema":
        model = Analysis if args.tipo == "analisis" else Grouping
        print(json.dumps(model.model_json_schema(), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
