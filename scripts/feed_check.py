"""feed_check -- sentinella generica dell'autoalimentazione del brain.

Forkato da `scripts/feed_check.py` di VMR Brain (HEAD 0efa9254). Cosa cambia:
  - niente path cablati: la memoria viene da `autoMemoryDirectory` del settings del nodo
    (o BRAIN_MEMORY_DIR, default <root>/.memory), le cartelle operative da
    `brain.profile.yaml: ops_dirs` (relative al nodo) + il CLAUDE.md del nodo;
  - tolte le sentinelle VMR-specifiche (trascrizioni Fathom, rami fuori da `master`);
  - aggiunto `--mark-fed`: aggiorna `workspace/feed-state.json` del nodo (last_fed_sha,
    last_fed_at, memory_mtime con precisione piena di os.stat) -- lo usa la skill feed
    PRIMA del sync, cosi' lo stato entra nel commit;
  - stato ASSENTE = sentinella muta (si inizializza con --mark-fed; `--list` lo dice).

Chiamato dall'hook Stop (hooks/stop_feed.py) a fine turno: confronta il LAVORO OPERATIVO
(ops_dirs via git + memory dir via mtime) con l'ultimo feed e stampa UNA riga se c'e'
qualcosa di non ancora compilato nel vault. Exit 0 SEMPRE, stdlib-only, < 1 s.

Uso:
    python scripts/feed_check.py              # hook: 1 riga, muto se allineato
    python scripts/feed_check.py --list       # origine<TAB>file (per la skill feed)
    python scripts/feed_check.py --mark-fed   # registra "tutto compilato fin qui"
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_config as kc  # noqa: E402

SKIP_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".html",
             ".xlsx", ".xls", ".docx", ".pptx", ".mp3", ".mp4", ".wav", ".pyc"}
SKIP_SUBDIRS = ("assets/",)


def state_path(node: kc.Node) -> Path:
    return node.dir / "workspace" / "feed-state.json"


def watched(node: kc.Node) -> list[str]:
    """Pathspec git (relativi alla RADICE del repo) osservati per questo nodo."""
    out: list[str] = []
    claude_md = node.dir / "CLAUDE.md"
    if claude_md.is_file():
        out.append(node.rel_root(claude_md))
    for d in node.profile.ops_dirs:
        p = node.dir / d
        if p.is_dir():
            out.append(node.rel_root(p) + "/")
    return out


def _relevant(path: str, node: kc.Node) -> bool:
    q = path.replace("\\", "/")
    for d in node.profile.ops_dirs:
        base = (d if node.is_root else f"{node.path}/{d}").strip("/") + "/"
        if any(q.startswith(base + s) for s in SKIP_SUBDIRS):
            return False
    return Path(q).suffix.lower() not in SKIP_EXTS


def _git(root: Path, args: list[str]) -> list[str]:
    """git nella radice, tollerante: errore/timeout = lista vuota.
    quotepath=off: path non-ASCII in chiaro."""
    try:
        r = subprocess.run(["git", "--no-optional-locks", "-c", "core.quotepath=off", *args], cwd=str(root),
                           capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            return []
        return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    except Exception:  # noqa: BLE001
        return []


def pending(root: Path, node: kc.Node) -> dict[str, list[str]]:
    """File operativi cambiati dopo l'ultimo feed, per origine (committed/dirty/memory)."""
    sp = state_path(node)
    if not sp.exists():
        return {"_warn": ["feed-state.json assente: inizializza con `scripts/feed_check.py --mark-fed`"]}
    try:
        state = json.loads(kc.read_text(sp))
    except Exception:  # noqa: BLE001
        return {"_warn": ["feed-state.json illeggibile: rigeneralo con --mark-fed"]}
    out: dict[str, list[str]] = {"committed": [], "dirty": [], "memory": []}
    paths = watched(node)
    sha = str(state.get("last_fed_sha", "") or "")
    if paths:
        if sha and _git(root, ["rev-parse", "--quiet", "--verify", sha + "^" + "{commit}"]):
            out["committed"] = [p for p in _git(root, ["diff", "--name-only", f"{sha}..HEAD", "--", *paths])
                                if _relevant(p, node)]
        elif sha:
            out["_warn"] = ["last_fed_sha non risolve (rebase?): pendenze committed non rilevabili, "
                            "rifai il feed e --mark-fed per riallineare"]
        for ln in _git(root, ["status", "--porcelain", "--", *paths]):
            parts = ln.split(None, 1)
            if len(parts) < 2:
                continue
            p = parts[1].strip().strip('"')
            if " -> " in p:
                p = p.split(" -> ", 1)[1].strip().strip('"')
            if p and _relevant(p, node) and p not in out["dirty"]:
                out["dirty"].append(p)
    mem_mtime = float(state.get("memory_mtime", 0) or 0)
    mem = kc.memory_dir(root, node)
    try:
        if mem.is_dir():
            for f in sorted(mem.rglob("*.md")):
                if f.stat().st_mtime > mem_mtime:
                    out["memory"].append(f.name)
    except Exception:  # noqa: BLE001
        pass
    out["committed"] = [p for p in out["committed"] if p not in out["dirty"]]
    return {k: v for k, v in out.items() if v}


def mark_fed(root: Path, node: kc.Node) -> dict:
    head = _git(root, ["rev-parse", "HEAD"])
    mem = kc.memory_dir(root, node)
    mtime = 0.0
    try:
        if mem.is_dir():
            mtime = max((f.stat().st_mtime for f in mem.rglob("*.md")), default=0.0)
    except Exception:  # noqa: BLE001
        pass
    state = {"last_fed_sha": head[0] if head else "", "last_fed_at": dt.datetime.now().isoformat(timespec="seconds"),
             "memory_mtime": mtime, "node_path": node.path}
    kc.write_text(state_path(node), json.dumps(state, indent=2) + "\n")
    return state


def summary_line(root: Path, node: kc.Node) -> str:
    """La riga dell'hook Stop: vuota se non c'e' lavoro non compilato."""
    pend = pending(root, node)
    pend.pop("_warn", None)
    n = sum(len(v) for v in pend.values())
    bid = kc.brain_id(root, strict=False) or "brain"
    return f"[brain-feed] {n} modifiche operative non ancora nel brain -> /{bid}-feed" if n else ""


def main(argv: list[str] | None = None) -> int:
    kc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Sentinella del lavoro operativo non ancora compilato nel vault.")
    kc.add_common_args(ap)
    ap.add_argument("--list", action="store_true", help="elenco origine<TAB>file")
    ap.add_argument("--mark-fed", action="store_true", help="registra lo stato 'tutto compilato fin qui'")
    a = ap.parse_args(argv)
    try:
        root = kc.resolve_root(a.root)
        node = kc.current_node(root, override=a.node)
        if a.mark_fed:
            st = mark_fed(root, node)
            print(f"feed-state aggiornato: sha={st['last_fed_sha'][:8] or '-'} at={st['last_fed_at']} "
                  f"memory_mtime={st['memory_mtime']:.6f} ({state_path(node)})")
            return 0
        pend = pending(root, node)
        warns = pend.pop("_warn", [])
        if a.list:
            for w in warns:
                print(f"warn\t{w}")
            for origin, files in pend.items():
                for f in files:
                    print(f"{origin}\t{f}")
            return 0
        line = summary_line(root, node)
        if line:
            print(line)
    except Exception:  # noqa: BLE001 -- sentinella: mai bloccante, mai rumorosa
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
