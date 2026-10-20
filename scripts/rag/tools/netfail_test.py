"""netfail_test -- errori di rete / chiave / permessi contro un server Qdrant REALE di laboratorio:
il motore v3.1 non deve MAI arrivare a delete_collection / create_collection.

Riscritto da `kit/rag/netfail_test.py` (3.0): quello confrontava la patch con l'originale di VMR
(lo fa gia' il rapporto REPORT-store del laboratorio); questo prova SOLO il motore del kit, con un brain finto
(engine/tests/fakebrain.py) e un embedder finto. Tutto contro il server di LABORATORIO o una
porta chiusa: mai il Cloud. La collection sacrificabile e' `brain_netfail` (3 punti canary).

Scenari:
  (a) porta chiusa                      -> connessione rifiutata
  (b) server con API key sbagliata      -> HTTP 401
  (c) JWT senza accesso alla collection -> HTTP 403
  (d) server SPENTO (solo con --qdrant-exe/--config/--pid-file: lo ferma e lo riaccende)
Per ogni scenario: exists() -> StoreUnavailable; ingest.run() -> SystemExit 2, stato locale
intatto, zero chiamate distruttive (spia sul client reale), canary intatti.
Le cartelle orfane (artefatto Qdrant/Windows, vedi labserver.py) si puliscono a server fermo.

Uso:
    python netfail_test.py --url http://127.0.0.1:<porta> --api-key-file <file>
        [--closed-url http://127.0.0.1:<porta-chiusa>] [--qdrant-exe <exe> --config <yaml> --pid-file <pid>] [--out risultati.md]
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import shutil
import sys
import tempfile
import time
import types
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
HERE = Path(__file__).resolve().parent
ENGINE = HERE.parents[1]
COLL = "brain_netfail"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ENGINE / "tests"))
import labserver  # noqa: E402
from labserver import SERVER, server_start, server_stop, server_up  # noqa: E402
from rbac_test import INDEXED_FIELDS, make_jwt, safe_delete  # noqa: E402

ROWS: list[dict] = []


class FakeEmbedder:
    model_name, dim = "fake-dense", 4

    def encode(self, texts, kind="passage"):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class FakeSparseEmbedder:
    model_name = "fake-bm25"

    def encode(self, texts, kind="passage"):
        return [([1, 2], [0.5, 0.5]) for _ in texts]


def install_fake_modules():
    emb = types.ModuleType("embedder")
    emb.dense_model_name = lambda: "fake-dense"
    emb.sparse_model_name = lambda: "fake-bm25"
    emb.bm25_lang = lambda: "italian"
    emb.Embedder, emb.SparseEmbedder = FakeEmbedder, FakeSparseEmbedder
    sys.modules["embedder"] = emb


def load_engine():
    for n in ("store", "ingest", "kb_config"):
        sys.modules.pop(n, None)
    sys.path.insert(0, str(ENGINE / "rag"))
    sys.path.insert(0, str(ENGINE))
    store = importlib.import_module("store")
    ingest = importlib.import_module("ingest")
    assert Path(store.__file__).resolve().parent == (ENGINE / "rag").resolve(), store.__file__
    return store, ingest


def make_spy(real_cls, log: list):
    class SpyClient(real_cls):
        def delete_collection(self, collection_name, *a, **kw):
            log.append(("delete_collection", collection_name))
            raise RuntimeError("SPIA: delete_collection bloccata")

        def create_collection(self, collection_name=None, *a, **kw):
            log.append(("create_collection", collection_name))
            raise RuntimeError("SPIA: create_collection bloccata")
    return SpyClient


def admin_client():
    from qdrant_client import QdrantClient  # noqa: PLC0415
    return QdrantClient(url=SERVER["url"], api_key=SERVER["key"], timeout=10, check_compatibility=False)


def reset_canary():
    from qdrant_client import models  # noqa: PLC0415
    from qdrant_client.http.exceptions import UnexpectedResponse  # noqa: PLC0415
    c = admin_client()
    for attempt in range(3):
        try:
            safe_delete(c, COLL)
            c.create_collection(COLL, vectors_config={"dense": models.VectorParams(size=4, distance=models.Distance.COSINE)},
                                sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)})
            break
        except UnexpectedResponse as e:
            msg = e.content.decode("utf-8", "replace")
            if attempt == 2 or not (e.status_code == 500 or "already exists" in msg) or not labserver.has_control():
                raise
            print(f"  [artefatto Windows ({e.status_code}) su {COLL}: pulizia della cartella orfana]")
            c.close()
            labserver.purge_orphans([COLL])
            c = admin_client()
    for f, s in INDEXED_FIELDS.items():
        c.create_payload_index(COLL, field_name=f, field_schema=s, wait=True)
    c.upsert(COLL, points=[models.PointStruct(
        id=n + 1, vector={"dense": [0.1 * (n + 1), 0.2, 0.3, 0.4], "bm25": models.SparseVector(indices=[n, 42], values=[0.5, 1.0])},
        payload={"canary": "NETFAIL", "path": f"notes/c{n}.md", "node_path": ".", "is_view": False, "is_session": False})
        for n in range(3)], wait=True)
    c.close()


def canary_state() -> str:
    c = admin_client()
    try:
        if not c.collection_exists(COLL):
            return "COLLECTION ASSENTE  <-- WIPE"
        pts, _ = c.scroll(COLL, limit=100, with_payload=True)
        n_can = sum(1 for p in pts if p.payload.get("canary") == "NETFAIL")
        return f"{n_can}/3 canary, {len(pts)} punti totali" + ("" if n_can == 3 else "  <-- WIPE")
    finally:
        c.close()


def make_brain(base: Path):
    import fakebrain  # noqa: PLC0415
    fb = fakebrain.build(base / "brain", with_git=False)
    import kb_config as kc  # noqa: PLC0415
    sp = kc.state_file(fb.root, ".", "vault_state")
    files = {r: {"hash": hashlib.sha256(t.encode()).hexdigest(), "chunks": 1} for r, t in fb.notes.items()}
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"model": "fake-dense", "sparse_model": "fake-bm25", "collection": COLL, "files": files}),
                  encoding="utf-8")
    return fb, sp


def run_case(scenario: str, store, ing, url: str, api_key: str, rebuild: bool = False) -> dict:
    os.environ["QDRANT_URL"] = url
    os.environ["QDRANT_API_KEY"] = api_key
    os.environ["RAG_COLLECTION"] = COLL
    log: list = []
    real = store.QdrantClient
    store.QdrantClient = make_spy(real, log)
    row = {"scenario": scenario, "run": "ingest.run()" + (" --rebuild" if rebuild else "")}
    base = Path(tempfile.mkdtemp(prefix="netfail_"))
    try:
        try:
            r = store.VectorStore(COLL, dim=4).exists()
            row["exists()"] = f"{r!r}"
        except Exception as e:  # noqa: BLE001
            row["exists()"] = type(e).__name__
            row["messaggio"] = str(e)[:160].replace("\n", " ")
        fb, state = make_brain(base)
        before = state.read_bytes()
        try:
            ing.run(fb.root, all_nodes=True, rebuild=rebuild)
            row["run()"] = "terminato senza errori"
        except SystemExit as e:
            row["run()"] = f"SystemExit {e.code}"
        except Exception as e:  # noqa: BLE001
            row["run()"] = f"crash {type(e).__name__}"
        row["stato locale"] = "intatto" if state.exists() and state.read_bytes() == before else "MODIFICATO"
        row["chiamate distruttive"] = ", ".join(f"{n}({c})" for n, c in log) or "nessuna"
    finally:
        store.QdrantClient = real
        shutil.rmtree(base, ignore_errors=True)
    if labserver.has_control() and not server_up():
        server_start()
    row["canary"] = canary_state()
    ROWS.append(row)
    print(f"  {row}")
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--closed-url", default="http://127.0.0.1:1")
    ap.add_argument("--api-key-file", required=True)
    ap.add_argument("--qdrant-exe", default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--pid-file", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    if "cloud.qdrant.io" in a.url:
        print("Rifiuto: questo test gira SOLO su un server di laboratorio.")
        return 2
    labserver.configure(a.url, Path(a.api_key_file).read_text().strip(), a.qdrant_exe, a.config, a.pid_file)
    assert server_up(a.url), "server di lab non raggiungibile"
    install_fake_modules()
    store, ing = load_engine()
    print(f"motore: {store.__file__}\ncollection: {COLL}")
    jwt_other = make_jwt(SERVER["key"], {"access": [{"collection": "brain_other", "access": "r"}]})

    print("\n(a) porta chiusa", a.closed_url)
    reset_canary()
    run_case("(a) porta chiusa", store, ing, a.closed_url, SERVER["key"])
    run_case("(a) porta chiusa", store, ing, a.closed_url, SERVER["key"], rebuild=True)
    print("\n(b) API key sbagliata")
    run_case("(b) API key sbagliata", store, ing, a.url, "chiave-sbagliata")
    print("\n(c) JWT senza accesso alla collection")
    run_case("(c) JWT senza accesso", store, ing, a.url, jwt_other)
    if labserver.has_control():
        print("\n(d) server spento a exists()")
        server_stop()
        run_case("(d) server spento", store, ing, a.url, SERVER["key"])
        server_stop()
        run_case("(d) server spento", store, ing, a.url, SERVER["key"], rebuild=True)
    final = canary_state()
    admin = admin_client()
    cleanup = safe_delete(admin, COLL)
    admin.close()
    if labserver.has_control():
        removed = labserver.purge_orphans([COLL])
        cleanup += f"; cartelle orfane rimosse: {removed or 'nessuna'}"
    print(f"\npulizia di {COLL}: {cleanup}")

    cols = ["scenario", "run", "exists()", "run()", "stato locale", "chiamate distruttive", "canary", "messaggio"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in ROWS:
        lines.append("| " + " | ".join(str(r.get(c, "")).replace("|", "\\|") for c in cols) + " |")
    table = "\n".join(lines)
    print("\n" + table)
    bad = [r for r in ROWS if r["chiamate distruttive"] != "nessuna" or r["stato locale"] != "intatto"
           or not r["run()"].startswith("SystemExit 2") or "WIPE" in r["canary"] or r["exists()"] != "StoreUnavailable"]
    verdict = ("MOTORE v3.1: nessuna chiamata distruttiva, stato intatto, exit 2 in tutti gli scenari; "
               f"canary finali: {final}") if not bad else f"ANOMALIE: {bad}"
    print("\n" + verdict)
    if a.out:
        Path(a.out).write_text(f"# netfail test -- {a.url} / {a.closed_url} -- collection {COLL}\n\n{table}\n\n{verdict}\n\n"
                               f"pulizia: {cleanup}\n", encoding="utf-8")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
