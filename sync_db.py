"""Guarda y recupera news.db en la rama "datos" del repositorio, sin cambiar de rama.

    python sync_db.py pull   # trae news.db desde origin/datos (si existe)
    python sync_db.py push   # commitea news.db en datos y lo sube

Usa comandos internos de git (hash-object / mktree / commit-tree), así el código en main
no se toca y la base no ensucia el historial principal.
"""

import os
import subprocess
import sys
from datetime import datetime, timezone

from db import DB_PATH

BRANCH = "datos"
REF = f"refs/remotes/origin/{BRANCH}"


def git(*args: str, input: bytes | None = None, check: bool = True) -> str:
    result = subprocess.run(["git", *args], input=input, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout.decode(errors="replace").strip()


def remote_exists() -> bool:
    git("fetch", "origin", f"+refs/heads/{BRANCH}:{REF}", check=False)
    return bool(git("rev-parse", "--verify", "--quiet", REF, check=False))


def pull() -> None:
    if not remote_exists():
        print(f"No existe la rama {BRANCH}; se arranca con una base nueva.")
        return
    data = subprocess.run(["git", "show", f"{REF}:news.db"], capture_output=True, check=True).stdout
    DB_PATH.write_bytes(data)
    print(f"news.db recuperada de {BRANCH} ({len(data) / 1e6:.1f} MB).")


def push() -> None:
    if not DB_PATH.exists():
        sys.exit("No hay news.db para guardar.")
    blob = git("hash-object", "-w", str(DB_PATH))
    tree = git("mktree", input=f"100644 blob {blob}\tnews.db\n".encode())
    parent = ["-p", git("rev-parse", REF)] if remote_exists() else []
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "NotiAgente", "GIT_AUTHOR_EMAIL": "notiagente@users.noreply.github.com",
           "GIT_COMMITTER_NAME": "NotiAgente", "GIT_COMMITTER_EMAIL": "notiagente@users.noreply.github.com"}
    message = f"Datos {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC"
    commit = subprocess.run(["git", "commit-tree", tree, *parent, "-m", message], env=env,
                            capture_output=True, check=True).stdout.decode().strip()
    git("push", "origin", f"{commit}:refs/heads/{BRANCH}")
    print(f"news.db guardada en {BRANCH} ({commit[:8]}).")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("pull", "push"):
        sys.exit(__doc__)
    pull() if sys.argv[1] == "pull" else push()
