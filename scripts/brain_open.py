#!/usr/bin/env python3
"""brain_open.py - launcher di una sessione nel brain (SPEC v3.1 par. 9).

  brain_open.py <node_root> [--agent claude|codex] [--skip-doctor] [--write] [--dry-run] [--] [args...]

Rifiuta se <node_root> non ha nodo.md/brain.yaml (una sottocartella non e' un nodo); crea la cartella della
memoria automatica se manca; esegue doctor.py (stessa cartella) e si ferma sui CRIT; rifiuta --resume/-r,
--continue/-c e --add-dir negli argomenti (una sessione non si riprende da un altro brain e non si allarga);
lancia `claude` nella radice oppure `codex --sandbox read-only` (`workspace-write` solo con --write, con
avviso sul trust); logga l'avvio in <GUARD_HOME>/audit/open.log. Solo stdlib.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

NODE_MARKERS = ["nodo.md", "brain.yaml"]
MANIFEST_NAME = ".template-manifest.json"
FORBIDDEN = {"--resume", "-r", "--continue", "-c", "--add-dir"}


def which_any(names: list[str]) -> str | None:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def default_guard_home() -> Path:
    if os.environ.get("BRAIN_GUARD_HOME"):
        return Path(os.environ["BRAIN_GUARD_HOME"])
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "brain-guard"
    return Path.home() / ".brain-guard"


def read_frontmatter_min(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return {}
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    out: dict = {}
    if not m:
        return out
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def find_brain_root(node: Path) -> Path:
    cur = node
    while True:
        if (cur / "brain.yaml").is_file():
            return cur
        if cur.parent == cur:
            return node
        cur = cur.parent


def forbidden_args(args: list[str]) -> list[str]:
    bad = []
    for a in args:
        base = a.split("=", 1)[0]
        if base in FORBIDDEN:
            bad.append(a)
    return bad


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Apre una sessione nel brain dopo il doctor.")
    ap.add_argument("node_root")
    ap.add_argument("--agent", choices=["claude", "codex"], default="claude")
    ap.add_argument("--skip-doctor", action="store_true", help="non eseguire il doctor (sconsigliato; VS Code lo salta comunque)")
    ap.add_argument("--write", action="store_true", help="codex: --sandbox workspace-write invece di read-only")
    ap.add_argument("--dry-run", action="store_true", help="stampa il comando senza lanciarlo")
    ap.epilog = "Tutto cio' che segue `--` viene passato all'agente (es. `-- --model opus`)."
    if "--" in argv:
        idx = argv.index("--")
        own, extra = argv[:idx], argv[idx + 1:]
    else:
        own, extra = list(argv), []
    a = ap.parse_args(own)

    node = Path(a.node_root).resolve()
    if not node.is_dir():
        print(f"ERRORE: {node} non esiste", file=sys.stderr)
        return 2
    marker = next((m for m in NODE_MARKERS if (node / m).is_file()), None)
    if marker is None:
        print(f"ERRORE: {node} non e' la radice di un brain o di un nodo (manca nodo.md/brain.yaml). "
              "Le sessioni si aprono nella radice del nodo, mai in una sottocartella.", file=sys.stderr)
        return 2
    bad = forbidden_args(extra)
    if bad:
        print(f"ERRORE: argomenti non ammessi da brain_open: {' '.join(bad)} "
              "(una sessione non si riprende da un altro brain e non si allarga con --add-dir)", file=sys.stderr)
        return 2

    brain_root = find_brain_root(node)
    fm = read_frontmatter_min(node / "nodo.md") if (node / "nodo.md").is_file() else {}
    brain_id = fm.get("brain_id", brain_root.name)
    node_path = fm.get("node_path", ".")
    manifest_ctx: dict = {}
    try:
        manifest_ctx = (json.loads((node / MANIFEST_NAME).read_text(encoding="utf-8-sig")) or {}).get("context") or {}
    except (OSError, json.JSONDecodeError):
        pass

    # memoria automatica
    mem = None
    try:
        settings = json.loads((node / ".claude" / "settings.json").read_text(encoding="utf-8-sig"))
        mem = settings.get("autoMemoryDirectory")
    except (OSError, json.JSONDecodeError):
        settings = None
    mem_dir = Path(os.path.expanduser(mem)) if isinstance(mem, str) and mem else node / ".memory"
    if not mem_dir.exists():
        mem_dir.mkdir(parents=True, exist_ok=True)
        print(f"[brain_open] creata la cartella della memoria: {mem_dir}")

    # doctor
    doctor_status = "skipped"
    if a.skip_doctor:
        print("[brain_open] ATTENZIONE: doctor saltato (--skip-doctor)")
    else:
        doctor = Path(__file__).resolve().parent / "doctor.py"
        if not doctor.is_file():
            print(f"ERRORE: doctor.py non trovato accanto a brain_open.py ({doctor})", file=sys.stderr)
            return 2
        cmd = [sys.executable, str(doctor), "--node", str(node)]
        if node != brain_root and fm.get("mode") == "internal":
            cmd += ["--allow-ancestors", str(brain_root)]
        rc = subprocess.call(cmd)
        if rc != 0:
            print(f"[brain_open] doctor exit {rc}: controlli critici falliti, sessione NON aperta.", file=sys.stderr)
            return rc
        doctor_status = "ok"

    # comando dell'agente
    if a.agent == "claude":
        exe = which_any(["claude.exe", "claude.cmd", "claude"] if os.name == "nt" else ["claude"])
        if not exe:
            print("ERRORE: `claude` non trovato nel PATH", file=sys.stderr)
            return 2
        cmd = [exe] + extra
    else:
        exe = which_any(["codex.cmd", "codex.exe", "codex"] if os.name == "nt" else ["codex"])
        if not exe:
            print("ERRORE: `codex` non trovato nel PATH", file=sys.stderr)
            return 2
        sandbox = "workspace-write" if a.write else "read-only"
        if a.write:
            print("[brain_open] ATTENZIONE: con workspace-write Codex puo' scrivere trust_level=\"trusted\" in ~/.codex/config.toml senza avviso.")
        cmd = [exe, "--sandbox", sandbox, "-C", str(node)] + extra

    # audit (fuori dal brain): GUARD_HOME da ambiente, poi env dei settings, poi manifest, poi default
    settings_env = (settings or {}).get("env") if isinstance((settings or {}).get("env"), dict) else {}
    if os.environ.get("BRAIN_GUARD_HOME"):
        guard_home = Path(os.environ["BRAIN_GUARD_HOME"])
    elif settings_env.get("BRAIN_GUARD_HOME"):
        guard_home = Path(str(settings_env["BRAIN_GUARD_HOME"]))
    elif manifest_ctx.get("GUARD_HOME"):
        guard_home = Path(str(manifest_ctx["GUARD_HOME"]))
    else:
        guard_home = default_guard_home()
    try:
        audit = guard_home / "audit"
        audit.mkdir(parents=True, exist_ok=True)
        line = "\t".join([_dt.datetime.now().astimezone().isoformat(timespec="seconds"), brain_id, node_path, a.agent,
                          str(node), f"doctor={doctor_status}", "dry-run" if a.dry_run else "launch"])
        with open(audit / "open.log", "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
    except OSError as e:
        print(f"[brain_open] avviso: audit non scritto ({e})")

    print(f"[brain_open] {brain_id} nodo {node_path} -> {' '.join(cmd)}  (cwd {node})")
    if a.dry_run:
        return 0
    try:
        return subprocess.call(cmd, cwd=str(node))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
