"""embedder -- modelli di embedding LOCALI del brain (denso + sparse).

Copiato da `scripts/rag/embedder.py` di VMR Brain (HEAD 0efa9254). Cosa cambia:
  - niente `load_dotenv()` all'import: il `.env` lo carica kb_config.load_env() per path
    esplicito, PRIMA di importare questo modulo (chi importa decide);
  - i nomi dei modelli si leggono dall'ambiente al momento dell'uso (funzioni
    dense_model_name()/sparse_model_name()/bm25_lang()), non all'import.

Nessuna chiave API, niente dati che escono dalla macchina: gli embedding si calcolano
in locale con ONNX (fastembed). Due modelli, due ruoli:
  - DENSE (semantico): intfloat/multilingual-e5-large (1024 dim). Si cambia con
    RAG_EMBED_MODEL -- il cambio richiede `ingest.py --rebuild` esplicito.
  - SPARSE (BM25): Qdrant/bm25 con stemming (RAG_BM25_LANG, default italian): cattura i
    NOMI PROPRI che il denso da solo sbaglia. Qdrant fonde le due classifiche con RRF.
I modelli e5 vogliono il prefisso "query:"/"passage:": fastembed lo aggiunge da solo via
query_embed()/passage_embed(). La cache dei modelli e' FASTEMBED_CACHE_PATH (condivisibile
fra brain): senza, fastembed usa la temp dell'utente.
"""
from __future__ import annotations

import os
import warnings

import numpy as np
from fastembed import SparseTextEmbedding, TextEmbedding

# fastembed >= 0.6 avvisa a ogni avvio che e5 usa il mean pooling (cambio rispetto a 0.5.1):
# e' il comportamento con cui l'indice e' stato costruito (stessa versione pinnata), non un errore.
warnings.filterwarnings("ignore", message=".*mean pooling.*")

DEFAULT_DENSE = "intfloat/multilingual-e5-large"
DEFAULT_SPARSE = "Qdrant/bm25"
DEFAULT_BM25_LANG = "italian"


def dense_model_name() -> str:
    return os.getenv("RAG_EMBED_MODEL", "").strip() or DEFAULT_DENSE


def sparse_model_name() -> str:
    return os.getenv("RAG_SPARSE_MODEL", "").strip() or DEFAULT_SPARSE


def bm25_lang() -> str:
    return os.getenv("RAG_BM25_LANG", "").strip() or DEFAULT_BM25_LANG


def _l2(a: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(a, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return (a / n).astype("float32")


class Embedder:
    """Embedding DENSO. Carica il modello una volta, ritorna vettori L2-normalizzati."""

    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or dense_model_name()
        self.model = TextEmbedding(self.model_name)
        self.dim = int(len(next(iter(self.model.query_embed(["x"])))))

    def encode(self, texts, kind: str = "passage", **_) -> np.ndarray:
        texts = list(texts)
        if not texts:
            return np.zeros((0, self.dim), dtype="float32")
        gen = self.model.query_embed(texts) if kind == "query" else self.model.passage_embed(texts)
        arr = np.array([v for v in gen], dtype="float32")
        return _l2(arr)


class SparseEmbedder:
    """Embedding SPARSE (BM25). encode() ritorna liste di (indici, valori) per Qdrant."""

    def __init__(self, model_name: str | None = None, language: str | None = None):
        self.model_name = model_name or sparse_model_name()
        language = language or bm25_lang()
        try:
            self.model = SparseTextEmbedding(self.model_name, language=language)
        except Exception:  # noqa: BLE001 -- lingua non supportata: stopword di default
            self.model = SparseTextEmbedding(self.model_name)

    def encode(self, texts, kind: str = "passage"):
        texts = list(texts)
        gen = self.model.query_embed(texts) if kind == "query" else self.model.passage_embed(texts)
        return [(sp.indices.tolist(), sp.values.tolist()) for sp in gen]
