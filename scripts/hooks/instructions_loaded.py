"""instructions_loaded -- hook InstructionsLoaded: audit di QUALI file di istruzioni/memoria
Claude Code ha caricato, FUORI dal brain.

Nuovo in v3.1. Appende una riga `ts<TAB>session_id<TAB>brain_id<TAB>file_path<TAB>memory_type
<TAB>load_reason` a `<GUARD_HOME>/audit/<brain_id>.log`. MAI il contenuto del file.
GUARD_HOME (kb_config.guard_home): BRAIN_GUARD_HOME nell'ambiente > cartella dello script
`brain-guard.mjs` referenziato negli hook di `.claude/settings.json` (nodo, poi radice) >
default di piattaforma (%LOCALAPPDATA%/brain-guard, POSIX ~/.brain-guard).
Payload atteso su stdin (Claude Code): session_id, cwd, file_path, memory_type, load_reason.
Stdlib-only, silenzioso, exit 0 sempre.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kb_config as kc  # noqa: E402


def _clean(v) -> str:
    s = str(v if v is not None else "")
    return "".join(ch if ch.isprintable() else " " for ch in s).replace("\t", " ").strip()[:500]


def main() -> int:
    try:
        payload = {}
        if sys.stdin is not None and not sys.stdin.isatty():
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
        cwd = Path(str(payload.get("cwd") or os.getcwd()))
        root = kc.deployed_root() or kc.find_brain_root(cwd)
        node = None
        if root is not None:
            try:
                node = kc.current_node(root, start=cwd)
            except Exception:  # noqa: BLE001
                node = None
        bid = (kc.brain_id(root, strict=False) if root is not None else "") or "unknown"
        line = "\t".join([
            dt.datetime.now().isoformat(timespec="seconds"),
            _clean(payload.get("session_id")), bid,
            _clean(payload.get("file_path")), _clean(payload.get("memory_type")),
            _clean(payload.get("load_reason")),
        ])
        audit = kc.guard_home(root, node) / "audit"
        audit.mkdir(parents=True, exist_ok=True)
        with open(audit / f"{bid}.log", "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
