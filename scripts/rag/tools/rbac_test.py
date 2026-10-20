"""rbac_test -- token JWT per collection (RBAC) su un Qdrant LOCALE di laboratorio.

Copiato da `kit/rag/rbac_test.py` (3.0). Cosa cambia: gli indici payload sono i quattro del
motore v3.1 (`path`, `node_path`, `is_view`, `is_session`); nessun default di porta o path di
macchina: URL e file della chiave arrivano SOLO dagli argomenti. Non tocca mai un cluster Cloud.

Cosa fa:
  1. con la chiave amministrativa crea `brain_master` e `brain_other` (dense 4-d COSINE +
     sparse `bm25` IDF + indici payload del motore) e inserisce 3 punti ciascuna con
     payload canary;
  2. genera JWT HS256 (segreto = API key) con `access` per-collection in sola lettura, piu'
     token di controllo (senza campo `access`, claims vuoti, `access: "r"` globale, firma
     sbagliata, scaduto);
  3. prova letture / scritture / delete con ogni token e registra esito, codice HTTP e messaggio
     del server; dopo ogni token i canary vengono ricontrollati e, se alterati, ricreati;
  4. verifica finale dei canary (3 + 3 punti, payload invariato).

Uso:
    python rbac_test.py --url http://127.0.0.1:<porta> --api-key-file <file> [--out risultati.md]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import sys
import time
import uuid
from importlib.metadata import version as pkg_version
from pathlib import Path

import httpx
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labserver  # noqa: E402

MASTER, OTHER = "brain_master", "brain_other"
DIM = 4
INDEXED_FIELDS = {
    "path": models.PayloadSchemaType.KEYWORD,
    "node_path": models.PayloadSchemaType.KEYWORD,
    "is_view": models.PayloadSchemaType.BOOL,
    "is_session": models.PayloadSchemaType.BOOL,
}
ROWS: list[dict] = []
RESTORES: list[tuple[str, list[str]]] = []


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def make_jwt(secret: str, payload: dict) -> str:
    head = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64url(sig)}"


def canary_points(tag: str) -> list[models.PointStruct]:
    pts = []
    for n in range(3):
        pid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{tag}:{n}"))
        dense = [0.1 * (n + 1), 0.2, 0.3 * (n + 1), 0.4 + 0.05 * n]
        sparse = models.SparseVector(indices=[n, n + 10, 42], values=[0.5, 0.25 * (n + 1), 1.0])
        payload = {"canary": tag, "path": f"notes/{tag.lower()}-{n}.md", "node_path": ".",
                   "is_view": False, "is_session": n == 2, "text": f"{tag} chunk {n}"}
        pts.append(models.PointStruct(id=pid, vector={"dense": dense, "bm25": sparse}, payload=payload))
    return pts


def safe_delete(client: QdrantClient, name: str, tries: int = 3, pause: float = 2.0) -> str:
    """delete_collection tollerante all'artefatto Qdrant/Windows (500 'failed to rename' subito
    dopo scritture: la collection sparisce dal registro, la cartella resta). Pausa, retry, verifica."""
    last = "?"
    for _ in range(tries):
        try:
            if not client.collection_exists(name):
                return "assente"
            time.sleep(pause)
            client.delete_collection(name)
            if not client.collection_exists(name):
                return "cancellata"
            last = "ancora presente"
        except UnexpectedResponse as e:
            if e.status_code != 500:
                raise
            last = "HTTP 500 (rename della cartella)"
        time.sleep(pause)
    return f"NON cancellata ({last})"


def create_engine_like(admin: QdrantClient, name: str) -> None:
    for attempt in range(2):
        try:
            safe_delete(admin, name)
            admin.create_collection(
                collection_name=name,
                vectors_config={"dense": models.VectorParams(size=DIM, distance=models.Distance.COSINE)},
                sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
            )
            break
        except UnexpectedResponse as e:
            # cartella orfana (artefatto Qdrant/Windows): si pulisce SOLO a server fermo e SOLO in lab
            if attempt == 1 or not (e.status_code == 400 and b"already exists" in e.content) or not labserver.has_control():
                raise
            print(f"  [artefatto Windows: cartella orfana di {name} -> pulizia a server fermo]")
            labserver.purge_orphans([name])
    for field, schema in INDEXED_FIELDS.items():
        admin.create_payload_index(name, field_name=field, field_schema=schema, wait=True)


def canaries_intact(admin: QdrantClient, name: str, tag: str) -> bool:
    if not admin.collection_exists(name):
        return False
    pts, _ = admin.scroll(name, limit=100, with_payload=True)
    return len(pts) == 3 and {p.payload.get("canary") for p in pts} == {tag}


def restore_canaries(admin: QdrantClient, after: str) -> None:
    fixed = []
    for name, tag in ((MASTER, "MASTER"), (OTHER, "OTHER")):
        if not canaries_intact(admin, name, tag):
            create_engine_like(admin, name)
            admin.upsert(name, points=canary_points(tag), wait=True)
            fixed.append(name)
    if fixed:
        RESTORES.append((after, fixed))
        print(f"  [canary alterati dal token {after}: ricreate {fixed}]")


def _server_error(e: UnexpectedResponse) -> str:
    try:
        return str(e.structured().get("status", {}).get("error") or e.content.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return e.content.decode("utf-8", "replace")[:200]


def _short(res) -> str:
    if res is None:
        return ""
    if isinstance(res, bool):
        return str(res)
    if isinstance(res, tuple) and len(res) == 2:
        return f"{len(res[0])} punti"
    if hasattr(res, "points"):
        return f"{len(res.points)} punti"
    if hasattr(res, "collections"):
        return "collections=" + ",".join(sorted(c.name for c in res.collections))
    if hasattr(res, "count"):
        return f"count={res.count}"
    if hasattr(res, "status"):
        return f"status={res.status}"
    return type(res).__name__


def attempt(token_label: str, op: str, coll: str, fn, expect: str) -> dict:
    row = {"token": token_label, "op": op, "collection": coll, "atteso": expect}
    try:
        res = fn()
        row.update(esito="OK", http=200, messaggio=_short(res))
    except UnexpectedResponse as e:
        row.update(esito="RIFIUTATO", http=e.status_code, messaggio=_server_error(e))
    except ResponseHandlingException as e:
        row.update(esito="ERRORE RETE", http="-", messaggio=repr(e.source))
    except Exception as e:  # noqa: BLE001
        row.update(esito=f"ECCEZIONE {type(e).__name__}", http="-", messaggio=str(e)[:200])
    row["verdetto"] = "come atteso" if row["esito"] == expect else "DIVERSO DALL'ATTESO"
    ROWS.append(row)
    return row


def client_for(url: str, token: str, bearer: bool = False) -> QdrantClient:
    if bearer:
        return QdrantClient(url=url, auth_token_provider=lambda: token, timeout=10, check_compatibility=False)
    return QdrantClient(url=url, api_key=token, timeout=10, check_compatibility=False)


def probe(url: str, label: str, token: str, own: str | None, other: str | None,
          writes_expected_ok: bool, bearer: bool = False) -> None:
    c = client_for(url, token, bearer=bearer)
    exp_w = "OK" if writes_expected_ok else "RIFIUTATO"
    dense_q = [0.1, 0.2, 0.3, 0.4]
    sparse_q = models.SparseVector(indices=[0, 10, 42], values=[0.5, 0.25, 1.0])
    flt = models.Filter(must=[models.FieldCondition(key="node_path", match=models.MatchAny(any=["."]))])

    def hybrid(coll):
        return c.query_points(
            coll,
            prefetch=[models.Prefetch(query=dense_q, using="dense", limit=5, filter=flt),
                      models.Prefetch(query=sparse_q, using="bm25", limit=5, filter=flt)],
            query=models.FusionQuery(fusion=models.Fusion.RRF), query_filter=flt, limit=3, with_payload=True)

    def new_point(tag):
        return models.PointStruct(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{tag}:extra:{label}")),
            vector={"dense": dense_q, "bm25": sparse_q}, payload={"canary": f"{tag}-EXTRA"})

    def delete_by_path(coll):
        return c.delete(coll, points_selector=models.FilterSelector(filter=models.Filter(must=[
            models.FieldCondition(key="path", match=models.MatchAny(any=["nessuno.md"]))])), wait=True)

    attempt(label, "get_collections", "*", c.get_collections, "OK")
    if own:
        attempt(label, "collection_exists", own, lambda: c.collection_exists(own), "OK")
        attempt(label, "count", own, lambda: c.count(own, exact=True), "OK")
        attempt(label, "scroll", own, lambda: c.scroll(own, limit=10, with_payload=True), "OK")
        attempt(label, "query_points dense", own, lambda: c.query_points(own, query=dense_q, using="dense", limit=3), "OK")
        attempt(label, "query_points ibrida RRF filtrata per nodo", own, lambda: hybrid(own), "OK")
        attempt(label, "upsert", own, lambda: c.upsert(own, points=[new_point(own)], wait=True), exp_w)
        attempt(label, "delete punti (filtro path)", own, lambda: delete_by_path(own), exp_w)
        attempt(label, "create_payload_index", own, lambda: c.create_payload_index(
            own, field_name="canary", field_schema=models.PayloadSchemaType.KEYWORD, wait=True), exp_w)
        attempt(label, "delete_collection", own, lambda: c.delete_collection(own), exp_w)
    if other:
        attempt(label, "collection_exists", other, lambda: c.collection_exists(other), "RIFIUTATO")
        attempt(label, "count", other, lambda: c.count(other, exact=True), "RIFIUTATO")
        attempt(label, "scroll", other, lambda: c.scroll(other, limit=10, with_payload=True), "RIFIUTATO")
        attempt(label, "query_points dense", other, lambda: c.query_points(other, query=dense_q, using="dense", limit=3), "RIFIUTATO")
        attempt(label, "upsert", other, lambda: c.upsert(other, points=[new_point(other)], wait=True), "RIFIUTATO")
        attempt(label, "delete punti (filtro path)", other, lambda: delete_by_path(other), "RIFIUTATO")
        attempt(label, "delete_collection", other, lambda: c.delete_collection(other), "RIFIUTATO")
    tmp = f"brain_tmp_{label.lower().replace('(', '_').replace(')', '')}"
    attempt(label, "create_collection", tmp, lambda: c.create_collection(
        tmp, vectors_config={"dense": models.VectorParams(size=DIM, distance=models.Distance.COSINE)}), exp_w)
    attempt(label, "delete_collection", tmp, lambda: c.delete_collection(tmp), exp_w)
    c.close()


def raw_http(url: str, token: str, label: str) -> None:
    for hdr_name, headers in (("api-key", {"api-key": token}), ("Bearer", {"Authorization": f"Bearer {token}"})):
        for path, method in ((f"/collections/{OTHER}", "GET"), (f"/collections/{MASTER}", "GET"),
                             (f"/collections/{OTHER}/exists", "GET"), (f"/collections/{MASTER}/exists", "GET"),
                             (f"/collections/{OTHER}/points", "PUT"), (f"/collections/{MASTER}", "DELETE")):
            body = None
            if method == "PUT":
                body = {"points": [{"id": str(uuid.uuid4()), "vector": {"dense": [0.1, 0.2, 0.3, 0.4]},
                                    "payload": {"canary": "RAW"}}]}
            r = httpx.request(method, url + path, headers=headers, json=body, timeout=10)
            ROWS.append({"token": f"{label} [{hdr_name}]", "op": f"HTTP {method} {path}", "collection": "-",
                         "atteso": "-", "esito": "OK" if r.status_code < 300 else "RIFIUTATO",
                         "http": r.status_code, "messaggio": r.text[:160].replace("\n", " "), "verdetto": "-"})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--api-key-file", required=True, help="file con la chiave amministrativa del server di lab")
    ap.add_argument("--out", default=None, help="file markdown con la tabella dei risultati")
    ap.add_argument("--qdrant-exe", default=None, help="con --config e --pid-file: pulizia delle cartelle orfane a server fermo")
    ap.add_argument("--config", default=None)
    ap.add_argument("--pid-file", default=None)
    a = ap.parse_args()
    if "cloud.qdrant.io" in a.url:
        print("Rifiuto: questo test gira SOLO su un server di laboratorio.")
        return 2
    key = Path(a.api_key_file).read_text(encoding="ascii").strip()
    labserver.configure(a.url, key, a.qdrant_exe, a.config, a.pid_file)

    admin = QdrantClient(url=a.url, api_key=key, timeout=10)
    info = httpx.get(a.url + "/", headers={"api-key": key}, timeout=10).json()
    print(f"Server: {a.url} | Qdrant {info.get('version')} | qdrant-client {pkg_version('qdrant-client')}")

    for name, tag in ((MASTER, "MASTER"), (OTHER, "OTHER")):
        create_engine_like(admin, name)
        admin.upsert(name, points=canary_points(tag), wait=True)
        print(f"  {name}: {admin.count(name, exact=True).count} punti canary '{tag}'")

    now = int(time.time())
    payloads = {
        "OTHER_r": {"access": [{"collection": OTHER, "access": "r"}]},
        "MASTER_r": {"access": [{"collection": MASTER, "access": "r"}]},
        "NO_ACCESS_FIELD": {"exp": now + 3600},
        "EMPTY_CLAIMS": {},
        "GLOBAL_r": {"access": "r"},
        "WRONG_SECRET": {"access": "m"},
        "EXPIRED": {"access": "m", "exp": now - 60},
    }
    tokens = {k: make_jwt(key + ("x" if k == "WRONG_SECRET" else ""), v) for k, v in payloads.items()}
    print("\nToken (payload JWT, HS256, segreto = API key del server):")
    for k, v in payloads.items():
        print(f"  {k:16s} {json.dumps(v)}" + ("  <- firmato con segreto diverso" if k == "WRONG_SECRET" else ""))

    probe(a.url, "OTHER_r", tokens["OTHER_r"], own=OTHER, other=MASTER, writes_expected_ok=False)
    restore_canaries(admin, "OTHER_r")
    probe(a.url, "OTHER_r(Bearer)", tokens["OTHER_r"], own=OTHER, other=MASTER, writes_expected_ok=False, bearer=True)
    restore_canaries(admin, "OTHER_r(Bearer)")
    probe(a.url, "MASTER_r", tokens["MASTER_r"], own=MASTER, other=OTHER, writes_expected_ok=False)
    restore_canaries(admin, "MASTER_r")
    probe(a.url, "NO_ACCESS_FIELD", tokens["NO_ACCESS_FIELD"], own=OTHER, other=None, writes_expected_ok=True)
    restore_canaries(admin, "NO_ACCESS_FIELD")
    probe(a.url, "EMPTY_CLAIMS", tokens["EMPTY_CLAIMS"], own=MASTER, other=None, writes_expected_ok=True)
    restore_canaries(admin, "EMPTY_CLAIMS")
    probe(a.url, "GLOBAL_r", tokens["GLOBAL_r"], own=OTHER, other=None, writes_expected_ok=False)
    restore_canaries(admin, "GLOBAL_r")
    for lab in ("WRONG_SECRET", "EXPIRED"):
        c = client_for(a.url, tokens[lab])
        attempt(lab, "get_collections", "*", c.get_collections, "RIFIUTATO")
        attempt(lab, "scroll", OTHER, lambda: c.scroll(OTHER, limit=3), "RIFIUTATO")
        c.close()
    raw_http(a.url, tokens["OTHER_r"], "OTHER_r")
    restore_canaries(admin, "raw_http OTHER_r")

    print("\nVerifica canary finale:")
    canary_ok = True
    for name, tag in ((MASTER, "MASTER"), (OTHER, "OTHER")):
        ok = canaries_intact(admin, name, tag)
        canary_ok = canary_ok and ok
        n = admin.count(name, exact=True).count if admin.collection_exists(name) else "ASSENTE"
        print(f"  {name}: {n} punti -> {'OK' if ok else 'ALTERATO'}")
    print("\nPulizia finale (collection di prova):")
    names = [MASTER, OTHER] + sorted(c.name for c in admin.get_collections().collections if c.name.startswith("brain_tmp_"))
    for name in names:
        print(f"  {name}: {safe_delete(admin, name)}")
    if labserver.has_control():
        # anche le "assenti": una delete finita in 500 le toglie dal registro ma lascia la cartella,
        # che risorgerebbe al prossimo riavvio del server
        labserver.purge_orphans(names + [f"brain_tmp_{t.lower().replace('(', '_').replace(')', '')}" for t in
                                         ("OTHER_r", "OTHER_r(Bearer)", "MASTER_r", "NO_ACCESS_FIELD", "EMPTY_CLAIMS", "GLOBAL_r")])

    hdr = "| token | operazione | collection | atteso | esito | HTTP | messaggio server | verdetto |"
    lines = [hdr, "|---|---|---|---|---|---|---|---|"]
    for r in ROWS:
        msg = str(r["messaggio"]).replace("|", "\\|").replace("\n", " ")[:150]
        lines.append(f"| {r['token']} | {r['op']} | {r['collection']} | {r['atteso']} | {r['esito']} | "
                     f"{r['http']} | {msg} | {r['verdetto']} |")
    table = "\n".join(lines)
    print("\n" + table)
    n_div = sum(1 for r in ROWS if r["verdetto"] == "DIVERSO DALL'ATTESO")
    summary = (f"Righe: {len(ROWS)} | diverse dall'atteso: {n_div} | canary finali intatti: {canary_ok} | "
               f"ripristini necessari: {RESTORES or 'nessuno'}")
    print("\n" + summary)
    if a.out:
        Path(a.out).write_text(
            f"# RBAC test -- {a.url} -- Qdrant {info.get('version')} -- qdrant-client {pkg_version('qdrant-client')}\n\nToken:\n\n"
            + "\n".join(f"- `{k}`: `{json.dumps(v)}`" + (" (segreto sbagliato)" if k == "WRONG_SECRET" else "")
                        for k, v in payloads.items())
            + f"\n\n{table}\n\n{summary}\n", encoding="utf-8")
    admin.close()
    return 0 if canary_ok else 1


if __name__ == "__main__":
    sys.exit(main())
