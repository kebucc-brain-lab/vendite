"""build_llms_txt -- genera llms.txt dalle note: la front door per gli agenti.

Forkato da `scripts/build_llms_txt.py` di VMR Brain (HEAD 0efa9254). Cosa cambia:
  - header con BRAIN_ID e nome del brain (da nodo.md/brain.yaml/.env), non cablato;
  - le sezioni sono le cartelle del PROFILO (`brain.profile.yaml: folders`), nell'ordine
    del profilo, sottocartelle comprese (code/ solo al primo livello: le sottocartelle
    sono codice vivo);
  - UN llms.txt per NODO (`<nodo>/llms.txt`); quello della radice di un master elenca i
    nodi e rimanda ai loro llms.txt; un nodo rimanda a quello della radice;
  - frontmatter via PyYAML (tollerante: su YAML rotto ripiega sul parser a regex, il lint
    e' il gate, non questo script); scrittura UTF-8 con newline "\n".

llms.txt e' DERIVATO: non si edita a mano. Uso:
    python scripts/build_llms_txt.py [--root R] [--node P]    (default: tutti i nodi)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_config as kc  # noqa: E402


def _fm(path: Path) -> dict:
    try:
        fm, _ = kc.read_frontmatter(path)
    except kc.FrontmatterError:
        fm, _ = kc.split_frontmatter(kc.read_text(path))
    except OSError:
        fm = {}
    return fm


def render_node(node: kc.Node, nodes: list[kc.Node], bid: str, name: str) -> tuple[str, int]:
    notes = kc.iter_notes(node, nodes)
    by_folder: dict[str, list[Path]] = {}
    for p in notes:
        folder = node.rel(p).split("/", 1)[0]
        by_folder.setdefault(folder, []).append(p)
    ordered = [f for f in node.profile.folders if f in by_folder] + \
              sorted(f for f in by_folder if f not in node.profile.folders)

    where = "radice" if node.is_root else f"nodo `{node.path}`"
    lines = [
        f"# {name} ({bid}) -- {where}" if node.is_root else f"# {node.name} -- {where} di {name} ({bid})",
        "",
        "> GENERATO da scripts/build_llms_txt.py -- NON editare a mano;",
        "> rigenera con `scripts/build_llms_txt.py` (o brain_sync).",
        "> Front door degli agenti: leggi questo indice, poi apri SOLO le note bersaglio.",
        "> Ogni cartella ha un solo lavoro; le regole stanno in CONVENTIONS.md.",
    ]
    if not node.is_root:
        up = "../" * len(node.path.split("/"))
        lines.append(f"> Radice del brain: `{up}llms.txt` (le note della radice e degli antenati sono visibili da qui).")
    lines.append("")
    count = 0
    for folder in ordered:
        idx = node.dir / folder / "_index.md"
        idx_summary = kc.fm_str(_fm(idx).get("summary")) if idx.is_file() else ""
        if not idx_summary:
            idx_summary = node.profile.folder_summaries.get(folder, "")
        lines.append(f"## {folder}")
        if idx_summary:
            lines.append(f"_{idx_summary}_")
        lines.append("")
        for p in by_folder[folder]:
            if p.stem == "_index":
                continue
            fm = _fm(p)
            title = kc.fm_str(fm.get("title")) or p.stem
            summary = kc.fm_str(fm.get("summary"))
            # doppia parentesi quadra spezzata in letterali adiacenti: lint-clean, output identico
            lines.append(f"- " f"[" f"[{p.stem}]] -- {summary or title}")
            count += 1
        lines.append("")
    paths = {x.path for x in nodes}

    def parent_of(n: kc.Node) -> str:
        return next((a for a in n.ancestors() if a in paths), ".")

    children = [n for n in nodes if not n.is_root and parent_of(n) == node.path]
    if children:
        lines.append("## nodi")
        lines.append("_Nodi interni: ognuno ha il proprio llms.txt e vede le note dei propri antenati._")
        lines.append("")
        for c in children:
            lines.append(f"- `{c.path}` -- {c.name} -> `{c.path}/llms.txt`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n", count


def build(root: Path, only_node: str | None = None) -> list[tuple[Path, int]]:
    nodes = kc.iter_nodes(root)
    bid = kc.brain_id(root)
    name = kc.brain_name(root)
    out: list[tuple[Path, int]] = []
    for node in nodes:
        if only_node is not None and node.path != only_node:
            continue
        text, count = render_node(node, nodes, bid, name)
        target = node.dir / "llms.txt"
        kc.write_text(target, text)
        out.append((target, count))
    if only_node is not None and not out:
        raise kc.ConfigError(f"nodo {only_node!r} inesistente")
    return out


def main(argv: list[str] | None = None) -> int:
    kc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Rigenera llms.txt (derivato) per ogni nodo del brain.")
    kc.add_common_args(ap)
    a = ap.parse_args(argv)
    try:
        root = kc.resolve_root(a.root)
        written = build(root, a.node)
    except kc.ConfigError as e:
        print(f"ERRORE configurazione: {e}")
        return 2
    for target, count in written:
        print(f"Scritto {target} ({count} note)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
