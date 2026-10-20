"""ingest -- costruisce l'indice del brain su Qdrant (denso + BM25), per nodo.

Copiato da `kit/rag/ingest.py` (3.0, patch tri-stato di VMR HEAD 0efa9254). Cosa cambia:
  - le note vengono dall'ALLOW-LIST del profilo (kb_config.iter_notes), piu' le sessioni
    del journal del nodo (`workspace/journal/sessions`); niente `views/` di VMR
    (`is_view` resta nel payload, sempre False, per lo schema degli indici);
  - `node_path` e `brain_id` nel payload: in un albero la collection e' UNA per brain e i
    nodi si separano col filtro `node_path` (query.py filtra in ogni Prefetch);
  - stato PER NODO: `scripts/rag/index/vault_state[.<nodekey>].json` (+ nav/meta); nodekey
    = node_path con `/` -> `__`, radice = nessun suffisso;
  - `.env` caricato per path esplicito (kb_config.load_env), RAG_COLLECTION obbligatoria,
    embedded solo con RAG_ALLOW_EMBEDDED=1 (store.py);
  - default: il nodo della sessione (cwd / --node); `--all-nodes` per tutto l'albero.
    `--rebuild` su un sottoinsieme di nodi NON ricrea la collection (cancellerebbe gli
    altri nodi): cancella i punti dei nodi scelti e li ricarica; `--rebuild --all-nodes`
    (o brain a nodo singolo) = recreate two-phase come in VMR.

Invarianti ereditati: incrementale per content-hash; id deterministici
uuid5("vault:<relp>#<n>") con relp relativo alla RADICE del brain (unico nell'albero);
la collection non viene mai ricreata nel run normale; modello/collection cambiati ->
stop e chiedi --rebuild; Qdrant non affidabile -> exit 2 senza toccare nulla;
`--append-sessions` senza indice -> exit 1; stato assente ma collection viva ->
re-upsert del nodo senza wipe.

Exit: 0 ok | 1 indice assente/config cambiata (serve run pieno o --rebuild) |
      2 Qdrant non raggiungibile/non affidabile | 3 configurazione (.env) mancante.

Uso:
    python scripts/rag/ingest.py                      # incrementale, nodo corrente
    python scripts/rag/ingest.py --all-nodes          # incrementale, tutti i nodi
    python scripts/rag/ingest.py --rebuild            # ricrea (vedi sopra)
    python scripts/rag/ingest.py --append-sessions    # solo il journal sessioni del nodo
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))         # scripts/rag
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # scripts
import kb_config as kc  # noqa: E402
from store import StoreConfigError, StoreUnavailable, VectorStore  # noqa: E402

MAX_CHARS = 1500
OVERLAP = 150
BATCH = 256
FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
SESSIONS_PREFIX = "workspace/journal/sessions"
PAYLOAD_KEYS = ("stem", "path", "node_path", "brain_id", "title", "summary", "heading",
                "text", "is_view", "is_session")


# ----------------------------------------------------------------------------
# Chunking (identico a VMR)
# ----------------------------------------------------------------------------
def _front(text: str):
    """Parser leggero del frontmatter per il chunking (tollerante: niente gate qui)."""
    m = FM.match(text)
    meta, body = {}, text
    if m:
        body = text[m.end():]
        for ln in m.group(1).splitlines():
            if ":" in ln and not ln.lstrip().startswith("-"):
                k, _, v = ln.partition(":")
                meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, body


def _sections(body: str):
    lines = body.splitlines()
    secs, cur_h, buf = [], "", []
    for ln in lines:
        if re.match(r"^#{1,6}\s+", ln):
            if buf and "".join(buf).strip():
                secs.append((cur_h, "\n".join(buf).strip()))
            cur_h = re.sub(r"^#{1,6}\s+", "", ln).strip()
            buf = []
        else:
            buf.append(ln)
    if buf and "".join(buf).strip():
        secs.append((cur_h, "\n".join(buf).strip()))
    return secs or [("", body.strip())]


def _split_long(txt: str):
    if len(txt) <= MAX_CHARS:
        return [txt]
    parts, paras, cur = [], re.split(r"\n\s*\n", txt), ""
    for p in paras:
        if len(cur) + len(p) + 2 <= MAX_CHARS:
            cur = (cur + "\n\n" + p).strip()
        else:
            if cur:
                parts.append(cur)
            cur = (cur[-OVERLAP:] + "\n\n" + p).strip() if cur else p
            while len(cur) > MAX_CHARS:
                parts.append(cur[:MAX_CHARS])
                cur = cur[MAX_CHARS - OVERLAP:]
    if cur:
        parts.append(cur)
    return parts


def chunk_note(path: Path, relp: str, node_path: str, bid: str, text: str | None = None):
    if text is None:
        text = kc.read_text(path)
    if text.startswith("﻿"):
        text = text[1:]
    meta, body = _front(text)
    stem = path.stem
    title = meta.get("title", stem)
    summary = meta.get("summary", "")
    chunks = []
    for heading, sec in _sections(body):
        for piece in _split_long(sec):
            if not piece.strip():
                continue
            head = f"{title}" + (f" -- {heading}" if heading else "")
            embed_text = f"{head}\n{summary}\n{piece}".strip()
            chunks.append({
                "stem": stem, "path": relp, "node_path": node_path, "brain_id": bid,
                "title": title, "summary": summary, "heading": heading,
                "text": piece, "embed_text": embed_text,
                "is_view": False, "is_session": _is_session(relp, node_path),
            })
    return chunks


def _sessions_prefix(node_path: str) -> str:
    return SESSIONS_PREFIX if node_path == "." else f"{node_path}/{SESSIONS_PREFIX}"


def _is_session(relp: str, node_path: str) -> bool:
    return relp.startswith(_sessions_prefix(node_path) + "/")


def _pid(relp: str, n: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"vault:{relp}#{n}"))


def _payload(c):
    return {k: c[k] for k in PAYLOAD_KEYS}


# ----------------------------------------------------------------------------
# Stato per nodo
# ----------------------------------------------------------------------------
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


def _node_files(node: kc.Node, nodes: list[kc.Node]) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for p in kc.iter_notes(node, nodes):
        if p.stem == "_index":
            continue  # navigazione, non conoscenza: sporca il retrieval
        files[node.rel_root(p)] = p
    for p in kc.iter_sessions(node):
        files[node.rel_root(p)] = p
    return files


class NodeJob:
    """Tutto cio' che l'ingest sa di un nodo durante il run."""

    def __init__(self, root: Path, node: kc.Node, nodes: list[kc.Node], model: str, sparse: str, coll: str):
        self.node = node
        self.files = _node_files(node, nodes)
        self.texts = {r: kc.read_text(p) for r, p in self.files.items()}
        self.hashes = {r: hashlib.sha256(t.encode("utf-8", "replace")).hexdigest()
                       for r, t in self.texts.items()}
        self.state_path = kc.state_file(root, node.path, "vault_state")
        self.nav_path = kc.state_file(root, node.path, "nav")
        self.meta_path = kc.state_file(root, node.path, "meta")
        self.state = _load_json(self.state_path, {})
        self.prev_files = dict(self.state.get("files", {}))
        self.old_meta = _load_json(self.meta_path, {})
        self.config_changed = bool(self.state) and (
            self.state.get("model") != model or self.state.get("sparse_model") != sparse
            or self.state.get("collection") != coll)
        self.bootstrap = False
        self.changed: list[str] = []
        self.removed: list[str] = []
        self.new_files: dict = {}
        self.total_chunks = 0

    def save_state(self, model: str, sparse: str, coll: str) -> None:
        _dump_json(self.state_path, {"model": model, "sparse_model": sparse, "collection": coll,
                                     "node_path": self.node.path, "files": self.new_files},
                   indent=2, sort_keys=True)

    def rebuild_nav(self, files: dict, dim, store: VectorStore, model: str, sparse: str, t0: float) -> None:
        """nav + meta interi dallo stato: id deterministici, niente vettori, per-macchina."""
        nav = {}
        for relp, info in sorted(files.items()):
            stem = Path(relp).stem
            entry = nav.setdefault(stem, {"path": relp, "ids": []})
            entry["ids"].extend(_pid(relp, n) for n in range(info.get("chunks", 0)))
        _dump_json(self.nav_path, nav)
        meta = {"model": model, "sparse_model": sparse, "node_path": self.node.path,
                "collection": store.collection, "notes": len(files),
                "chunks": sum(v.get("chunks", 0) for v in files.values()),
                "where": store.where, "seconds": round(time.time() - t0, 1),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if dim:
            meta["dim"] = dim
        _dump_json(self.meta_path, meta, indent=2)


# ----------------------------------------------------------------------------
# Run
# ----------------------------------------------------------------------------
def select_nodes(root: Path, node_paths=None, all_nodes: bool = False, start: Path | None = None):
    nodes = kc.iter_nodes(root)
    if all_nodes:
        return nodes, list(nodes)
    if node_paths:
        return nodes, [kc.current_node(root, override=p, nodes=nodes) for p in node_paths]
    return nodes, [kc.current_node(root, start=start, nodes=nodes)]


def run(root: Path, node_paths=None, scope: str | None = None, rebuild: bool = False,
        all_nodes: bool = False, start: Path | None = None, store: VectorStore | None = None,
        embedders=None) -> None:
    """scope: None = tutto; 'sessions' = solo il journal sessioni dei nodi scelti.
    `store`/`embedders` iniettabili (test); embedders = (Embedder, SparseEmbedder) classi."""
    t0 = time.time()
    import embedder as emb  # noqa: PLC0415 -- dopo load_env (nomi modello dall'ambiente)
    model, sparse_model = emb.dense_model_name(), emb.sparse_model_name()
    EmbCls, SparseCls = embedders or (emb.Embedder, emb.SparseEmbedder)

    bid = kc.brain_id(root)
    coll = kc.require_env("RAG_COLLECTION", "nessun default: mettila in .env")
    nodes, targets = select_nodes(root, node_paths, all_nodes, start)
    try:
        store = store or VectorStore(coll)
    except StoreConfigError as e:
        print(f"ERRORE configurazione: {e}")
        sys.exit(3)
    jobs = [NodeJob(root, n, nodes, model, sparse_model, coll) for n in targets]

    # exists() tri-stato: timeout/rete/5xx/chiave -> StoreUnavailable: ci si ferma QUI,
    # prima di embeddare e senza toccare la collection.
    try:
        present = store.exists()
    except StoreUnavailable as e:
        print(f"ERRORE: Qdrant non raggiungibile o non affidabile, indice NON toccato: {e}")
        sys.exit(2)
    changed_cfg = [j.node.path for j in jobs if j.config_changed]
    if changed_cfg and not rebuild:
        print(f"Modello o collection cambiati rispetto allo stato (nodi: {', '.join(changed_cfg)}): "
              f"il wipe va chiesto esplicitamente -> scripts/rag/ingest.py --rebuild"
              + ("" if len(targets) == len(nodes) else " --all-nodes"))
        sys.exit(1)

    needs_full = rebuild or not present
    for j in jobs:
        j.bootstrap = not needs_full and not j.state   # collection viva, stato del nodo assente
    if scope and (needs_full or any(j.bootstrap for j in jobs)):
        print("Indice del nodo assente o config cambiata: serve prima un run pieno -> "
              "scripts/rag/ingest.py" + ("" if len(targets) == len(nodes) else " --all-nodes"))
        sys.exit(1)

    for j in jobs:
        if needs_full or j.bootstrap:
            j.changed, j.removed = sorted(j.hashes), []
            continue
        prefix = None if scope != "sessions" else _sessions_prefix(j.node.path) + "/"
        in_scope = (lambda r: True) if prefix is None else (lambda r, p=prefix: r.startswith(p))
        j.changed = sorted(r for r, h in j.hashes.items()
                           if in_scope(r) and j.prev_files.get(r, {}).get("hash") != h)
        j.removed = sorted(r for r in j.prev_files if in_scope(r) and r not in j.hashes)
        # sanity: stato pieno ma zero punti del nodo (rebuild morto a meta', wipe esterno)
        if not j.changed and not j.removed and j.prev_files and store.count([j.node.path]) == 0:
            print(f"ATTENZIONE: nodo '{j.node.path}' con stato pieno ma ZERO punti: ri-upsert completo.")
            j.bootstrap, j.changed = True, sorted(j.hashes)

    for j in jobs:
        n_sess = sum(1 for r in j.hashes if _is_session(r, j.node.path))
        print(f"[{j.node.path}] note: {len(j.hashes)} ({len(j.hashes) - n_sess} vault + {n_sess} sessioni)"
              f" | da (ri)embeddare: {len(j.changed)} | da rimuovere: {len(j.removed)}")

    if not any(j.changed or j.removed for j in jobs):
        for j in jobs:
            j.rebuild_nav(j.prev_files, j.old_meta.get("dim"), store, model, sparse_model, t0)
        print(f"Indice invariato: nessuna nota cambiata ({time.time() - t0:.1f}s)")
        return

    de = sp = None
    if any(j.changed for j in jobs):
        de, sp = EmbCls(), SparseCls()
        print(f"Denso: {de.model_name} (dim {de.dim}) | Sparse: {sp.model_name} -- locali, nessuna chiave")
    mode = ""
    if needs_full:
        mode = " | CREATE (collection assente)" if not present else (
            " | REBUILD two-phase (wipe)" if len(targets) == len(nodes) else " | REBUILD parziale (solo nodi scelti, niente wipe)")
    print(f"Qdrant: {store.where} | collezione: {store.collection}{mode}"
          + (" | bootstrap senza wipe (stato locale assente)" if any(j.bootstrap for j in jobs) else ""))

    if needs_full:
        # FASE 1: embedda tutto PRIMA di toccare la collection (resta interrogabile).
        all_pts = []
        done = 0
        total = sum(len(j.changed) for j in jobs)
        for j in jobs:
            for relp in j.changed:
                chunks = chunk_note(j.files[relp], relp, j.node.path, bid, j.texts[relp])
                if chunks:
                    dv = de.encode([c["embed_text"] for c in chunks], kind="passage")
                    sv = sp.encode([c["embed_text"] for c in chunks], kind="passage")
                    all_pts.extend(store.point(_pid(relp, i), dv[i], sv[i], _payload(c))
                                   for i, c in enumerate(chunks))
                j.total_chunks += len(chunks)
                j.new_files[relp] = {"hash": j.hashes[relp], "chunks": len(chunks)}
                done += 1
                if done % 50 == 0 or done == total:
                    print(f"  embed {done}/{total} file ({sum(x.total_chunks for x in jobs)} chunk, "
                          f"{time.time() - t0:.0f}s)", flush=True)
        # FASE 2: finestra breve. Stato invalidato PRIMA del wipe: un crash qui non lascia
        # mai "stato pieno + collection vuota" spacciato per sano.
        for j in jobs:
            j.state_path.unlink(missing_ok=True)
        if not present:
            store.create(de.dim)                 # collection assente davvero: solo create, mai delete
        elif len(targets) == len(nodes):
            store.recreate(de.dim)               # wipe + create: SOLO qui, su --rebuild esplicito
        else:
            store.ensure_indexes()
            for j in jobs:
                store.delete_node(j.node.path)   # rebuild parziale: via i punti di QUESTI nodi
        for b in range(0, len(all_pts), BATCH):
            store.upsert(all_pts[b:b + BATCH])
        for j in jobs:
            j.save_state(model, sparse_model, coll)
    else:
        store.ensure_indexes()   # idempotente; serve al filtro dei delete sul server
        for j in jobs:
            if j.removed:
                store.delete_paths(j.removed)
            j.new_files = {r: v for r, v in j.prev_files.items()
                           if r not in set(j.changed) | set(j.removed)}
            for i, relp in enumerate(j.changed, 1):
                chunks = chunk_note(j.files[relp], relp, j.node.path, bid, j.texts[relp])
                # delete per-file appena prima dell'upsert: serve solo per gli orfani
                # (nota accorciata); gli id deterministici sovrascrivono il resto.
                store.delete_paths([relp])
                if chunks:
                    dv = de.encode([c["embed_text"] for c in chunks], kind="passage")
                    sv = sp.encode([c["embed_text"] for c in chunks], kind="passage")
                    pts = [store.point(_pid(relp, n), dv[n], sv[n], _payload(c))
                           for n, c in enumerate(chunks)]
                    for b in range(0, len(pts), BATCH):
                        store.upsert(pts[b:b + BATCH])
                j.total_chunks += len(chunks)
                # stato scritto DOPO ogni file: un run interrotto riprende dal successivo
                j.new_files[relp] = {"hash": j.hashes[relp], "chunks": len(chunks)}
                j.save_state(model, sparse_model, coll)
                if i % 25 == 0 or i == len(j.changed):
                    print(f"  [{j.node.path}] {i}/{len(j.changed)} file ({j.total_chunks} chunk, "
                          f"{time.time() - t0:.0f}s)", flush=True)
            j.save_state(model, sparse_model, coll)   # copre i run di sole rimozioni

    dim = de.dim if de else None
    for j in jobs:
        j.rebuild_nav(j.new_files, dim or j.old_meta.get("dim"), store, model, sparse_model, t0)
    n_changed = sum(len(j.changed) for j in jobs)
    n_removed = sum(len(j.removed) for j in jobs)
    print(f"Indice {'RICOSTRUITO' if needs_full else 'aggiornato'}: {n_changed} file (ri)embeddati, "
          f"{n_removed} rimossi, {store.count()} chunk in '{store.collection}' [{store.where}] "
          f"({time.time() - t0:.1f}s)")


def main(argv: list[str] | None = None) -> int:
    kc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Ingest del vault su Qdrant (incrementale, per nodo).")
    kc.add_common_args(ap)
    ap.add_argument("--all-nodes", action="store_true", help="tutti i nodi dell'albero")
    ap.add_argument("--rebuild", action="store_true", help="ricostruzione esplicita (vedi docstring)")
    ap.add_argument("--append-sessions", action="store_true", help="solo workspace/journal/sessions")
    a = ap.parse_args(argv)
    try:
        root = kc.resolve_root(a.root)
        node = kc.current_node(root, override=a.node) if a.node else None
        kc.load_env(root, node.dir if node else kc.current_node(root).dir)
        run(root, node_paths=[a.node] if a.node else None,
            scope="sessions" if a.append_sessions else None, rebuild=a.rebuild, all_nodes=a.all_nodes)
    except kc.ConfigError as e:
        print(f"ERRORE configurazione: {e}")
        return 3
    except StoreUnavailable as e:
        print(f"ERRORE: Qdrant non raggiungibile o non affidabile, indice NON toccato: {e}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
