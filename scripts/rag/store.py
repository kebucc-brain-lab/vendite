"""store -- archivio vettoriale del brain su Qdrant (denso + sparse + RRF).

Copiato da `kit/rag/store.py` (3.0, patch tri-stato di VMR HEAD 0efa9254). Cosa cambia:
  - la COLLECTION e' obbligatoria (parametro esplicito; chi chiama la legge da
    RAG_COLLECTION): nessun default `vmr_brain`;
  - QDRANT_URL obbligatoria: l'embedded su file e' ammesso SOLO con RAG_ALLOW_EMBEDDED=1
    (mai in un albero di nodi: il file ammette UNA connessione per volta);
  - niente `load_dotenv()` all'import (lo fa kb_config.load_env per path esplicito);
  - indici payload creati con la collection: `path`, `node_path`, `is_session`, `is_view`;
  - filtro `node_path IN {...}` in OGNI Prefetch della query ibrida (e nel dense/sparse),
    `count()` e `delete_node()` filtrati per nodo, `sparse()` separato per la confidenza;
  - `drop()` esplicito (usato solo dal selftest sulla SUA collection).

Invariante ereditato: exists() ha TRE esiti. True/False solo se lo dice il server; timeout,
rete, 5xx, 401/403, 429 -> StoreUnavailable, e chi chiama si ferma SENZA toccare la
collection (niente recreate/create/delete "al buio").
"""
from __future__ import annotations

import os
import warnings
from pathlib import Path
from urllib.parse import urlsplit

from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

LOCAL_DB = Path(__file__).resolve().parent / "qdrant_db"

# Campi payload usati nei filtri (delete per path, filtro per nodo, scope sessioni).
# L'embedded filtra anche senza indice; il server NO (400 "Index required").
INDEXED_FIELDS = {
    "path": models.PayloadSchemaType.KEYWORD,
    "node_path": models.PayloadSchemaType.KEYWORD,
    "is_view": models.PayloadSchemaType.BOOL,
    "is_session": models.PayloadSchemaType.BOOL,
}


class StoreUnavailable(RuntimeError):
    """Qdrant non ha dato una risposta affidabile (timeout, connessione rifiutata,
    5xx, chiave/permessi): NON significa "collection assente". Chi chiama si ferma
    senza toccare la collection: niente recreate, niente delete, niente create."""


class StoreConfigError(RuntimeError):
    """Configurazione mancante (collection, QDRANT_URL): il comando si ferma con messaggio."""


def node_filter(node_paths) -> models.Filter | None:
    """Filtro `node_path IN node_paths`; None = nessun filtro (esplicito, mai implicito)."""
    if node_paths is None:
        return None
    paths = [str(p) for p in node_paths]
    if not paths:
        paths = ["__nessun_nodo__"]   # lista vuota = zero risultati, non "tutto"
    return models.Filter(must=[models.FieldCondition(key="node_path", match=models.MatchAny(any=paths))])


def _public_where(url: str) -> str:
    u = urlsplit(url)
    host = u.hostname or url
    return f"remote {u.scheme}://{host}" + (f":{u.port}" if u.port else "")


class VectorStore:
    def __init__(self, collection: str, dim: int | None = None, url: str | None = None,
                 api_key: str | None = None, embedded_path: Path | str | None = None,
                 timeout: int = 120):
        if not collection or not str(collection).strip():
            raise StoreConfigError("collection assente: RAG_COLLECTION e' obbligatoria (nessun default)")
        self.dim = dim
        self.collection = str(collection).strip()
        url = (url if url is not None else os.getenv("QDRANT_URL", "")).strip()
        if url:
            key = api_key if api_key is not None else (os.getenv("QDRANT_API_KEY") or None)
            host = (urlsplit(url).hostname or "").lower()
            with warnings.catch_warnings():
                if host in ("127.0.0.1", "localhost", "::1"):
                    # chiave su http verso loopback: non esce dalla macchina, l'avviso e' solo rumore
                    warnings.filterwarnings("ignore", message=".*insecure connection.*")
                self.client = QdrantClient(url=url, api_key=key, timeout=timeout)
            self.where = _public_where(url)
            self.embedded = False
        else:
            if os.getenv("RAG_ALLOW_EMBEDDED", "").strip() != "1":
                raise StoreConfigError("QDRANT_URL assente: serve un server Qdrant (l'embedded su file e' "
                                       "ammesso solo con RAG_ALLOW_EMBEDDED=1, mai in un albero di nodi)")
            path = Path(embedded_path or os.getenv("RAG_EMBEDDED_PATH") or LOCAL_DB)
            path.mkdir(parents=True, exist_ok=True)
            self.client = QdrantClient(path=str(path))
            self.where = f"embedded {path}"
            self.embedded = True

    # -- ciclo di vita -------------------------------------------------------
    def recreate(self, dim: int | None = None):
        """Wipe + create: SOLO su --rebuild ESPLICITO (unico punto che cancella la
        collection condivisa). Se la collection manca davvero, crea e basta."""
        dim = dim or self.dim
        if self.exists():   # tri-stato: su errore solleva StoreUnavailable, nessun delete
            self.client.delete_collection(self.collection)
        self.create(dim)

    def create(self, dim: int | None = None):
        """Crea la collection (mai cancellarla) + indici payload. Se esiste gia' il
        server risponde 409 e l'errore propaga: non si "risolve" con un delete."""
        dim = dim or self.dim
        if not dim:
            raise StoreConfigError("dimensione dei vettori sconosciuta: serve l'embedder per creare la collection")
        self.client.create_collection(
            collection_name=self.collection,
            vectors_config={"dense": models.VectorParams(size=int(dim), distance=models.Distance.COSINE)},
            sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )
        self.ensure_indexes()

    def ensure_indexes(self):
        """Indici payload per i campi filtrati: idempotente, innocuo in embedded."""
        for field, schema in INDEXED_FIELDS.items():
            try:
                self.client.create_payload_index(
                    self.collection, field_name=field, field_schema=schema, wait=True)
            except Exception:  # noqa: BLE001 -- gia' presente
                pass

    def ensure(self, dim: int | None = None):
        """Crea la collection se manca (senza distruggerla se esiste). Su errore di
        rete/chiave propaga StoreUnavailable: nessuna create "al buio"."""
        if not self.exists():
            self.create(dim or self.dim)
        else:
            self.ensure_indexes()

    def drop(self):
        """Cancella la collection. Esplicito e raro: lo usa il selftest sulla SUA
        collection `<id>_selftest`. Mai chiamato dall'ingest."""
        if self.exists():
            self.client.delete_collection(self.collection)

    def exists(self) -> bool:
        """Tre esiti, non due. True = presente. False = ASSENTE, e solo se lo dice il
        server (exists=false, oppure 404 esplicito). Tutto il resto (timeout, rete,
        5xx, 401/403, 429...) e' StoreUnavailable."""
        try:
            return bool(self.client.collection_exists(self.collection))
        except UnexpectedResponse as e:
            if e.status_code == 404:
                return False
            raise StoreUnavailable(
                f"Qdrant {self.where}: HTTP {e.status_code} {e.reason_phrase} su "
                f"collection_exists('{self.collection}')") from e
        except Exception as e:  # noqa: BLE001
            cause = getattr(e, "source", None) or e   # ResponseHandlingException avvolge httpx
            raise StoreUnavailable(
                f"Qdrant {self.where}: {type(cause).__name__}: {cause} su "
                f"collection_exists('{self.collection}')") from e

    # -- dati ----------------------------------------------------------------
    def count(self, node_paths=None) -> int:
        flt = node_filter(node_paths)
        if flt is None:
            return self.client.count(self.collection, exact=True).count
        return self.client.count(self.collection, count_filter=flt, exact=True).count

    def delete_paths(self, paths):
        """Elimina tutti i punti il cui payload 'path' e' in `paths`."""
        if not paths:
            return
        self.client.delete(
            self.collection,
            points_selector=models.FilterSelector(filter=models.Filter(
                must=[models.FieldCondition(key="path", match=models.MatchAny(any=list(paths)))])),
            wait=True,
        )

    def delete_node(self, node_path: str):
        """Elimina tutti i punti di UN nodo (rebuild parziale senza wipe della collection)."""
        self.client.delete(self.collection,
                           points_selector=models.FilterSelector(filter=node_filter([node_path])), wait=True)

    @staticmethod
    def point(pid, dense, sparse, payload):
        idx, val = sparse
        return models.PointStruct(
            id=pid if isinstance(pid, str) else int(pid),
            vector={
                "dense": [float(x) for x in dense],
                "bm25": models.SparseVector(indices=[int(i) for i in idx], values=[float(v) for v in val]),
            },
            payload=payload,
        )

    def upsert(self, points):
        self.client.upsert(self.collection, points=points, wait=True)

    # -- ricerca -------------------------------------------------------------
    def hybrid(self, dense_vec, sparse, k: int = 8, pool: int = 30, node_paths=None):
        """Denso + BM25 fusi con RRF lato server. Il filtro per nodo sta in OGNI
        Prefetch (e al livello della fusione): i candidati fuori dominio non entrano
        nemmeno nelle classifiche parziali."""
        idx, val = sparse
        flt = node_filter(node_paths)
        res = self.client.query_points(
            self.collection,
            prefetch=[
                models.Prefetch(query=[float(x) for x in dense_vec], using="dense", limit=pool, filter=flt),
                models.Prefetch(
                    query=models.SparseVector(indices=[int(i) for i in idx], values=[float(v) for v in val]),
                    using="bm25", limit=pool, filter=flt),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            query_filter=flt,
            limit=k, with_payload=True,
        )
        return res.points

    def dense(self, dense_vec, k: int = 8, node_paths=None):
        res = self.client.query_points(
            self.collection, query=[float(x) for x in dense_vec],
            using="dense", limit=k, with_payload=True, query_filter=node_filter(node_paths))
        return res.points

    def sparse(self, sparse_vec, k: int = 8, node_paths=None):
        idx, val = sparse_vec
        res = self.client.query_points(
            self.collection,
            query=models.SparseVector(indices=[int(i) for i in idx], values=[float(v) for v in val]),
            using="bm25", limit=k, with_payload=True, query_filter=node_filter(node_paths))
        return res.points

    def retrieve(self, ids):
        # id uuid5 deterministici (stringhe); gli interi restano ammessi per compatibilita'
        return self.client.retrieve(
            self.collection,
            ids=[i if isinstance(i, str) else int(i) for i in ids],
            with_payload=True)
