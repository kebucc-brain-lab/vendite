"""session_start -- hook SessionStart: data (da orologio), nodo e brain_id in UNA riga.

Forkato da `scripts/hook_today.py` di VMR Brain (HEAD 0efa9254). Cosa cambia:
  - oltre alla data stampa nodo (`node_path`) e `brain_id`, risalendo dalla cwd della
    sessione (payload JSON su stdin di Claude Code) al `nodo.md` piu' vicino;
  - se `CLAUDE_ENV_FILE` e' definita vi scrive `BRAIN_NODE=<node_path>` (persistente per
    la sessione: i comandi del motore lo leggono come fallback);
  - se nella catena non c'e' nessun nodo.md/brain.yaml stampa un AVVISO (sessione fuori
    da un brain): e' l'unico caso in cui si aggiunge una seconda riga.

I nomi italiani dei giorni sono CABLATI (il locale di Windows risponde nella lingua della
UI, in shell non interattiva LC_TIME spesso manca): output identico su ogni macchina.
Stdlib-only, mai bloccante (exit 0 sempre).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kb_config as kc  # noqa: E402

GIORNI = ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica")


def _payload() -> dict:
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001
        return {}


def main() -> int:
    kc.utf8_stdout()
    now = datetime.now()
    data = f"Data: {now:%Y-%m-%d} ({GIORNI[now.weekday()]}) ore {now:%H:%M} -- giorno dall'orologio di sistema, mai a memoria"
    payload = _payload()
    cwd = Path(str(payload.get("cwd") or os.getcwd()))
    node = None
    root = None
    try:
        root = kc.deployed_root() or kc.find_brain_root(cwd)
        if root is not None:
            node = kc.current_node(root, start=cwd)
    except Exception:  # noqa: BLE001
        node = None
    if node is None or root is None:
        print(data)
        print(f"AVVISO: nessun nodo.md/brain.yaml risalendo da {cwd}: sessione fuori da un brain "
              f"(apri la sessione nella radice del brain o del nodo).")
        return 0
    bid = kc.brain_id(root, strict=False) or "BRAIN_ID assente (nodo.md/brain.yaml/.env)"
    print(f"{data} | brain: {bid} | nodo: {node.path}")
    env_file = os.environ.get("CLAUDE_ENV_FILE", "").strip()
    if env_file:
        try:
            with open(env_file, "a", encoding="utf-8", newline="\n") as f:
                f.write(f"BRAIN_NODE={node.path}\n")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 -- un hook di avvio non deve mai bloccare la sessione
        sys.exit(0)
