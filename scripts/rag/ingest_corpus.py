"""ingest_corpus -- strato CORPUS (materiale grezzo) del brain, collection separata.

Copiato da `kit/rag/ingest_corpus.py` (3.0, patch tri-stato di VMR HEAD 0efa9254).
Cosa cambia:
  - le radici del corpus vengono dal PROFILO (`brain.profile.yaml: corpus_roots`,
    relative alla cartella del nodo), non dal layout VMR (`sources/workspace-import/...`);
  - collection da RAG_CORPUS_COLLECTION, nessun default: assente = nessun corpus
    configurato (exit 0 con messaggio, non e' un errore);
  - `node_path` e `brain_id` nel payload; stato per nodo (`corpus_state[.<nodekey>].json`);
  - `.env` per path esplicito; embedded solo con RAG_ALLOW_EMBEDDED=1 (store.py).

Invarianti ereditati: incrementale per content-hash; run pieno (create, mai wipe) solo se
la collection NON esiste; il wipe solo con --full esplicito; stato assente o
modello/collection cambiati con la collection viva -> stop (exit 1); Qdrant non
affidabile -> exit 2 senza toccare nulla; resumabile per file; id uuid5("corpus:<relp>#<n>").
Metadati: opzionale `manifest.json` nella cartella della fonte ({"<file>": {"title",
"date", "client"}}); fallback data dal nome `YYYY-MM-DD[_ -]...`.

Uso:
    python scripts/rag/ingest_corpus.py                  # incrementale, tutte le fonti del nodo
    python scripts/rag/ingest_corpus.py --full           # ricrea la collection
    python scripts/rag/ingest_corpus.py --source <nome>  # una sola fonte
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))         # scripts/rag
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # scripts
import kb_config as kc  # noqa: E402
from ingest import _sections, _split_long  # noqa: E402  (riuso chunking del vault)
from store import StoreConfigError, StoreUnavailable, VectorStore  # noqa: E402

BATCH = 256
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[_ \-]?")
PAYLOAD_KEYS = ("stem", "path", "node_path", "brain_id", "title", "summary", "heading", "text",
                "source_type", "date", "client", "is_corpus", "is_view", "is_session")


def _load_json(p: Path, default):
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return default
    return default


def _dump_json(p: Path, obj, **kw):
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, ensure_ascii=True, **kw))


def corpus_roots(node: kc.Node) -> list[Path]:
    return [node.dir / r for r in node.profile.corpus_roots]


def scan(node: kc.Node, source: str | None = None) -> dict[str, tuple[Path, str, Path]]:
    """relp (rispetto alla RADICE del brain, separatore /) -> (Path, nome fonte, dir fonte)."""
    files: dict[str, tuple[Path, str, Path]] = {}
    for croot in corpus_roots(node):
        if not croot.is_dir():
            continue
        for src_dir in sorted(croot.iterdir()):
            if not src_dir.is_dir() or src_dir.name.startswith("."):
                continue
            if source and src_dir.name != source:
                continue
            for ext in ("*.txt", "*.md"):
                for p in sorted(src_dir.rglob(ext)):
                    files[node.rel_root(p)] = (p, src_dir.name, src_dir)
    return files


def _meta_for(p: Path, source_name: str, manifest: dict) -> dict:
    m = manifest.get(p.name, {}) if isinstance(manifest, dict) else {}
    title = m.get("title")
    date = m.get("date")
    client = m.get("client")
    if not date:
        dm = DATE_RE.match(p.stem)
        if dm:
            date = dm.group(1)
    if not title:
        title = DATE_RE.sub("", p.stem).replace("_", " ").strip() or p.stem
    meta = {"title": title, "source_type": source_name, "is_corpus": True, "summary": "", "heading": ""}
    if date:
        meta["date"] = date
    if client:
        meta["client"] = client
    return meta


def chunk_corpus_file(p: Path, relp: str, text: str, meta: dict, node_path: str, bid: str) -> list[dict]:
    """md -> per heading; txt -> finestre. Nessun frontmatter richiesto."""
    if p.suffix.lower() == ".md":
        _fm, body = kc.split_frontmatter(text)  # tollera frontmatter se presente
        sections = _sections(body)
    else:
        sections = [("", text.strip())]
    chunks = []
    for heading, sec in sections:
        for piece in _split_long(sec):
            if not piece.strip():
                continue
            head = meta["title"] + (f" -- {heading}" if heading else "")
            chunks.append({
                "stem": p.stem, "path": relp, "node_path": node_path, "brain_id": bid,
                "title": meta["title"], "summary": "", "heading": heading, "text": piece,
                "embed_text": f"{head}\n{piece}".strip(),
                "source_type": meta["source_type"],
                "date": meta.get("date", ""), "client": meta.get("client", ""),
                "is_corpus": True, "is_view": False, "is_session": False,
            })
    for n, c in enumerate(chunks):
        c["_n"] = n
    return chunks


def _pid(relp: str, n: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"corpus:{relp}#{n}"))


def run(root: Path, node: kc.Node, source: str | None = None, full: bool = False,
        store: VectorStore | None = None, embedders=None) -> None:
    t0 = time.time()
    import embedder as emb  # noqa: PLC0415
    model, sparse_model = emb.dense_model_name(), emb.sparse_model_name()
    EmbCls, SparseCls = embedders or (emb.Embedder, emb.SparseEmbedder)
    bid = kc.brain_id(root)
    coll = (kc.require_env("RAG_CORPUS_COLLECTION") if store is None else store.collection)
    state_path = kc.state_file(root, node.path, "corpus_state")

    files = scan(node, source)
    if not files and not full:
        roots = ", ".join(str(r) for r in node.profile.corpus_roots) or "(nessuna radice nel profilo)"
        print(f"Corpus vuoto: 0 file sotto {roots}. Deposita materiale in <radice>/<fonte>/ e rilancia.")
        return

    texts = {r: kc.read_text(p) for r, (p, _, _) in files.items()}
    hashes = {r: hashlib.sha256(t.encode("utf-8", "replace")).hexdigest() for r, t in texts.items()}

    state = _load_json(state_path, {})
    prev_files = dict(state.get("files", {}))
    try:
        store = store or VectorStore(coll)
    except StoreConfigError as e:
        print(f"ERRORE configurazione: {e}")
        sys.exit(3)
    config_changed = bool(state) and (state.get("model") != model
                                      or state.get("sparse_model") != sparse_model
                                      or state.get("collection") != store.collection)
    try:
        present = store.exists()
    except StoreUnavailable as e:
        print(f"ERRORE: Qdrant non raggiungibile o non affidabile, corpus NON toccato: {e}")
        sys.exit(2)
    if present and not full and (not state or config_changed):
        print("Stato corpus assente o config cambiata, ma collection presente: ripristina lo stato "
              "da git oppure ricostruisci esplicitamente -> scripts/rag/ingest_corpus.py --full")
        sys.exit(1)
    needs_full = full or not present
    if needs_full and source:
        print("Indice corpus assente: serve prima un run pieno -> scripts/rag/ingest_corpus.py")
        sys.exit(1)

    if needs_full:
        changed, removed = sorted(hashes), []
    else:
        changed = sorted(r for r, h in hashes.items() if prev_files.get(r, {}).get("hash") != h)
        if source:
            prefixes = tuple(node.rel_root(croot / source) + "/" for croot in corpus_roots(node))
            removed = sorted(r for r in prev_files if r.startswith(prefixes) and r not in hashes)
        else:
            removed = sorted(r for r in prev_files if r not in hashes)

    if not changed and not removed:
        print(f"Corpus invariato: {len(hashes)} file, nessuna modifica ({time.time() - t0:.1f}s)")
        return

    de = sp = None
    if needs_full or changed:
        de, sp = EmbCls(), SparseCls()
        print(f"Denso: {de.model_name} (dim {de.dim}) | Sparse: {sp.model_name} -- "
              f"{len(changed)} file da (ri)embeddare, {len(removed)} da rimuovere")
    if needs_full:
        _dump_json(state_path, {})    # stato invalidato PRIMA del wipe
        if full:
            store.recreate(de.dim)    # wipe + create: SOLO qui, su --full esplicito
        else:
            store.create(de.dim)      # collection assente davvero: solo create, mai delete
        new_files: dict = {}
    else:
        if de is not None:
            store.ensure(de.dim)
        store.delete_paths(list(changed) + list(removed))
        new_files = {r: v for r, v in prev_files.items() if r not in set(changed) | set(removed)}

    def save():
        _dump_json(state_path, {"model": model, "sparse_model": sparse_model, "collection": store.collection,
                                "node_path": node.path, "files": new_files}, indent=2, sort_keys=True)

    manifests: dict[str, dict] = {}
    total_chunks = 0
    for i, relp in enumerate(changed, 1):
        p, src_name, src_dir = files[relp]
        if src_name not in manifests:
            manifests[src_name] = _load_json(src_dir / "manifest.json", {})
        meta = _meta_for(p, src_name, manifests[src_name])
        chunks = chunk_corpus_file(p, relp, texts[relp], meta, node.path, bid)
        if chunks:
            dv = de.encode([c["embed_text"] for c in chunks], kind="passage")
            sv = sp.encode([c["embed_text"] for c in chunks], kind="passage")
            pts = [store.point(_pid(relp, c["_n"]), dv[j], sv[j], {k: c[k] for k in PAYLOAD_KEYS})
                   for j, c in enumerate(chunks)]
            for b in range(0, len(pts), BATCH):
                store.upsert(pts[b:b + BATCH])
        total_chunks += len(chunks)
        new_files[relp] = {"hash": hashes[relp]}
        save()   # stato scritto DOPO ogni file: interruzione = si riprende dal successivo
        if i % 25 == 0 or i == len(changed):
            print(f"  {i}/{len(changed)} file ({total_chunks} chunk, {time.time() - t0:.0f}s)", flush=True)
    save()   # copre i run di sole RIMOZIONI
    print(f"Corpus {'PIENO' if needs_full else 'incrementale'}: {len(changed)} file (ri)embeddati, "
          f"{len(removed)} rimossi, {len(new_files)} file totali, {store.count()} chunk in "
          f"'{store.collection}' [{store.where}] ({time.time() - t0:.1f}s)")


def main(argv: list[str] | None = None) -> int:
    kc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Ingest del corpus (materiale grezzo) su Qdrant, collection separata.")
    kc.add_common_args(ap)
    ap.add_argument("--full", action="store_true", help="ricostruzione totale (ricrea la collection)")
    ap.add_argument("--source", default=None, help="limita a una fonte (nome sottocartella del corpus)")
    a = ap.parse_args(argv)
    try:
        root = kc.resolve_root(a.root)
        node = kc.current_node(root, override=a.node)
        kc.load_env(root, node.dir)
        if not os.environ.get("RAG_CORPUS_COLLECTION", "").strip():
            print("RAG_CORPUS_COLLECTION assente: nessun corpus configurato per questo brain (niente da fare).")
            return 0
        run(root, node, source=a.source, full=a.full)
    except kc.ConfigError as e:
        print(f"ERRORE configurazione: {e}")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
