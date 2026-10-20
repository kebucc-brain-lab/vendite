"""brain -- entry point unico per interrogare il brain.

Forkato da `scripts/brain.py` di VMR Brain (HEAD 0efa9254) + la logica del lookup
esatto di `scripts/codex/brain_read.py`. Cosa cambia:
  - `ask` ha tre modi: `--exact` (llms.txt + grep, stdlib, non apre Qdrant), `--semantic`
    (rag/query.py: chunk grezzi) e il default AUTO (esatto; se zero match, semantico);
    `--json` su entrambi; `--k`; `--node` per interrogare un nodo preciso;
  - il dominio dell'esatto e' il nodo + gli antenati (stessa regola del RAG);
  - `--llm`, `--no-agent` rimossi: niente LLM nel core (sintetizza l'agente);
  - `status` (identita', nodi, profilo, variabili presenti SENZA valori, git, llms.txt,
    meta dell'indice; con `--remote` prova il server Qdrant del .env: raggiungibile |
    non raggiungibile + motivo senza segreti, e se la collection esiste; exit 0/2) e
    `index` (stato dell'indice per nodo, senza segreti);
  - nessun dato sensibile in argv per uso federato: la federazione passa da
    `federation_adapter.py` (stdin), che riusa `exact_hits()` di questo modulo.

    python scripts/brain.py ask "nome o numero esatto" --exact
    python scripts/brain.py ask "domanda fuzzy" --semantic --k 8
    python scripts/brain.py ask "termine" --json
    python scripts/brain.py status [--remote] | index
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_config as kc  # noqa: E402

EXACT_MAX = 40
MAX_LINE = 400


# ----------------------------------------------------------------------------
# Lookup esatto (stdlib; usato anche da federation_adapter.py)
# ----------------------------------------------------------------------------
def exact_hits(term: str, root: Path, node: kc.Node, nodes: list[kc.Node] | None = None,
               limit: int = EXACT_MAX) -> dict:
    """Sottostringa case-insensitive, niente regex: i termini esatti sono nomi propri, ID,
    path. Ordine: llms.txt del nodo e degli antenati (e' l'indice), poi il corpo delle note
    compilate del dominio. Ritorna {"hits": [...], "total": n, "truncated": bool}."""
    nodes = nodes or kc.iter_nodes(root)
    by_path = {n.path: n for n in nodes}
    needle = term.lower()
    hits: list[dict] = []
    total = 0
    domain = [by_path[p] for p in node.visible_paths() if p in by_path]
    sources: list[tuple[str, Path, str]] = []
    for n in domain:
        llms = n.dir / "llms.txt"
        if llms.is_file():
            sources.append((n.rel_root(llms), llms, n.path))
    for n in domain:
        for note in kc.iter_notes(n, nodes):
            sources.append((n.rel_root(note), note, n.path))
    for rel, path, np_ in sources:
        try:
            lines = kc.read_text(path).splitlines()
        except OSError:
            continue
        for i, ln in enumerate(lines, 1):
            if needle in ln.lower():
                total += 1
                if len(hits) < limit:
                    text = ln.strip()
                    if len(text) > MAX_LINE:
                        text = text[:MAX_LINE - 1] + "…"
                    hits.append({"source": rel, "line": i, "text": text, "node_path": np_,
                                 "kind": "llms" if path.name == "llms.txt" else "note"})
    return {"term": term, "node": node.path, "hits": hits, "total": total, "truncated": total > len(hits)}


def print_exact(res: dict) -> None:
    llms = [h for h in res["hits"] if h["kind"] == "llms"]
    notes = [h for h in res["hits"] if h["kind"] == "note"]
    if llms:
        print(f"== llms.txt ({len(llms)} righe) ==")
        for h in llms:
            print(f"  {h['source']}:{h['line']}: {h['text']}")
    if notes:
        print(f"== note compilate ({len(notes)} righe in {len({h['source'] for h in notes})} note) ==")
        for h in notes:
            print(f"  {h['source']}:{h['line']}: {h['text']}")
    if res["truncated"]:
        print(f"  ... e altre {res['total'] - len(res['hits'])} righe (alza --limit)")
    if not res["hits"]:
        print(f"Nessun match esatto per {res['term']!r} nel dominio del nodo '{res['node']}'.")


# ----------------------------------------------------------------------------
# Semantico (import pigro: senza questo ramo non serve il venv)
# ----------------------------------------------------------------------------
def semantic(question: str, root: Path, node: kc.Node, k: int, mode: str, expand: bool,
             corpus: bool, as_json: bool) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "rag"))
    import query as q  # noqa: PLC0415
    from store import StoreConfigError, StoreUnavailable  # noqa: PLC0415
    try:
        res = q.answer(question, k=k, mode=mode, expand=expand, root=root, node=node, corpus=corpus)
    except (kc.ConfigError, StoreConfigError) as e:
        print(f"ERRORE configurazione: {e}")
        return 3
    except StoreUnavailable as e:
        print(f"ERRORE: Qdrant non raggiungibile: {e}")
        return 2
    except ValueError as e:
        if "not found" in str(e).lower():
            print("Collection assente: esegui prima l'ingest (scripts/rag/ingest.py).")
            return 1
        raise
    if as_json:
        print(json.dumps(q.hits_json(res), ensure_ascii=False, indent=2))
    else:
        q.print_human(res)
    return 0 if res["hits"] else 1


def _semantic_available() -> tuple[bool, str]:
    try:
        import fastembed  # noqa: F401,PLC0415
        import qdrant_client  # noqa: F401,PLC0415
    except Exception as e:  # noqa: BLE001
        return False, f"venv senza fastembed/qdrant-client ({e.__class__.__name__}): usa il python del venv"
    if not os.environ.get("QDRANT_URL", "").strip() and os.environ.get("RAG_ALLOW_EMBEDDED", "") != "1":
        return False, "QDRANT_URL assente nel .env (embedded solo con RAG_ALLOW_EMBEDDED=1)"
    if not os.environ.get("RAG_COLLECTION", "").strip():
        return False, "RAG_COLLECTION assente nel .env"
    return True, ""


# ----------------------------------------------------------------------------
# status / index
# ----------------------------------------------------------------------------
def _git(root: Path, *args: str) -> str:
    try:
        p = subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args], capture_output=True,
                           text=True, timeout=10, stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
        return p.stdout.strip() if p.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _redact(text: str) -> str:
    """Mai segreti a schermo: se il valore della chiave comparisse in un messaggio, via."""
    for name in ("QDRANT_API_KEY",):
        v = os.environ.get(name, "").strip()
        if v and v in text:
            text = text.replace(v, "***")
    return text


def remote_check() -> tuple[int, dict]:
    """`status --remote`: prova il server Qdrant del .env con lo store tri-stato.
    Ritorna (exit, info): 0 raggiungibile, 2 non raggiungibile/non configurato."""
    sys.path.insert(0, str(Path(__file__).resolve().parent / "rag"))
    try:
        from store import StoreConfigError, StoreUnavailable, VectorStore  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        return 2, {"reachable": False, "reason": f"venv senza qdrant-client ({e.__class__.__name__}): usa il python del venv"}
    coll = os.environ.get("RAG_COLLECTION", "").strip()
    if not coll:
        return 2, {"reachable": False, "reason": "RAG_COLLECTION assente nel .env"}
    try:
        vs = VectorStore(coll)
    except StoreConfigError as e:
        return 2, {"reachable": False, "reason": _redact(str(e))}
    try:
        present = vs.exists()
    except StoreUnavailable as e:
        return 2, {"reachable": False, "where": vs.where, "collection": coll, "reason": _redact(str(e))}
    info = {"reachable": True, "where": vs.where, "collection": coll, "collection_exists": bool(present)}
    corpus = os.environ.get("RAG_CORPUS_COLLECTION", "").strip()
    if corpus:
        try:
            info["corpus_collection"] = corpus
            info["corpus_exists"] = bool(VectorStore(corpus).exists())
        except (StoreUnavailable, StoreConfigError) as e:
            info["corpus_exists"] = None
            info["corpus_reason"] = _redact(str(e))
    return 0, info


def status(root: Path, node: kc.Node, as_json: bool, remote: bool = False) -> int:
    nodes = kc.iter_nodes(root)
    env_names = ["BRAIN_ID", "BRAIN_ROOT", "RAG_COLLECTION", "RAG_CORPUS_COLLECTION", "QDRANT_URL",
                 "QDRANT_API_KEY", "RAG_EMBED_MODEL", "RAG_BM25_LANG", "FASTEMBED_CACHE_PATH",
                 "BRAIN_MEMORY_DIR", "BRAIN_GUARD_HOME", "BRAIN_NODE"]
    info = {
        "root": str(root), "brain_id": kc.brain_id(root, strict=False) or None, "name": kc.brain_name(root),
        "node": node.path, "nodes": [n.path for n in nodes],
        "profile": {"source": str(node.profile.source) if node.profile.source else "default (brain.profile.yaml assente)",
                    "kind": node.profile.kind, "folders": node.profile.folders, "raw_dirs": node.profile.raw_dirs,
                    "ops_dirs": node.profile.ops_dirs, "corpus_roots": node.profile.corpus_roots},
        "env_present": {k: bool(os.environ.get(k, "").strip()) for k in env_names},   # mai i valori
        "env_files": [str(p) for p in ((root / ".env"), (node.dir / ".env")) if p.is_file()],
        "python": sys.executable,
        "git": {"head": _git(root, "rev-parse", "--short", "HEAD") or None,
                "branch": _git(root, "rev-parse", "--abbrev-ref", "HEAD") or None,
                "dirty": bool(_git(root, "status", "--porcelain"))},
        "llms_txt": {}, "index": {}, "memory_dir": str(kc.memory_dir(root, node)),
    }
    for n in nodes:
        llms = n.dir / "llms.txt"
        info["llms_txt"][n.path] = {"exists": llms.is_file(),
                                    "bytes": llms.stat().st_size if llms.is_file() else 0}
        meta = _load_json(kc.state_file(root, n.path, "meta"))
        info["index"][n.path] = {k: meta.get(k) for k in ("notes", "chunks", "model", "dim", "where", "updated_at")}
    rc = 0
    if remote:
        rc, info["remote"] = remote_check()
    if as_json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return rc
    print(f"brain: {info['name']} ({info['brain_id'] or 'BRAIN_ID ASSENTE'})  radice: {root}")
    print(f"nodo corrente: {node.path}  nodi: {', '.join(info['nodes'])}")
    print(f"profilo: {info['profile']['source']}  kind={node.profile.kind}  folders={node.profile.folders}")
    print(f"env presenti (solo nomi): {', '.join(k for k, v in info['env_present'].items() if v) or '-'}")
    print(f"env assenti: {', '.join(k for k, v in info['env_present'].items() if not v) or '-'}")
    g = info["git"]
    print(f"git: {g['branch'] or '-'} @ {g['head'] or '-'}{' (modifiche non committate)' if g['dirty'] else ''}")
    for np_, l in info["llms_txt"].items():
        idx = info["index"][np_]
        print(f"[{np_}] llms.txt: {'ok ' + str(l['bytes']) + ' B' if l['exists'] else 'ASSENTE'} | "
              f"indice: {idx['chunks'] if idx['chunks'] is not None else '-'} chunk, {idx['notes'] or '-'} note, "
              f"modello {idx['model'] or '-'}, {idx['where'] or 'mai ingerito'}")
    print(f"memoria: {info['memory_dir']}")
    if remote:
        r = info["remote"]
        if r["reachable"]:
            line = f"remoto: raggiungibile ({r['where']}) | collection {r['collection']}: " \
                   f"{'presente' if r['collection_exists'] else 'ASSENTE (serve l\'ingest)'}"
            if "corpus_collection" in r:
                ce = r.get("corpus_exists")
                line += f" | corpus {r['corpus_collection']}: " + ("presente" if ce else ("ASSENTE" if ce is False else f"n/d ({r.get('corpus_reason')})"))
            print(line)
        else:
            print(f"remoto: non raggiungibile ({r['reason']})")
    return rc


def index_info(root: Path, as_json: bool) -> int:
    nodes = kc.iter_nodes(root)
    out = {}
    for n in nodes:
        st = _load_json(kc.state_file(root, n.path, "vault_state"))
        meta = _load_json(kc.state_file(root, n.path, "meta"))
        cs = _load_json(kc.state_file(root, n.path, "corpus_state"))
        out[n.path] = {
            "state_files": len(st.get("files", {})), "state_model": st.get("model"), "state_collection": st.get("collection"),
            "meta": {k: meta.get(k) for k in ("notes", "chunks", "model", "dim", "where", "updated_at")},
            "corpus_files": len(cs.get("files", {})), "corpus_collection": cs.get("collection"),
        }
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    for np_, i in out.items():
        m = i["meta"]
        print(f"[{np_}] stato: {i['state_files']} file, modello {i['state_model'] or '-'}, collection "
              f"{i['state_collection'] or '-'} | meta: {m['chunks'] or '-'} chunk, {m['where'] or 'mai ingerito'}, "
              f"aggiornato {m['updated_at'] or '-'} | corpus: {i['corpus_files']} file"
              f"{(' in ' + i['corpus_collection']) if i['corpus_collection'] else ''}")
    return 0


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    kc.utf8_stdout()
    ap = argparse.ArgumentParser(prog="brain", description="Interroga il brain (esatto, semantico, stato).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ask", help="cerca nel brain")
    kc.add_common_args(a)
    a.add_argument("question")
    g = a.add_mutually_exclusive_group()
    g.add_argument("--exact", action="store_true", help="solo lookup esatto (llms.txt + note): non apre Qdrant")
    g.add_argument("--semantic", action="store_true", help="solo retrieval semantico (Qdrant, chunk grezzi)")
    a.add_argument("--json", action="store_true")
    a.add_argument("--k", "-k", type=int, default=8, help="chunk da recuperare (default 8)")
    a.add_argument("--limit", type=int, default=EXACT_MAX, help=f"righe massime per l'esatto (default {EXACT_MAX})")
    a.add_argument("--mode", choices=["hybrid", "dense"], default="hybrid")
    a.add_argument("--no-expand", action="store_true", help="salta l'espansione sul grafo dei wikilink")
    a.add_argument("--collection", choices=["brain", "corpus"], default="brain")

    s = sub.add_parser("status", help="identita', nodi, profilo, variabili (solo nomi), git, indice")
    kc.add_common_args(s)
    s.add_argument("--json", action="store_true")
    s.add_argument("--remote", action="store_true",
                   help="prova il server Qdrant del .env (collection_exists tri-stato): raggiungibile | non raggiungibile; exit 0/2")
    i = sub.add_parser("index", help="stato dell'indice per nodo, senza segreti")
    kc.add_common_args(i)
    i.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        root = kc.resolve_root(args.root)
        node = kc.current_node(root, override=args.node)
        kc.load_env(root, node.dir)
        if args.cmd == "status":
            return status(root, node, args.json, remote=args.remote)
        if args.cmd == "index":
            return index_info(root, args.json)
        if args.limit < 1 or args.k < 1:
            ap.error("--limit e --k devono essere positivi")
        if not args.semantic:
            res = exact_hits(args.question, root, node, limit=args.limit)
            if args.exact or res["hits"]:
                if args.json:
                    print(json.dumps(res, ensure_ascii=False, indent=2))
                else:
                    print_exact(res)
                return 0 if res["hits"] else 1
            ok, why = _semantic_available()
            if not ok:
                if args.json:
                    print(json.dumps({**res, "semantic": {"available": False, "reason": why}}, ensure_ascii=False, indent=2))
                else:
                    print_exact(res)
                    print(f"Semantico non disponibile: {why}")
                return 1
            if not args.json:
                print(f"Nessun match esatto per {args.question!r}: passo al semantico.")
        return semantic(args.question, root, node, k=args.k, mode=args.mode, expand=not args.no_expand,
                        corpus=args.collection == "corpus", as_json=args.json)
    except kc.ConfigError as e:
        print(f"ERRORE configurazione: {e}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
