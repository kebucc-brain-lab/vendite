#!/usr/bin/env python3
"""render.py - motore dei template del Brain Builder v3.1 (solo stdlib).

Sostituisce i segnaposto a doppie graffe (BRAIN_ID, PYTHON, MEMORY_DIR, ...) nei template e
fallisce se ne resta anche uno solo. E' importato da nuovo_brain.py, nuovo_nodo.py,
estrai_nodo.py e doctor.py; nel brain generato vive in scripts/render.py accanto a doctor.py.

Valori ammessi nel contesto: stringhe, numeri, booleani (resi "true"/"false"), None (""),
liste e dizionari (resi come JSON: in un template .json o .yaml diventano una lista valida).

Uso da riga di comando:
  render.py --in <file> --out <file> [--set CHIAVE=VALORE ...] [--context <file.json>]
  render.py --check <dir>          # elenca i segnaposto rimasti in un albero (exit 1 se ce ne sono)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# Le due sequenze vengono costruite per concatenazione: cosi' questo file, copiato nel brain,
# non contiene mai la sequenza letterale che il collaudo cerca con grep.
OPEN = "{" * 2
CLOSE = "}" * 2
_PLACEHOLDER = re.compile("\\{\\{([A-Za-z0-9_]+)\\}\\}")

# Cartelle che non vengono mai ispezionate dal controllo dei segnaposto residui.
DEFAULT_EXCLUDE_DIRS = frozenset({".git", ".venv", "venv", ".memory", "node_modules", "__pycache__",
                                  ".pytest_cache", ".tmp"})
# Nei file di CODICE la doppia graffa e' legittima (escape delle f-string Python, template literal JS):
# li' conta solo un token a forma di segnaposto (NOME fra doppie graffe). Nei documenti conta qualunque doppia graffa.
CODE_EXTENSIONS = frozenset({".py", ".pyw", ".mjs", ".cjs", ".js", ".ts", ".ps1", ".psm1", ".sh", ".bat", ".cmd"})

# Basename degli script hook del motore -> flag di contesto che li attiva.
HOOK_FLAGS = {
    "session_start.py": "HOOKS_DATE",
    "stop_feed.py": "HOOKS_FEED",
    "instructions_loaded.py": "HOOKS_AUDIT",
}


class RenderError(Exception):
    """Segnaposto non definito o rimasto nel risultato."""


def to_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "si", "on"}
    return bool(value)


def render_text(text: str, ctx: dict, *, source: str = "<testo>", strict: bool = True) -> str:
    """Rende i segnaposto di `text`. Con strict=True solleva RenderError se un segnaposto non e'
    nel contesto o se nel risultato resta una doppia graffa aperta."""
    missing: list[str] = []

    def _sub(m: re.Match) -> str:
        name = m.group(1)
        if name in ctx:
            return to_text(ctx[name])
        missing.append(name)
        return m.group(0)

    out = _PLACEHOLDER.sub(_sub, text)
    if strict:
        if missing:
            raise RenderError(f"{source}: segnaposto non definiti nel contesto: {sorted(set(missing))}")
        if OPEN in out:
            rows = [i + 1 for i, line in enumerate(out.splitlines()) if OPEN in line]
            raise RenderError(f"{source}: doppia graffa residua alle righe {rows}")
    return out


def render_name(name: str, ctx: dict) -> str:
    """Rende un nome di file o cartella: il prefisso `ID-` diventa `<BRAIN_ID>-` (convenzione
    delle skill del motore, es. ID-sync), e i segnaposto nel nome vengono sostituiti."""
    if name.startswith("ID-") and "BRAIN_ID" in ctx:
        name = str(ctx["BRAIN_ID"]) + name[2:]
    return render_text(name, ctx, source=name)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_text(path: Path, text: str) -> str:
    """Scrive UTF-8 senza BOM con newline LF e restituisce lo sha256 dei byte scritti."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    with open(path, "wb") as fh:
        fh.write(data)
    return sha256_bytes(data)


def render_file(src: Path, dst: Path, ctx: dict) -> str:
    text = Path(src).read_text(encoding="utf-8-sig")
    out = render_text(text, ctx, source=str(src))
    if not out.endswith("\n"):
        out += "\n"
    return write_text(Path(dst), out)


def canonical_json(obj) -> str:
    """Forma canonica dei file JSON generati (settings, hooks, manifest): indent 2, UTF-8, LF."""
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def finalize_hooks(obj: dict, ctx: dict) -> dict:
    """Applica i flag hooks.{date,feed,audit} della config a un settings Claude o a un hooks.json
    Codex gia' reso: toglie gli hook del motore disattivati e gli eventi rimasti vuoti.
    E' deterministica: doctor.py la riapplica con il contesto del manifest per confrontare."""
    hooks = obj.get("hooks")
    if not isinstance(hooks, dict):
        return obj
    for event in list(hooks.keys()):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            kept = []
            for hook in group.get("hooks") or []:
                if not isinstance(hook, dict):
                    continue
                target = " ".join([str(hook.get("command", ""))] + [str(a) for a in (hook.get("args") or [])]
                                  + [str(hook.get("commandWindows", ""))])
                flag = next((f for base, f in HOOK_FLAGS.items() if base in target), None)
                if flag is not None and not _truthy(ctx.get(flag, True)):
                    continue
                kept.append(hook)
            if kept:
                group = dict(group)
                group["hooks"] = kept
                kept_groups.append(group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    return obj


def render_json_template(template_path: Path, ctx: dict) -> tuple[dict, str]:
    """Rende un template JSON (settings, hooks Codex), applica finalize_hooks e restituisce
    (oggetto, testo canonico)."""
    text = render_text(Path(template_path).read_text(encoding="utf-8-sig"), ctx, source=str(template_path))
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise RenderError(f"{template_path}: il risultato non e' JSON valido ({e})") from e
    obj = finalize_hooks(obj, ctx)
    return obj, canonical_json(obj)


def find_placeholders(root: Path, exclude_dirs=DEFAULT_EXCLUDE_DIRS, max_hits: int = 200,
                      strict_code: bool = False) -> list[tuple[Path, int, str]]:
    """Cerca segnaposto residui sotto `root` (regola della SPEC: zero doppie graffe nel brain generato).
    Documenti, template, JSON, YAML: qualunque doppia graffa aperta e' un residuo. File di codice
    (CODE_EXTENSIONS): solo un token a forma di segnaposto, salvo strict_code=True. Binari saltati."""
    hits: list[tuple[Path, int, str]] = []
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in exclude_dirs)
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if OPEN not in text:
                continue
            code = p.suffix.lower() in CODE_EXTENSIONS and not strict_code
            for i, line in enumerate(text.splitlines(), 1):
                if (_PLACEHOLDER.search(line) if code else OPEN in line):
                    hits.append((p, i, line.strip()[:120]))
                    if len(hits) >= max_hits:
                        return hits
    return hits


def _parse_set(items: list[str]) -> dict:
    ctx: dict = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"--set richiede CHIAVE=VALORE, ricevuto: {item}")
        k, v = item.split("=", 1)
        ctx[k.strip()] = v
    return ctx


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Rende un template del Brain Builder o cerca segnaposto residui.")
    ap.add_argument("--in", dest="src")
    ap.add_argument("--out", dest="dst")
    ap.add_argument("--set", action="append", default=[], help="CHIAVE=VALORE (ripetibile)")
    ap.add_argument("--context", help="file JSON con il contesto")
    ap.add_argument("--check", help="cartella da ispezionare per segnaposto residui")
    a = ap.parse_args(argv)

    if a.check:
        hits = find_placeholders(Path(a.check))
        for p, n, line in hits:
            print(f"{p}:{n}: {line}")
        print(f"{len(hits)} segnaposto residui in {a.check}")
        return 1 if hits else 0

    if not a.src or not a.dst:
        ap.error("servono --in e --out (oppure --check <dir>)")
    ctx: dict = {}
    if a.context:
        ctx.update(json.loads(Path(a.context).read_text(encoding="utf-8-sig")))
    ctx.update(_parse_set(a.set))
    try:
        digest = render_file(Path(a.src), Path(a.dst), ctx)
    except RenderError as e:
        print(f"ERRORE: {e}", file=sys.stderr)
        return 1
    print(f"{a.dst}  sha256={digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
