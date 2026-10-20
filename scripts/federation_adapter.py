"""federation_adapter -- adapter federato di QUESTO brain (contratto v1, sola lettura).

Forkato da `kit/federation/adapters/vmr/vmr_shim.py` e reso parte del motore: vive in
`<root>/scripts/federation_adapter.py`, il Master lo avvia come processo diretto con il
python del venv del brain (`-I -B -X utf8`) e cwd = radice del brain:

    .venv/Scripts/python.exe -I -B -X utf8 scripts/federation_adapter.py [--root <radice>]

Legge UNA riga JSON da stdin (request.schema.json), risponde UNA riga JSON su stdout
(response.schema.json). `capabilities` dichiarate = implementate: query, health, briefing.

  query  mode=exact    -> brain.exact_hits() in-process (stdlib): llms.txt + note del nodo
                          radice; se la frase intera non trova nulla e ha piu' termini,
                          ripiega sui singoli termini (`data.match` lo dice).
  query  mode=semantic -> SOLO se nel .env del brain c'e' RAG_FEDERATION_SEMANTIC=1 E
                          QDRANT_URL (il .env si carica, per path esplicito, soltanto in
                          questo ramo). Altrimenti capability_unavailable.
  health               -> radice, motore, venv, git HEAD e data, llms.txt, meta indice
                          (mai l'URL del cluster), profilo, sentinella d'importazione.
  briefing             -> ultimo daily + ultime 3 sessioni del journal + feed_check
                          (pendenze), SOLO lettura.

Garanzie: nessuna scrittura (niente log, stato, temp, bytecode); per gli op non semantici
una sentinella d'importazione blocca i moduli di rete/DB/credenziali; stderr solo
diagnostica sanificata (mai testo della richiesta); errori sempre come envelope `error`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

CONTRACT_MAJOR = 1
CAPABILITIES = ["query", "health", "briefing"]
MAX_REQUEST_BYTES = 65_536
MAX_HIT_TEXT = 600
DEFAULT_LIMIT = 20
MAX_LIMIT = 200
GIT_TIMEOUT = 10

BLOCKED_MODULES = frozenset({
    "socket", "_socket", "ssl", "_ssl", "http", "http.client", "urllib.request", "urllib3", "requests",
    "httpx", "aiohttp", "websocket", "websockets", "grpc", "smtplib", "ftplib", "xmlrpc",
    "qdrant_client", "openai", "anthropic", "dotenv", "fastembed", "sentence_transformers", "torch",
    "transformers", "huggingface_hub", "embedder", "store", "query",
})
STOPWORDS = frozenset("""
a ad agli ai al alla alle allo anche c che chi ci cio come con cosa cui da dagli dai dal dall dalla dalle
dallo degli dei del dell della delle dello di dove e ed era essere fa fra gli ha hai hanno ho i il in io l la
le lei li lo loro lui ma me mi mia mie miei mio ne nei nel nell nella nelle nello noi non nostra nostro o
ogni per perche piu poi quale quali quando quanto quella quelle quelli quello questa queste questi questo
qui quindi se sei senza si sia sono sta sto su sua sue sugli sui sul sull sulla sulle suo suoi te ti tra tu
tua tue tuo tuoi tutta tutte tutti tutto un una uno va vi voi cerca cercami trova trovami dimmi dammi mostra
mostrami elenca riassumi frase dentro parola esiste contiene
about after all also an and any are as at be been but by can could did do does for from had has have he her
here his how if in into is it its just me more most my no not of on one only or other our out over she so
some such than that the their them then there these they this those to up was we were what when where which
who why will with would you your find search show tell give list
""".split())
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


class _ImportSentinel:
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".")[0]
        if fullname in BLOCKED_MODULES or root in BLOCKED_MODULES:
            raise ImportError(f"modulo {fullname!r} bloccato dalla sentinella dell'adapter federato (sola lettura)")
        return None


def install_sentinel() -> list[str]:
    sys.meta_path.insert(0, _ImportSentinel())
    return sorted(m for m in sys.modules if m in BLOCKED_MODULES)


# ----------------------------------------------------------------------------
# Utilita'
# ----------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def mtime_iso(p: Path) -> str | None:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except OSError:
        return None


class Ctx:
    brain_id = "unknown"


def envelope(request_id, status, data=None, error=None, freshness=None) -> dict:
    return {"contract_version": CONTRACT_MAJOR, "brain_id": Ctx.brain_id, "request_id": request_id,
            "status": status, "generated_at": now_iso(), "freshness": freshness, "data": data, "error": error}


def err(request_id, code, message, freshness=None) -> dict:
    return envelope(request_id, "error", None, {"code": code, "message": message}, freshness)


def emit(env: dict) -> None:
    sys.stdout.write(json.dumps(env, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def diag(msg: str) -> None:
    sys.stderr.write(f"[federation_adapter] {msg}\n")
    sys.stderr.flush()


def _clip(text: str) -> str:
    text = text.strip()
    return text if len(text) <= MAX_HIT_TEXT else text[: MAX_HIT_TEXT - 1] + "…"


# ----------------------------------------------------------------------------
# Operazioni
# ----------------------------------------------------------------------------
def freshness(root: Path, kc) -> dict:
    llms = root / "llms.txt"
    last_session = None
    sess = sorted((root / kc.SESSIONS_REL).glob("*.md")) if (root / kc.SESSIONS_REL).is_dir() else []
    if sess:
        last_session = mtime_iso(max(sess, key=lambda p: p.stat().st_mtime))
    return {"index_updated_at": mtime_iso(llms) if llms.is_file() else None, "last_session_at": last_session}


def op_query_exact(root: Path, kc, brain, text: str, limit: int) -> tuple[dict, bool]:
    nodes = kc.iter_nodes(root)
    node = nodes[0]
    phrase = text.strip().lower()
    tokens, seen = [], set()
    for tok in _TOKEN_RE.findall(phrase):
        if len(tok) >= 3 and tok not in STOPWORDS and tok not in seen:
            seen.add(tok)
            tokens.append(tok)

    def to_hits(res: dict) -> list[dict]:
        out = []
        for h in res["hits"]:
            out.append({"text": _clip(h["text"]), "source_id": f"{h['source']}#L{h['line']}",
                        "source_updated_at": mtime_iso(root / h["source"]), "node_path": h["node_path"]})
        return out

    res = brain.exact_hits(phrase, root, node, nodes, limit=limit)
    hits, total, match, terms = to_hits(res), res["total"], "phrase", [phrase]
    if not hits and len(tokens) > 1:
        merged: dict[str, dict] = {}
        for tok in tokens:
            r = brain.exact_hits(tok, root, node, nodes, limit=MAX_LIMIT * 4)
            for h in to_hits(r):
                e = merged.setdefault(h["source_id"], {**h, "terms_matched": 0})
                e["terms_matched"] += 1
        ranked = sorted(merged.values(), key=lambda h: -h["terms_matched"])
        hits, total, match, terms = ranked[:limit], len(ranked), "terms", tokens
    partial = not (root / "llms.txt").is_file()
    data = {"hits": hits, "index_updated_at": mtime_iso(root / "llms.txt"), "mode": "exact",
            "match": match, "terms": terms, "total_hits": total, "truncated": total > len(hits)}
    return data, partial


def op_query_semantic(root: Path, kc, text: str, limit: int) -> tuple[dict | None, str | None]:
    """Ritorna (data, None) oppure (None, motivo) se la capability non e' disponibile."""
    kc.load_env(root, root)   # SOLO qui: la semantica ha bisogno del .env
    if os.environ.get("RAG_FEDERATION_SEMANTIC", "").strip() != "1":
        return None, "modalita' semantica non abilitata per la federazione (RAG_FEDERATION_SEMANTIC=1 nel .env del brain)"
    if not os.environ.get("QDRANT_URL", "").strip():
        return None, "QDRANT_URL assente: nessun server Qdrant configurato per la semantica"
    sys.path.insert(0, str(SCRIPTS / "rag"))
    import query as q  # noqa: PLC0415
    node = kc.iter_nodes(root)[0]
    res = q.answer(text, k=min(limit, 50), root=root, node=node)
    hits = [{"text": _clip(h.get("text", "")), "source_id": f"{h.get('path')}#{h.get('heading') or h.get('stem')}",
             "source_updated_at": mtime_iso(root / str(h.get("path", ""))), "score": round(float(h.get("score", 0)), 5),
             "node_path": h.get("node_path")} for h in res["hits"][:limit]]
    meta_p = kc.state_file(root, ".", "meta")
    return {"hits": hits, "index_updated_at": mtime_iso(meta_p) if meta_p.is_file() else None, "mode": "semantic",
            "confidence": res["confidence"], "total_hits": len(res["hits"]), "truncated": len(res["hits"]) > len(hits)}, None


def _git(root: Path, *args: str) -> tuple[int, str]:
    cmd = ["git", "--no-optional-locks", "-c", f"safe.directory={root.as_posix()}", "-C", str(root), *args]
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, e.__class__.__name__
    out = (p.stdout or b"").decode("utf-8", "replace").strip()
    return p.returncode, out if p.returncode == 0 else (p.stderr or b"").decode("utf-8", "replace").strip()[:120]


def op_health(root: Path, kc, kc_error: str | None, preloaded_blocked: list[str]) -> dict:
    checks: list[dict] = []

    def add(name, ok, detail, degraded_only=True):
        checks.append({"name": name, "state": "ok" if ok else ("degraded" if degraded_only else "unavailable"),
                       "detail": detail})

    add("root", kc is not None and kc.is_node_dir(root), root.as_posix(), degraded_only=False)
    add("engine", (root / "scripts" / "kb_config.py").is_file(), "scripts/kb_config.py" if (root / "scripts" / "kb_config.py").is_file()
        else "motore assente in scripts/", degraded_only=False)
    in_venv = Path(sys.prefix).resolve() == (root / ".venv").resolve()
    add("venv", in_venv, f"python {sys.version.split()[0]} prefix={'root/.venv' if in_venv else Path(sys.prefix).name}")
    if kc is not None:
        try:
            prof = kc.load_profile(root)
            present = [f for f in prof.folders if (root / f).is_dir()]
            add("profile", not prof.is_default, f"{'brain.profile.yaml' if not prof.is_default else 'profilo di default (file assente)'}; "
                f"{len(present)}/{len(prof.folders)} cartelle presenti")
        except Exception as e:  # noqa: BLE001
            add("profile", False, f"profilo illeggibile ({e.__class__.__name__})")
    else:
        add("profile", False, f"motore non importabile: {kc_error}", degraded_only=False)
    rc, head = _git(root, "rev-parse", "--short", "HEAD")
    rc2, when = _git(root, "log", "-1", "--format=%cI")
    last_commit = when if rc2 == 0 else None
    add("git", rc == 0 and rc2 == 0, f"HEAD {head} ultimo commit {when}" if rc == 0 and rc2 == 0 else f"git non disponibile ({head or when})")
    llms = root / "llms.txt"
    add("llms_txt", llms.is_file(), f"{llms.stat().st_size} byte, aggiornato {mtime_iso(llms)}" if llms.is_file() else "assente")
    meta_p = root / "scripts" / "rag" / "index" / "meta.json"
    if meta_p.is_file():
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            where = str(meta.get("where", ""))
            kind = "remoto" if where.startswith("remote") else ("embedded" if where else "n/d")
            add("rag_index_meta", True, f"meta.json aggiornato {mtime_iso(meta_p)}; note={meta.get('notes')} chunk={meta.get('chunks')} "
                f"modello={meta.get('model')} indice={kind}")
        except (OSError, ValueError) as e:
            add("rag_index_meta", False, f"meta.json illeggibile ({e.__class__.__name__})")
    else:
        add("rag_index_meta", False, "meta.json assente (nessun ingest)")
    add("import_sentinel", not preloaded_blocked, "nessun modulo di rete/DB caricato" if not preloaded_blocked
        else f"moduli gia' caricati all'avvio: {', '.join(preloaded_blocked)}")
    states = {c["state"] for c in checks}
    state = "unavailable" if "unavailable" in states else ("degraded" if "degraded" in states else "ok")
    fresh = {"index_updated_at": mtime_iso(llms) if llms.is_file() else None, "last_session_at": last_commit}
    return {"state": state, "checks": checks, "last_sync_at": last_commit, "capabilities": CAPABILITIES,
            "_freshness": fresh}


def _note_item(root: Path, kc, p: Path) -> dict:
    try:
        fm, body = kc.read_frontmatter(p)
    except Exception:  # noqa: BLE001
        fm, body = {}, kc.read_text(p)
    title = kc.fm_str(fm.get("title")) or p.stem
    summary = kc.fm_str(fm.get("summary"))
    text = f"{title}: {summary}" if summary else f"{title}: {_clip(body)[:300]}"
    return {"text": _clip(text), "source_id": p.relative_to(root).as_posix(), "source_updated_at": mtime_iso(p)}


def op_briefing(root: Path, kc, since_days: int) -> tuple[dict, bool]:
    items: list[dict] = []
    partial = False
    stamps: list[float] = []
    daily_dir, sess_dir = root / kc.DAILY_REL, root / kc.SESSIONS_REL
    dailies = sorted(daily_dir.glob("*.md")) if daily_dir.is_dir() else []
    if dailies:
        p = dailies[-1]
        items.append({**_note_item(root, kc, p), "kind": "daily"})
        stamps.append(p.stat().st_mtime)
    sessions = sorted(sess_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:3] if sess_dir.is_dir() else []
    for p in sessions:
        items.append({**_note_item(root, kc, p), "kind": "session"})
        stamps.append(p.stat().st_mtime)
    try:
        import feed_check  # noqa: PLC0415
        node = kc.iter_nodes(root)[0]
        pend = feed_check.pending(root, node)
        warns = pend.pop("_warn", [])
        for origin, files in pend.items():
            for f in files:
                items.append({"text": f"lavoro operativo non ancora compilato ({origin}): {f}", "source_id": f, "kind": "feed"})
        for w in warns:
            items.append({"text": f"feed_check: {w}", "source_id": "workspace/feed-state.json", "kind": "feed_warn"})
    except Exception as e:  # noqa: BLE001
        partial = True
        diag(f"feed_check non disponibile: {e.__class__.__name__}")
    src_upd = datetime.fromtimestamp(max(stamps), timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z") if stamps else None
    return {"items": items, "source_updated_at": src_upd, "since_days": since_days,
            "counts": {"daily": len(dailies[-1:]), "sessions": len(sessions)}}, partial


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--root", default=None)
    ns, _unknown = ap.parse_known_args()

    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    req: dict | None = None
    parse_error = None
    if len(raw) > MAX_REQUEST_BYTES:
        parse_error = f"richiesta oltre {MAX_REQUEST_BYTES} byte"
    else:
        try:
            req = json.loads(raw.decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            parse_error = "richiesta non e' un oggetto JSON UTF-8"
            req = None
    req = req or {}
    rid = req.get("request_id") if isinstance(req.get("request_id"), str) else None
    op = req.get("op")
    args = req.get("args") if isinstance(req.get("args"), dict) else {}
    mode = args.get("mode", "exact") if op == "query" else None
    semantic = op == "query" and mode == "semantic" and req.get("class") in ("normal", "sensitive")

    # Sentinella: per tutto cio' che non e' semantico, rete/DB/credenziali restano fuori.
    preloaded = install_sentinel() if not semantic else []

    # Identita' PRIMA di qualunque risposta: anche un errore porta il brain_id giusto.
    kc = None
    kc_error = None
    try:
        import kb_config as kc_mod  # noqa: PLC0415
        kc = kc_mod
    except Exception as e:  # noqa: BLE001
        kc_error = e.__class__.__name__
    root = Path(ns.root).resolve() if ns.root else (
        (kc.deployed_root() if kc is not None else None) or Path.cwd().resolve())
    if kc is not None:
        try:
            Ctx.brain_id = kc.brain_id(root, strict=False) or Ctx.brain_id
        except Exception:  # noqa: BLE001
            pass

    if parse_error:
        emit(err(None, "bad_request", parse_error))
        return 0
    cv = req.get("contract_version")
    if not isinstance(cv, int) or isinstance(cv, bool) or cv != CONTRACT_MAJOR:
        emit(err(rid, "bad_request", f"contract_version non supportata (attesa {CONTRACT_MAJOR})"))
        return 0
    cls = req.get("class")
    if cls not in ("normal", "sensitive"):
        emit(err(rid, "class_rejected", "classe non trasferibile o sconosciuta: accettate solo normal|sensitive"))
        return 0

    if op == "health":
        data = op_health(root, kc, kc_error, preloaded)
        fresh = data.pop("_freshness")
        emit(envelope(rid, "ok" if data["state"] != "unavailable" else "partial", data, None, fresh))
        return 0
    if kc is None:
        emit(err(rid, "internal", f"motore del brain non importabile ({kc_error})"))
        return 0
    if op == "briefing":
        sd = args.get("since_days", 1)
        if isinstance(sd, bool) or not isinstance(sd, int) or not 1 <= sd <= 365:
            emit(err(rid, "bad_request", "args.since_days deve essere un intero in [1, 365]", freshness(root, kc)))
            return 0
        try:
            data, partial = op_briefing(root, kc, sd)
        except Exception as e:  # noqa: BLE001
            diag(f"briefing fallito: {e.__class__.__name__}")
            emit(err(rid, "internal", f"briefing fallito ({e.__class__.__name__})", freshness(root, kc)))
            return 0
        emit(envelope(rid, "partial" if partial else "ok", data, None, freshness(root, kc)))
        return 0
    if op != "query":
        emit(err(rid, "bad_request", "op sconosciuta: accettate query|health|briefing"))
        return 0
    if mode not in ("exact", "semantic"):
        emit(err(rid, "bad_request", "mode fuori enum exact|semantic", freshness(root, kc)))
        return 0
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        emit(err(rid, "bad_request", "args.text mancante o vuoto", freshness(root, kc)))
        return 0
    limit = args.get("limit", DEFAULT_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        emit(err(rid, "bad_request", f"args.limit deve essere un intero in [1, {MAX_LIMIT}]", freshness(root, kc)))
        return 0
    t0 = time.monotonic()
    try:
        if semantic:
            data, why = op_query_semantic(root, kc, text, limit)
            if data is None:
                emit(err(rid, "capability_unavailable", why, freshness(root, kc)))
                return 0
            partial = False
        else:
            import brain  # noqa: PLC0415
            data, partial = op_query_exact(root, kc, brain, text, limit)
    except Exception as e:  # noqa: BLE001
        diag(f"lookup fallito: {e.__class__.__name__}")
        emit(err(rid, "internal", f"lookup fallito ({e.__class__.__name__})", freshness(root, kc)))
        return 0
    data["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    emit(envelope(rid, "partial" if partial else "ok", data, None, freshness(root, kc)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
    except Exception as e:  # noqa: BLE001 -- mai il contenuto della richiesta su stderr
        diag(f"errore interno: {e.__class__.__name__}")
        try:
            emit(err(None, "internal", f"errore interno dell'adapter ({e.__class__.__name__})"))
        except Exception:  # noqa: BLE001
            pass
        sys.exit(0)
