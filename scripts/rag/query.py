"""query -- retrieval ibrido dal brain (Qdrant denso+BM25, filtro per nodo, grafo dei wikilink).

Copiato da `scripts/rag/query.py` di VMR Brain (HEAD 0efa9254). Cosa cambia:
  - NIENTE LLM nel core: tolte decomposizione agentica (OpenRouter) e sintesi. I chunk
    grezzi li sintetizza l'agente che legge, citando gli stem;
  - filtro `node_path IN {nodo, antenati}` in OGNI Prefetch (store.hybrid) e nel dense;
    `graph_expand` segue i wikilink SOLO dentro lo stesso dominio (nav del nodo + antenati);
  - confidenza ONESTA e misurata: `alta` solo se il top dense supera la soglia
    (RAG_CONF_DENSE_MIN, default 0.85) E la stessa nota e' in testa sia al dense sia allo
    sparse; `media` se vale una sola delle due; `bassa` se ci sono hit ma nessuna;
    `non_presente` se zero hit. Calibrazione misurata con e5-large (TESTLOG): domande con
    risposta nel vault 0.90-0.91 di coseno, domande-trappola 0.79-0.81, domanda sul tema
    giusto ma senza il fatto 0.85 -> 0.85 separa i casi; e' una manopola, non una verita';
  - output `--json` (hit con stem/path/node_path/score/heading/text/via, segnali di
    confidenza); `.env` per path esplicito; RAG_COLLECTION obbligatoria.

Uso:
    python scripts/rag/query.py "domanda" [--k 8] [--mode hybrid|dense] [--no-expand]
                                [--collection brain|corpus] [--node P] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))         # scripts/rag
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # scripts
import kb_config as kc  # noqa: E402
from store import StoreConfigError, StoreUnavailable, VectorStore  # noqa: E402

ABSENT = "Non presente nel cervello."
WIKILINK = re.compile(r"\[\[([^\]|#]+)")
MAX_CTX_CHUNKS = 14

_E = None
_S = None


def _embedder():
    global _E
    if _E is None:
        from embedder import Embedder  # noqa: PLC0415
        _E = Embedder()
    return _E


def _sparse():
    global _S
    if _S is None:
        from embedder import SparseEmbedder  # noqa: PLC0415
        _S = SparseEmbedder()
    return _S


def _load_json(p: Path, default):
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return default
    return default


class Retriever:
    """Recupero ibrido su Qdrant filtrato per nodo + espansione sul grafo dei wikilink."""

    def __init__(self, root: Path, node: kc.Node, nodes: list[kc.Node] | None = None,
                 store: VectorStore | None = None, collection: str | None = None, corpus: bool = False):
        self.root = Path(root)
        self.node = node
        self.nodes = nodes or kc.iter_nodes(root)
        self.node_paths = node.visible_paths()
        self.corpus = corpus
        # nav/meta del nodo e degli antenati: e' il dominio del grafo
        self.nav: dict = {}
        dim = 0
        for np_ in reversed(self.node_paths):   # radice prima: il nodo sovrascrive a parita' di stem
            self.nav.update(_load_json(kc.state_file(root, np_, "nav"), {}))
            meta = _load_json(kc.state_file(root, np_, "meta"), {})
            dim = int(meta.get("dim", 0)) or dim
        self.dim = dim
        if store is None:
            coll = collection or (kc.require_env("RAG_CORPUS_COLLECTION") if corpus
                                  else kc.require_env("RAG_COLLECTION", "nessun default: mettila in .env"))
            store = VectorStore(coll, dim=dim or None)
        self.store = store
        self.stem2path = {s: info.get("path", "") for s, info in self.nav.items()}
        self._outlinks: dict[str, set[str]] = {}

    def count(self):
        return self.store.count(self.node_paths)

    def _hit(self, pt, via=""):
        p = dict(pt.payload or {})
        p["score"] = float(pt.score) if getattr(pt, "score", None) is not None else 0.0
        p["_i"] = pt.id
        if via:
            p["_via"] = via
        return p

    def hybrid(self, q, k=8, pool=30):
        dv = _embedder().encode([q], kind="query")[0]
        sv = _sparse().encode([q], kind="query")[0]
        return [self._hit(p) for p in self.store.hybrid(dv, sv, k=k, pool=pool, node_paths=self.node_paths)]

    def dense(self, q, k=8):
        dv = _embedder().encode([q], kind="query")[0]
        return [self._hit(p) for p in self.store.dense(dv, k=k, node_paths=self.node_paths)]

    def sparse(self, q, k=8):
        sv = _sparse().encode([q], kind="query")[0]
        return [self._hit(p) for p in self.store.sparse(sv, k=k, node_paths=self.node_paths)]

    def _note_outlinks(self, stem):
        if stem in self._outlinks:
            return self._outlinks[stem]
        out = set()
        p = self.stem2path.get(stem)
        if p:
            try:
                txt = kc.read_text(self.root / p)
                out = {m.strip() for m in WIKILINK.findall(txt)}
            except Exception:  # noqa: BLE001
                out = set()
        self._outlinks[stem] = out
        return out

    def graph_expand(self, hits, max_add=6, top_seed=5):
        """Segue i wikilink delle note migliori. nav contiene SOLO il nodo e i suoi
        antenati: l'espansione non puo' uscire dal dominio. Sul corpus non si espande."""
        if self.corpus:
            return hits
        seen = {h["stem"] for h in hits}
        add_ids = []
        for h in hits[:top_seed]:
            for lk in self._note_outlinks(h["stem"]):
                if lk in seen or lk not in self.nav:
                    continue
                seen.add(lk)
                ids = self.nav[lk].get("ids") or []
                if ids:
                    add_ids.append(ids[0])
                if len(add_ids) >= max_add:
                    break
            if len(add_ids) >= max_add:
                break
        if not add_ids:
            return hits
        added = [self._hit(p, via="grafo") for p in self.store.retrieve(add_ids)]
        allowed = set(self.node_paths)
        return hits + [h for h in added if h.get("node_path", ".") in allowed]

    def search(self, q, k=8, mode="hybrid", expand=True):
        hits = self.dense(q, k=k) if mode == "dense" else self.hybrid(q, k=k)
        if expand:
            hits = self.graph_expand(hits)
        return hits


def confidence(retr: Retriever, q: str, hits: list[dict]) -> tuple[str, dict]:
    """Confidenza misurata, non dichiarata. Vedi docstring del modulo."""
    if not hits:
        return "non_presente", {"dense_top": None, "sparse_top": None, "dense_score": None}
    thr = float(os.getenv("RAG_CONF_DENSE_MIN", "0.85"))
    try:
        d = retr.dense(q, k=1)
        s = retr.sparse(q, k=1)
    except Exception:  # noqa: BLE001 -- segnali indisponibili: non si finge
        return "bassa", {"dense_top": None, "sparse_top": None, "dense_score": None, "threshold": thr}
    d_top = d[0]["stem"] if d else None
    s_top = s[0]["stem"] if s else None
    d_score = d[0]["score"] if d else 0.0
    above = d_score >= thr
    same = d_top is not None and d_top == s_top
    level = "alta" if (above and same) else ("media" if (above or same) else "bassa")
    return level, {"dense_top": d_top, "sparse_top": s_top, "dense_score": round(d_score, 4),
                   "threshold": thr, "agree": same}


def answer(q: str, k: int = 8, mode: str = "hybrid", expand: bool = True,
           retriever: Retriever | None = None, root: Path | None = None, node: kc.Node | None = None,
           corpus: bool = False) -> dict:
    r = retriever or Retriever(root, node, corpus=corpus)
    hits = r.search(q, k=k, mode=mode, expand=expand)
    hits.sort(key=lambda h: -h["score"])
    hits = hits[:MAX_CTX_CHUNKS]
    files: list[str] = []
    for h in hits:
        if h["stem"] not in files:
            files.append(h["stem"])
    conf, signals = confidence(r, q, hits)
    return {"question": q, "node": r.node.path, "node_paths": r.node_paths,
            "answer": hits[0]["text"][:400] if hits else ABSENT,
            "files": files, "confidence": conf, "signals": signals, "hits": hits}


def hits_json(res: dict) -> dict:
    out = dict(res)
    out["hits"] = [{"stem": h.get("stem"), "path": h.get("path"), "node_path": h.get("node_path"),
                    "heading": h.get("heading", ""), "score": round(h.get("score", 0.0), 5),
                    "text": h.get("text", ""), "via": h.get("_via", ""), "is_session": h.get("is_session", False)}
                   for h in res["hits"]]
    return out


def print_human(res: dict) -> None:
    print(f"== {len(res['hits'])} chunk recuperati (nodo '{res['node']}', dominio {res['node_paths']}) "
          f"-- sintetizza tu citando gli stem ==")
    for h in res["hits"]:
        via = f" via:{h['_via']}" if h.get("_via") else ""
        print(f"\n--- [{h['stem']}] nodo={h.get('node_path', '?')} score={h['score']:.3f}{via} ---")
        print(h["text"].strip())
    print("-" * 70)
    print("FONTI:", ", ".join(res["files"][:8]) or "-")
    sig = res["signals"]
    print(f"CONFIDENZA: {res['confidence']} (dense_top={sig.get('dense_top')} sparse_top={sig.get('sparse_top')} "
          f"dense_score={sig.get('dense_score')} soglia={sig.get('threshold')})")


def main(argv: list[str] | None = None) -> int:
    kc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Retrieval ibrido dal brain (chunk grezzi, niente LLM).")
    kc.add_common_args(ap)
    ap.add_argument("question")
    ap.add_argument("--k", "-k", type=int, default=8)
    ap.add_argument("--mode", choices=["hybrid", "dense"], default="hybrid")
    ap.add_argument("--no-expand", action="store_true")
    ap.add_argument("--collection", choices=["brain", "corpus"], default="brain")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        root = kc.resolve_root(a.root)
        node = kc.current_node(root, override=a.node)
        kc.load_env(root, node.dir)
        res = answer(a.question, k=a.k, mode=a.mode, expand=not a.no_expand,
                     root=root, node=node, corpus=a.collection == "corpus")
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
    if a.json:
        print(json.dumps(hits_json(res), ensure_ascii=False, indent=2))
    else:
        print_human(res)
    return 0 if res["hits"] else 1


if __name__ == "__main__":
    sys.exit(main())
