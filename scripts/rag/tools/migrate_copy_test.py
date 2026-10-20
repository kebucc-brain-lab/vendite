"""migrate_copy_test -- copia una collection in un'altra SENZA re-embedding (scroll + upsert).

Copiato da `kit/rag/migrate_copy_test.py` (3.0): unico cambiamento, nessun default di porta o
path di macchina (URL e chiave SOLO dagli argomenti). Sorgente e destinazione sullo STESSO
server di laboratorio; la destinazione viene creata con `create` (mai delete+create di una
collection viva: se esiste gia' ci si ferma, `--force-lab` per ricrearla SOLO in lab).

Preserva: id, payload (incluso `node_path`), vettore denso `dense`, vettore sparse `bm25`, indici
payload. Verifica: conteggi, schema indici, confronto punto per punto (payload uguale, denso entro
1e-6, sparse indici uguali e valori entro 1e-6). E' il ciclo di `estrai_nodo` (copia dei punti di
un nodo su una nuova collection) ridotto all'osso.

Uso:
    python migrate_copy_test.py --url http://127.0.0.1:<porta> --api-key-file <file> --src <coll> --dst <coll>_copy
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from qdrant_client import QdrantClient, models

BATCH = 2   # piccolo apposta: esercita la paginazione dello scroll (offset)


def _dim_of(client: QdrantClient, coll: str) -> int:
    params = client.get_collection(coll).config.params.vectors
    if isinstance(params, dict):
        return int(params["dense"].size)
    return int(params.size)


def copy_collection(c: QdrantClient, src: str, dst: str, force: bool, node_path: str | None = None) -> int:
    info = c.get_collection(src)
    if c.collection_exists(dst):
        if not force:
            print(f"Destinazione '{dst}' gia' presente: mi fermo (usa --force-lab per ricrearla, SOLO in lab).")
            sys.exit(1)
        c.delete_collection(dst)
    c.create_collection(
        dst,
        vectors_config={"dense": models.VectorParams(
            size=_dim_of(c, src), distance=info.config.params.vectors["dense"].distance)},
        sparse_vectors_config=info.config.params.sparse_vectors,
    )
    for field, idx in (info.payload_schema or {}).items():
        schema = idx.params if idx.params is not None else idx.data_type
        c.create_payload_index(dst, field_name=field, field_schema=schema, wait=True)
    flt = None
    if node_path is not None:
        flt = models.Filter(must=[models.FieldCondition(key="node_path", match=models.MatchAny(any=[node_path]))])
    total, offset = 0, None
    while True:
        points, offset = c.scroll(src, with_vectors=True, with_payload=True, limit=BATCH, offset=offset, scroll_filter=flt)
        if not points:
            break
        batch = []
        for p in points:
            vec = {}
            for name, v in (p.vector or {}).items():
                if hasattr(v, "indices"):
                    vec[name] = models.SparseVector(indices=list(v.indices), values=list(v.values))
                elif isinstance(v, dict) and "indices" in v:
                    vec[name] = models.SparseVector(indices=v["indices"], values=v["values"])
                else:
                    vec[name] = list(v)
            batch.append(models.PointStruct(id=p.id, vector=vec, payload=p.payload))
        c.upsert(dst, points=batch, wait=True)
        total += len(batch)
        print(f"  [{src} -> {dst}] copiati {total} punti", flush=True)
        if offset is None:
            break
    return total


def dump(c: QdrantClient, coll: str, node_path: str | None = None) -> dict:
    out, offset = {}, None
    flt = None
    if node_path is not None:
        flt = models.Filter(must=[models.FieldCondition(key="node_path", match=models.MatchAny(any=[node_path]))])
    while True:
        pts, offset = c.scroll(coll, with_vectors=True, with_payload=True, limit=100, offset=offset, scroll_filter=flt)
        for p in pts:
            out[str(p.id)] = (p.vector, p.payload)
        if offset is None or not pts:
            break
    return out


def compare(c: QdrantClient, src: str, dst: str, tol: float = 1e-6, node_path: str | None = None) -> bool:
    a, b = dump(c, src, node_path), dump(c, dst)
    n_dst = c.count(dst, exact=True).count
    ok = True
    print(f"\nConteggi: {src}{' [' + node_path + ']' if node_path else ''}={len(a)} {dst}={n_dst} -> "
          f"{'OK' if len(a) == n_dst == len(b) else 'DIVERSI'}")
    ok &= (len(a) == n_dst == len(b))
    missing = set(a) - set(b)
    extra = set(b) - set(a)
    print(f"Id mancanti in {dst}: {sorted(missing)} | id in piu': {sorted(extra)}")
    ok &= not missing and not extra
    max_dense = max_sparse = 0.0
    for pid in sorted(set(a) & set(b)):
        va, pa = a[pid]
        vb, pb = b[pid]
        same_payload = pa == pb
        da, db = va["dense"], vb["dense"]
        d_dense = max(abs(x - y) for x, y in zip(da, db)) if len(da) == len(db) else float("inf")
        sa, sb = va["bm25"], vb["bm25"]
        same_idx = list(sa.indices) == list(sb.indices)
        d_sparse = (max(abs(x - y) for x, y in zip(sa.values, sb.values))
                    if same_idx and len(sa.values) == len(sb.values) else float("inf"))
        max_dense, max_sparse = max(max_dense, d_dense), max(max_sparse, d_sparse)
        row_ok = same_payload and d_dense <= tol and d_sparse <= tol
        ok &= row_ok
        print(f"  {pid}: payload {'=' if same_payload else '!='} | dense max|d|={d_dense:.2e} "
              f"| bm25 idx {'=' if same_idx else '!='} max|d|={d_sparse:.2e} -> {'OK' if row_ok else 'DIFF'}")
    sa_schema = {k: str(v.data_type) for k, v in (c.get_collection(src).payload_schema or {}).items()}
    sb_schema = {k: str(v.data_type) for k, v in (c.get_collection(dst).payload_schema or {}).items()}
    print(f"Indici payload {src}: {sa_schema}")
    print(f"Indici payload {dst}: {sb_schema} -> {'OK' if sa_schema == sb_schema else 'DIVERSI'}")
    ok &= sa_schema == sb_schema
    cfg_a, cfg_b = c.get_collection(src).config.params, c.get_collection(dst).config.params
    same_cfg = (cfg_a.vectors["dense"].size == cfg_b.vectors["dense"].size
                and cfg_a.vectors["dense"].distance == cfg_b.vectors["dense"].distance
                and set(cfg_a.sparse_vectors or {}) == set(cfg_b.sparse_vectors or {})
                and all((cfg_a.sparse_vectors[k].modifier == cfg_b.sparse_vectors[k].modifier)
                        for k in (cfg_a.sparse_vectors or {})))
    print(f"Config vettori (dim/distanza/sparse+modifier) uguali: {same_cfg}")
    ok &= same_cfg
    print(f"\nMax scostamento denso: {max_dense:.2e} | sparse: {max_sparse:.2e} | tolleranza {tol:.0e}")
    print(f"ESITO: {'COPIA FEDELE' if ok else 'DIFFERENZE'}")
    return bool(ok)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--api-key-file", required=True)
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    ap.add_argument("--node-path", default=None, help="copia SOLO i punti di questo nodo (come estrai_nodo)")
    ap.add_argument("--force-lab", action="store_true", help="ricrea la destinazione se esiste (SOLO lab)")
    ap.add_argument("--drop-dst", action="store_true", help="cancella la destinazione a fine confronto (SOLO lab)")
    a = ap.parse_args()
    if "cloud.qdrant.io" in a.url:
        print("Rifiuto: questo test gira SOLO su un server di laboratorio.")
        return 2
    key = Path(a.api_key_file).read_text(encoding="ascii").strip()
    c = QdrantClient(url=a.url, api_key=key, timeout=30)
    n = copy_collection(c, a.src, a.dst, force=a.force_lab, node_path=a.node_path)
    print(f"Copiati {n} punti con scroll+upsert, nessun embedding ricalcolato.")
    ok = compare(c, a.src, a.dst, node_path=a.node_path)
    if a.drop_dst:
        # artefatto Qdrant/Windows: 500 "failed to rename" subito dopo gli upsert -> pausa e retry
        import time  # noqa: PLC0415
        from qdrant_client.http.exceptions import UnexpectedResponse  # noqa: PLC0415
        esito = "NON cancellata"
        for _ in range(3):
            try:
                time.sleep(2)
                if c.collection_exists(a.dst):
                    c.delete_collection(a.dst)
                if not c.collection_exists(a.dst):
                    esito = "cancellata"
                    break
            except UnexpectedResponse as e:
                if e.status_code != 500:
                    raise
        print(f"Destinazione {a.dst}: {esito}.")
    c.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
