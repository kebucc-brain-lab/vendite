"""lint_kb -- lint strutturale del vault contro CONVENTIONS.md (Brain Builder v3.1).

Forkato da `scripts/lint_kb.py` di VMR Brain (HEAD 0efa9254). Cosa cambia:
  - gate sull'ALLOW-LIST del profilo (`brain.profile.yaml: folders`), per nodo; le
    cartelle di sistema sono escluse a prescindere (kb_config.EXCLUDE_DIRS);
  - frontmatter letto con PyYAML: YAML invalido (es. `title: a: b` non quotato) = ERRORE
    con riga; BOM UTF-8 tollerato ma segnalato (WARN); UnicodeDecodeError gestito;
  - stem duplicati nel brain = ERRORE (i wikilink risolvono per stem: deve essere unico);
  - i `[[link]]` dentro code fence (``` / ~~~) e inline code sono ignorati;
  - esenzioni da minimo-link e orfano: `_index`, meta di radice (README, CONVENTIONS),
    deliverable nelle sottocartelle di outputs/, schede code/<x>.md, note `type/asset`
    e `status: derived`; le sottocartelle di code/ sono codice vivo: ignorate;
  - in un albero (master + nodi) un link e' ammesso solo verso note del proprio nodo o
    degli antenati: un link verso un nodo fratello/discendente = ERRORE;
  - vault vuoto = OK (0 note, 0 errori): e' lo stato di un brain appena generato.

Gate (ERROR -> exit 1): frontmatter completo e valido, >= 3 wikilink uscenti, zero broken
link, zero orfani, profondita' <= 4 (relativa al nodo), stem unici, link nel dominio.
WARN (exit 0): body > 300 righe, BOM, source: non risolvibile, grafo sconnesso, file .md
alla radice fuori convenzione.

Uso:  python scripts/lint_kb.py [--root R] [--node P]      (default: tutto il brain)
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_config as kc  # noqa: E402

REQUIRED_KEYS = ("title", "summary", "tags", "status", "created", "updated")
WARN_BODY = 300
MIN_LINKS = 3
MAX_DEPTH = 4

WIKILINK = re.compile(r"\[\[([^\]\|#]+)(?:[#\|][^\]]*)?\]\]")
BAD_COLON = re.compile(r"^(?:title|summary):\s+[^\"'\n]*:\s", re.M)
FENCE = re.compile(r"(^|\n)(```|~~~)[^\n]*\n.*?(\n\2[ \t]*(?=\n|$)|\Z)", re.S)
INLINE_CODE = re.compile(r"`[^`\n]*`")


@dataclass
class Note:
    path: Path
    node: kc.Node
    rel: str                 # relativo alla radice del brain
    stem: str
    fm: dict = field(default_factory=dict)
    body: str = ""
    raw_fm: str = ""
    error: str | None = None
    is_meta: bool = False
    exempt: bool = False
    links: set[str] = field(default_factory=set)

    @property
    def node_rel(self) -> str:
        return self.node.rel(self.path)


def strip_code(text: str) -> str:
    """Toglie code fence e inline code: i wikilink li' dentro sono esempi, non link."""
    text = FENCE.sub("\n", text)
    return INLINE_CODE.sub("", text)


def outgoing_links(text: str) -> set[str]:
    return {t.strip() for t in WIKILINK.findall(text) if t.strip()}


def _exempt(note: Note) -> bool:
    parts = Path(note.node_rel).parts
    if note.stem == "_index" or note.is_meta:
        return True
    tags = kc.fm_str(note.fm.get("tags"))
    if "type/asset" in tags or kc.fm_str(note.fm.get("status")) == "derived":
        return True
    if parts and parts[0] == "outputs" and len(parts) > 2:     # deliverable in sottocartella
        return True
    if parts and parts[0] == "code" and len(parts) == 2:       # scheda del codice importato
        return True
    return False


def collect(root: Path) -> tuple[list[kc.Node], list[Note], list[str]]:
    nodes = kc.iter_nodes(root)
    notes: list[Note] = []
    warnings: list[str] = []
    for node in nodes:
        files = kc.iter_notes(node, nodes)
        metas = kc.root_meta_files(node)
        for p in [*files, *metas]:
            n = Note(path=p, node=node, rel=node.rel_root(p), stem=p.stem, is_meta=p in metas)
            notes.append(n)
        for p in sorted(node.dir.glob("*.md")):
            if p.name in kc.ROOT_SKIP or p.name in kc.ROOT_META:
                continue
            warnings.append(f"{node.rel_root(p)}: file .md alla radice del nodo fuori convenzione "
                            f"(spostalo in una cartella del profilo o in workspace/)")
    return nodes, notes, warnings


def lint(root: Path, only_node: str | None = None) -> tuple[list[str], list[str], dict]:
    nodes, notes, warnings = collect(root)
    errors: list[str] = []
    by_path = {n.path: n for n in nodes}
    if only_node is not None and only_node not in by_path:
        raise kc.ConfigError(f"nodo {only_node!r} inesistente (nodi: {', '.join(by_path)})")

    def reported(note: Note) -> bool:
        return only_node is None or note.node.path == only_node

    # 1. lettura + frontmatter
    for n in notes:
        if kc.has_bom(n.path):
            warnings.append(f"{n.rel}: BOM UTF-8 in testa al file (rimuovilo: Obsidian e git lo mostrano come carattere spurio)")
        try:
            text = n.path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as e:
            n.error = f"non e' UTF-8 valido ({e.reason} al byte {e.start})"
            continue
        except OSError as e:
            n.error = f"illeggibile ({e.__class__.__name__})"
            continue
        fmm = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
        n.raw_fm = fmm.group(1) if fmm else ""
        try:
            n.fm, n.body = kc.read_frontmatter(text)
        except kc.FrontmatterError as e:
            hint = " -- title/summary con ':' seguito da spazio vanno tra virgolette doppie" \
                if BAD_COLON.search(n.raw_fm) else ""
            n.error = f"frontmatter {e}{hint}"
            n.fm, n.body = {}, text
            continue
        if not n.fm and BAD_COLON.search(n.raw_fm):
            n.error = "title/summary con ':' non quotato (YAML invalido: quotalo)"
        n.exempt = _exempt(n)
        n.links = outgoing_links(strip_code(n.body)) | outgoing_links(kc.fm_str(n.fm.get("related")))

    # 2. stem unici (le _index sono una per cartella, per costruzione)
    stems: dict[str, list[Note]] = {}
    for n in notes:
        if n.stem != "_index" and n.error is None:
            stems.setdefault(n.stem, []).append(n)
    for stem, lst in sorted(stems.items()):
        if len(lst) > 1 and any(reported(x) for x in lst):
            errors.append(f"stem duplicato [{stem}]: " + ", ".join(sorted(x.rel for x in lst))
                          + " -- i wikilink risolvono per stem: rinomina (prefisso di dominio)")
    visible_cache: dict[str, dict[str, Note]] = {}

    def visible(node: kc.Node) -> dict[str, Note]:
        if node.path not in visible_cache:
            allowed = set(node.visible_paths())
            visible_cache[node.path] = {s: lst[0] for s, lst in stems.items()
                                        if lst[0].node.path in allowed}
        return visible_cache[node.path]

    incoming: dict[str, int] = {}
    adjacency: dict[str, set[str]] = {n.rel: set() for n in notes}

    # 3. per nota: chiavi, profondita', corpo, link
    for n in notes:
        if n.error is not None:
            if reported(n):
                errors.append(f"{n.rel}: {n.error}")
            continue
        depth = len(Path(n.node_rel).parts)
        if depth > MAX_DEPTH and reported(n):
            errors.append(f"{n.rel}: profondita' {depth} > {MAX_DEPTH} (relativa al nodo)")
        missing = [k for k in REQUIRED_KEYS if k not in n.fm or kc.fm_str(n.fm[k]) == ""]
        if missing and reported(n):
            errors.append(f"{n.rel}: frontmatter incompleto, mancano {missing}")
        for src in (s.strip() for s in kc.fm_str(n.fm.get("source")).split(";") if s.strip()):
            if src.startswith("sources/") and not (n.node.dir / src).exists() and not (root / src).exists():
                if reported(n):
                    warnings.append(f"{n.rel}: source non risolvibile '{src}'")
        body_lines = n.body.count("\n") + 1 if n.body.strip() else 0
        if body_lines > WARN_BODY and reported(n):
            warnings.append(f"{n.rel}: corpo di {body_lines} righe (> {WARN_BODY}: valuta uno split)")
        if not n.exempt and len(n.links) < MIN_LINKS and reported(n):
            errors.append(f"{n.rel}: {len(n.links)} wikilink uscenti < {MIN_LINKS}")
        vis = visible(n.node)
        for tgt in sorted(n.links):
            if tgt == "_index":
                continue
            if tgt in vis:
                t = vis[tgt]
                incoming[t.rel] = incoming.get(t.rel, 0) + 1
                adjacency[n.rel].add(t.rel)
                adjacency.setdefault(t.rel, set()).add(n.rel)
            elif tgt in stems:
                other = stems[tgt][0].node.path
                if reported(n):
                    errors.append(f"{n.rel}: link " f"[" f"[{tgt}]] verso il nodo '{other}' che non e' "
                                  f"antenato di '{n.node.path}' (ammessi: nodo + antenati)")
            elif reported(n):
                errors.append(f"{n.rel}: wikilink rotto " f"[" f"[{tgt}]]")

    # 4. orfani
    for n in notes:
        if n.error is not None or n.exempt or not reported(n):
            continue
        if incoming.get(n.rel, 0) == 0:
            errors.append(f"{n.rel}: ORFANA (0 link entranti)")

    # 5. componenti connesse (solo avviso), calcolate sulle note REGOLARI: _index, meta di
    # radice e deliverable esenti non sono nodi del grafo e falserebbero il conteggio
    seen: set[str] = set()
    components = 0
    for start in (n.rel for n in notes if n.error is None and not n.exempt):
        if start in seen:
            continue
        components += 1
        stack = [start]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(adjacency.get(cur, set()) - seen)
    if components > 1:
        warnings.append(f"grafo con {components} componenti sconnesse (collegale)")

    stats = {"notes": len(notes), "nodes": len(nodes), "components": components,
             "errors": len(errors), "warnings": len(warnings)}
    return errors, warnings, stats


def main(argv: list[str] | None = None) -> int:
    kc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Lint strutturale del vault (0 errori = gate verde).")
    kc.add_common_args(ap)
    a = ap.parse_args(argv)
    try:
        root = kc.resolve_root(a.root)
        errors, warnings, st = lint(root, a.node)
    except kc.ConfigError as e:
        print(f"ERRORE configurazione: {e}")
        return 2
    scope = f"nodo '{a.node}'" if a.node else f"{st['nodes']} nodo/i"
    print(f"Lint: {st['notes']} note compilate in {root.name}/ ({scope}), componenti del grafo: {st['components']}")
    if warnings:
        print(f"\nWARNINGS ({len(warnings)}):")
        for w in warnings:
            print(f"  ! {w}")
    if errors:
        print(f"\nERRORS ({len(errors)}):")
        for e in errors:
            print(f"  x {e}")
        print("\nFAIL")
        return 1
    print("\nOK -- 0 errori")
    return 0


if __name__ == "__main__":
    sys.exit(main())
