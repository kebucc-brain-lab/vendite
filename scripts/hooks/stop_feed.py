"""stop_feed -- hook Stop: stampa `[brain-feed] N modifiche ...` SOLO se c'e' lavoro
operativo non ancora compilato nel vault (feed_check.summary_line). Tace altrimenti.

Nuovo in v3.1 (in VMR l'hook chiamava feed_check.py direttamente): risolve il nodo dalla
cwd del payload JSON della sessione, cosi' in un albero ogni nodo osserva le SUE ops_dirs e
la SUA memoria. Exit 0 sempre, stdlib-only, nessuna scrittura.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kb_config as kc  # noqa: E402


def main() -> int:
    kc.utf8_stdout()
    try:
        payload = {}
        if sys.stdin is not None and not sys.stdin.isatty():
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
        # Claude Code puo' richiamare Stop a cascata: `stop_hook_active` evita loop.
        if payload.get("stop_hook_active"):
            return 0
        cwd = Path(str(payload.get("cwd") or os.getcwd()))
        root = kc.deployed_root() or kc.find_brain_root(cwd)
        if root is None:
            return 0
        node = kc.current_node(root, start=cwd)
        import feed_check  # noqa: PLC0415
        line = feed_check.summary_line(root, node)
        if line:
            print(line)
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
