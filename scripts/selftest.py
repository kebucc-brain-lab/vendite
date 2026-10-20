"""selftest -- prova l'intera catena del motore in una COPIA TEMPORANEA del brain (mai sul reale).

Nuovo in v3.1 (SPEC par. 8). Copia il brain (senza .git, .venv, .memory, .secrets, .env*,
stato dell'indice), poi, nella copia:
  lint                 0 errori (anche a vault vuoto)
  skills_yaml          frontmatter YAML delle skill valido (name + description)
  placeholders         zero segnaposto (doppia graffa) residui nel brain
  env_untracked        .env/.env.* non tracciati da git (sul brain reale, sola lettura)
  llms_exact           llms.txt rigenerato e `brain.py ask --exact` trova il BRAIN_ID
  --- con Qdrant (--qdrant-url + --qdrant-api-key-env NAME, oppure --qdrant-exe PATH) ---
  append_no_index      `ingest.py --append-sessions` senza indice -> exit 1
  ingest_first         3 note finte (+ hub) ingerite in `<id>_selftest`
  ingest_nochange      rilancio -> "nessuna nota cambiata"
  closed_port          QDRANT_URL su porta chiusa -> exit 2 e conteggio punti intatto
  semantic_query       la query semantica trova la nota giusta
  tree_isolation       due nodi finti; query dal nodo A con k=50 -> zero hit del nodo B
  cleanup              la collection `<id>_selftest` viene cancellata
Stampa la tabella PASS/FAIL/NA (o --json); exit 0 SOLO con zero FAIL.

`--qdrant-api-key-env NAME`: NAME e' il NOME di una variabile (mai il valore); si risolve prima
nell'ambiente del processo, poi nel `.env` del brain (`<root>/.env`, caricato per path esplicito
con kb_config.load_env senza sovrascrivere l'ambiente). Esempio tipico: `--qdrant-api-key-env
QDRANT_API_KEY` legge la chiave gia' scritta nel `.env` del brain, senza esportarla.

Uso:
    python scripts/selftest.py --root <brain> [--qdrant-url URL --qdrant-api-key-env NAME | --qdrant-exe PATH]
                               [--json] [--keep] [--verbose]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import kb_config as kc  # noqa: E402

COPY_IGNORE = shutil.ignore_patterns(".git", ".venv", "venv", ".memory", ".secrets", "qdrant_db", "__pycache__",
                                     "node_modules", ".tmp", ".env", ".env.*", ".pytest_cache", ".obsidian")
TEXT_EXT = {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".py", ".ps1", ".mjs", ".js", ".ini", ".cfg", ".code-workspace", ""}
SKIP_SCAN_DIRS = {".git", ".venv", "node_modules", "__pycache__"}
PH = "{" + "{"   # la coppia di graffe aperte dei segnaposto, scritta cosi' per non essere un segnaposto
FAKE_NOTES = [
    ("selftest-hub", "Selftest hub", "Nota hub del selftest che collega le tre note finte.",
     "Hub del selftest: collega [[selftest-tartufo]], [[selftest-faro]] e [[selftest-orologio]]."),
    ("selftest-tartufo", "Tartufo bianco di Alba", "Il tartufo bianco di Alba si raccoglie in autunno con i cani addestrati.",
     "Il tartufo bianco di Alba (Tuber magnatum) si cerca di notte, in autunno, con cani addestrati; il prezzo supera i tremila euro al chilo."),
    ("selftest-faro", "Faro di Capo Spartivento", "Il faro di Capo Spartivento in Sardegna e' del 1866 e oggi e' un albergo.",
     "Il faro di Capo Spartivento, costruito nel 1866 sulla costa sud della Sardegna, e' stato trasformato in un albergo di lusso."),
    ("selftest-orologio", "Orologio astronomico di Praga", "L'orologio astronomico di Praga e' del 1410 e mostra la posizione del sole e della luna.",
     "L'orologio astronomico di Praga, montato nel 1410 sulla torre del municipio, mostra la posizione del sole, della luna e i segni zodiacali."),
]
SEMANTIC_Q = ("come si cerca il tartufo bianco e quanto costa", "selftest-tartufo")
TREE_NODES = {
    "selftest-nodi/alpha": [("selftest-alpaca", "Allevamento di alpaca", "Alpaca allevati in Patagonia per la lana.",
                             "Gli alpaca si allevano in Patagonia; la lana si tosa in primavera.")],
    "selftest-nodi/beta": [("selftest-barbabietola", "Barbabietola da zucchero", "Barbabietola da zucchero coltivata in Polonia.",
                            "La barbabietola da zucchero si semina in aprile in Polonia e si raccoglie in ottobre.")],
}
TREE_Q = ("coltivazione della barbabietola da zucchero in Polonia", "selftest-nodi/alpha", "selftest-nodi/beta")


class Report:
    def __init__(self):
        self.rows: list[dict] = []
        self.logs: list[str] = []

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.rows.append({"name": name, "status": status, "detail": detail})

    @property
    def fails(self) -> int:
        return sum(1 for r in self.rows if r["status"] == "FAIL")

    def table(self) -> str:
        w = max(len(r["name"]) for r in self.rows) if self.rows else 10
        lines = [f"{'check'.ljust(w)}  esito  dettaglio", f"{'-' * w}  -----  ---------"]
        for r in self.rows:
            lines.append(f"{r['name'].ljust(w)}  {r['status'].ljust(5)}  {r['detail']}")
        n_pass = sum(1 for r in self.rows if r["status"] == "PASS")
        n_na = sum(1 for r in self.rows if r["status"] == "NA")
        lines.append(f"\nPASS {n_pass} | FAIL {self.fails} | NA {n_na}")
        return "\n".join(lines)


def tail(out: str, n: int = 1) -> str:
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return " | ".join(lines[-n:])[:300] if lines else ""


def run(cmd: list[str], env: dict, cwd: Path, timeout: int = 1800) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr.strip() else "")
    except subprocess.TimeoutExpired:
        return 124, f"timeout dopo {timeout}s"
    except OSError as e:
        return 127, f"avvio fallito: {e}"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def closed_port() -> int:
    """Una porta sicuramente chiusa: la si apre e la si richiude subito."""
    return free_port()


def _http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


class QdrantProc:
    """Istanza Qdrant usa-e-getta: porte libere, chiave casuale, jwt_rbac, storage nel temp."""

    def __init__(self, exe: Path, base: Path):
        self.exe, self.base = exe, base
        self.proc = None
        self.url = ""
        self.key = ""

    def start(self) -> None:
        http, grpc = free_port(), free_port()
        self.key = secrets.token_hex(24)
        st = self.base / "qdrant"
        for d in ("storage", "snapshots", "temp"):
            (st / d).mkdir(parents=True, exist_ok=True)
        cfg = st / "config.yaml"
        kc.write_text(cfg, "log_level: WARN\ntelemetry_disabled: true\nstorage:\n"
                           f"  storage_path: {(st / 'storage').as_posix()}\n  snapshots_path: {(st / 'snapshots').as_posix()}\n"
                           f"  temp_path: {(st / 'temp').as_posix()}\nservice:\n  host: 127.0.0.1\n  http_port: {http}\n"
                           f"  grpc_port: {grpc}\n  api_key: {self.key}\n  jwt_rbac: true\n")
        log = open(st / "qdrant.log", "ab")
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self.proc = subprocess.Popen([str(self.exe), "--config-path", str(cfg)], cwd=str(st), stdout=log,
                                     stderr=subprocess.STDOUT, creationflags=flags, stdin=subprocess.DEVNULL)
        self.url = f"http://127.0.0.1:{http}"
        for _ in range(120):
            if _http_ok(self.url + "/readyz"):
                return
            if self.proc.poll() is not None:
                break
            time.sleep(0.25)
        raise RuntimeError(f"Qdrant non e' partito (log: {st / 'qdrant.log'})")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def write_note(path: Path, folder: str, stem: str, title: str, summary: str, links: list[str], body: str) -> None:
    rel = "".join(f'  - "[[{lk}]]"\n' for lk in links)
    kc.write_text(path, f"---\ntitle: \"{title}\"\nsummary: \"{summary}\"\ntags: [{folder}, type/note, status/active]\n"
                        f"status: active\ncreated: 2026-10-18\nupdated: 2026-10-18\nrelated:\n{rel}---\n\n# {title}\n\n{body}\n")


def point_count(url: str, key: str, collection: str) -> int | None:
    try:
        from qdrant_client import QdrantClient  # noqa: PLC0415
        c = QdrantClient(url=url, api_key=key or None, timeout=20)
        try:
            if not c.collection_exists(collection):
                return 0
            return int(c.count(collection, exact=True).count)
        finally:
            c.close()
    except Exception:  # noqa: BLE001
        return None


def drop_collection(url: str, key: str, collection: str) -> tuple[bool, str]:
    """Cancella la collection del selftest. Qdrant su Windows, subito dopo scritture o ricerche,
    puo' rispondere 500 "failed to rename" alla delete: la collection sparisce dal registro ma la
    cartella resta su disco e RISORGE al riavvio del server. Qui: pausa prima della delete, fino
    a 3 tentativi, verifica con collection_exists; se dopo un 500 il server la dice assente lo si
    riporta come artefatto (PASS con nota), se resta presente e' FAIL dichiarato."""
    last = ""
    artifact = False
    for attempt in range(3):
        try:
            from qdrant_client import QdrantClient  # noqa: PLC0415
            from qdrant_client.http.exceptions import UnexpectedResponse  # noqa: PLC0415
            c = QdrantClient(url=url, api_key=key or None, timeout=20)
            try:
                if not c.collection_exists(collection):
                    if attempt == 0:
                        return True, "gia' assente"
                    return True, ("cancellata al tentativo %d" % (attempt + 1)) + (
                        " (500 'failed to rename' al primo tentativo: artefatto Qdrant/Windows, la cartella puo' "
                        "restare su disco e ricomparire al riavvio del server)" if artifact else "")
                time.sleep(2)   # lascia chiudere i file della collection prima del rename
                c.delete_collection(collection)
                if not c.collection_exists(collection):
                    return True, "cancellata" if attempt == 0 else f"cancellata al tentativo {attempt + 1}"
                last = "delete accettata ma la collection risulta ancora presente"
            except UnexpectedResponse as e:
                artifact = artifact or e.status_code == 500
                last = f"HTTP {e.status_code} {e.reason_phrase}"
            finally:
                c.close()
        except Exception as e:  # noqa: BLE001
            last = f"{e.__class__.__name__}: {str(e)[:120]}"
        time.sleep(2)
    return False, f"NON cancellata dopo 3 tentativi ({last}) -- cancellala a mano: {collection}"


def scan_placeholders(copy: Path) -> list[str]:
    bad: list[str] = []
    for p in copy.rglob("*"):
        if not p.is_file():
            continue
        parts = p.relative_to(copy).parts
        if any(seg in SKIP_SCAN_DIRS for seg in parts) or p.suffix == ".tmpl":
            continue
        if len(parts) > 1 and parts[0] == "scripts" and parts[1] in ("vault", "skills", "tests"):
            continue
        if p.suffix.lower() not in TEXT_EXT:
            continue
        try:
            if PH in p.read_text(encoding="utf-8", errors="ignore"):
                bad.append("/".join(parts))
        except OSError:
            continue
    return bad


def main(argv: list[str] | None = None) -> int:
    kc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Selftest del motore in una copia temporanea del brain.")
    ap.add_argument("--root", default=None)
    ap.add_argument("--qdrant-url", default=None)
    ap.add_argument("--qdrant-api-key-env", default=None, help="NOME della variabile con la chiave (mai il valore)")
    ap.add_argument("--qdrant-exe", default=None, help="binario qdrant: istanza usa-e-getta avviata dal selftest")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--keep", action="store_true", help="non cancellare la copia temporanea")
    ap.add_argument("--verbose", action="store_true", help="stampa l'output dei comandi")
    a = ap.parse_args(argv)
    rep = Report()
    t_start = time.time()
    try:
        root = kc.resolve_root(a.root)
    except kc.ConfigError as e:
        print(f"ERRORE configurazione: {e}")
        return 2
    # Il .env del brain STESSO, per path esplicito e senza sovrascrivere l'ambiente del processo:
    # serve a risolvere --qdrant-api-key-env (ambiente del processo prima, poi <root>/.env) e
    # BRAIN_ID. Il selftest non lo stampa e non lo copia (la copia temporanea esclude .env*).
    env_files = kc.load_env(root, root)
    bid = kc.brain_id(root, strict=False)
    if not bid:
        rep.add("identity", "FAIL", "BRAIN_ID assente da .env, nodo.md e brain.yaml")
        bid = "brain"
    else:
        rep.add("identity", "PASS", f"brain_id={bid} nome={kc.brain_name(root)}"
                + (f" (.env caricato: {', '.join(str(p) for p in env_files)})" if env_files else " (nessun .env)"))

    base = Path(os.environ.get("BRAIN_LAB") or (Path(tempfile.gettempdir()) / "brain-lab")) / "selftest"
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f"{bid}-", dir=str(base)))
    copy = tmp / "brain"
    qd: QdrantProc | None = None
    py = sys.executable
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RAG_", "QDRANT_")) and k not in ("BRAIN_ROOT", "BRAIN_NODE")}
    env.update({"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "BRAIN_ID": bid})
    out_all: list[str] = []

    def sh(name: str, cmd: list[str], **kw) -> tuple[int, str]:
        rc, out = run(cmd, kw.pop("env", env), kw.pop("cwd", copy), **kw)
        out_all.append(f"$ {' '.join(cmd)}\n[exit {rc}]\n{out}")
        if a.verbose:
            print(f"\n$ {' '.join(cmd)}\n[exit {rc}]\n{out}")
        return rc, out

    try:
        shutil.copytree(root, copy, ignore=COPY_IGNORE, symlinks=False)
        idx = copy / "scripts" / "rag" / "index"
        if idx.is_dir():
            shutil.rmtree(idx, ignore_errors=True)
        if not (copy / "scripts" / "kb_config.py").is_file():
            rep.add("engine", "FAIL", "scripts/kb_config.py assente: il motore non e' installato nel brain")
            raise SystemExit
        rep.add("engine", "PASS", f"copia in {copy}")
        sc = copy / "scripts"

        # 1. lint
        rc, out = sh("lint", [py, str(sc / "lint_kb.py")])
        rep.add("lint", "PASS" if rc == 0 else "FAIL", tail(out, 1 if rc == 0 else 4))

        # 2. skills YAML
        skills = [p for p in copy.rglob("SKILL.md") if ".claude" in p.parts and "scripts" not in p.parts]
        bad = []
        for p in skills:
            try:
                fm, _ = kc.read_frontmatter(p)
                if not fm.get("name") or not fm.get("description"):
                    bad.append(f"{p.parent.name}: name/description mancanti")
            except kc.FrontmatterError as e:
                bad.append(f"{p.parent.name}: {e}")
        if not skills:
            rep.add("skills_yaml", "NA", "nessuna skill in .claude/skills")
        else:
            rep.add("skills_yaml", "FAIL" if bad else "PASS", "; ".join(bad) if bad else f"{len(skills)} skill valide")

        # 3. placeholders
        left = scan_placeholders(copy)
        rep.add("placeholders", "FAIL" if left else "PASS", ", ".join(left[:5]) if left else f"zero '{PH}' residui")

        # 4. .env non tracciati (brain reale, sola lettura)
        if (root / ".git").exists():
            rc, out = sh("git-env", ["git", "-C", str(root), "ls-files", "--", ".env", ".env.*"], cwd=root)
            tracked = [ln for ln in out.splitlines() if ln.strip() and ln.strip() != ".env.example" and not ln.startswith("[")]
            rep.add("env_untracked", "FAIL" if tracked else "PASS", ", ".join(tracked) if tracked else "nessun .env tracciato")
        else:
            rep.add("env_untracked", "NA", "il brain non e' un repository git")

        # 5. llms + exact
        rc, out = sh("llms", [py, str(sc / "build_llms_txt.py")])
        if rc != 0:
            rep.add("llms_exact", "FAIL", tail(out, 3))
        else:
            rc, out = sh("exact", [py, str(sc / "brain.py"), "ask", bid, "--exact", "--json"])
            try:
                hits = json.loads(out.split("\n[stderr]")[0])["hits"]
            except Exception:  # noqa: BLE001
                hits = []
            rep.add("llms_exact", "PASS" if rc == 0 and hits else "FAIL",
                    f"{len(hits)} match per '{bid}' in {hits[0]['source'] if hits else 'nessun file'}")

        # 6. Qdrant
        url = key = None
        if a.qdrant_exe:
            try:
                qd = QdrantProc(Path(a.qdrant_exe), tmp)
                qd.start()
                url, key = qd.url, qd.key
                rep.add("qdrant", "PASS", f"istanza usa-e-getta {url} (jwt_rbac, chiave casuale)")
            except Exception as e:  # noqa: BLE001
                rep.add("qdrant", "FAIL", f"avvio Qdrant fallito: {e}")
        elif a.qdrant_url:
            url = a.qdrant_url
            key = os.environ.get(a.qdrant_api_key_env, "").strip() if a.qdrant_api_key_env else ""
            if a.qdrant_api_key_env and not key:
                rep.add("qdrant", "FAIL", f"variabile {a.qdrant_api_key_env} vuota o assente sia nell'ambiente del processo "
                                          f"sia in {root / '.env'}")
                url = None
            else:
                rep.add("qdrant", "PASS", f"server {url}" + (f" con chiave da {a.qdrant_api_key_env}" if key else " senza chiave"))
        qcheck = ["append_no_index", "ingest_first", "ingest_nochange", "closed_port", "semantic_query", "tree_isolation", "cleanup"]
        if not url:
            if not a.qdrant_exe and not a.qdrant_url:
                rep.add("qdrant", "NA", "nessun --qdrant-url/--qdrant-exe: controlli Qdrant saltati")
            for n in qcheck:
                rep.add(n, "NA", "senza Qdrant")
        else:
            coll = f"{bid}_selftest"
            qenv = {**env, "RAG_COLLECTION": coll, "QDRANT_URL": url, "QDRANT_API_KEY": key or ""}
            prof = kc.load_profile(copy)
            folder = prof.folders[0]
            # residui di un selftest precedente interrotto (o risorti al riavvio del server): via prima
            pre_ok, pre_why = drop_collection(url, key, coll)
            if pre_why != "gia' assente":
                rep.rows[-1]["detail"] += f"; residuo {coll}: {pre_why}"
            # a. append senza indice
            rc, out = sh("append", [py, str(sc / "rag" / "ingest.py"), "--append-sessions"], env=qenv)
            rep.add("append_no_index", "PASS" if rc == 1 else "FAIL", f"exit {rc}: {tail(out)}")
            # b. note finte + ingest
            stems = [s for s, *_ in FAKE_NOTES]
            for stem, title, summary, body in FAKE_NOTES:
                links = [s for s in stems if s != stem][:3]
                write_note(copy / folder / f"{stem}.md", folder, stem, title, summary, links, body)
            sh("llms2", [py, str(sc / "build_llms_txt.py")])
            rc, out = sh("ingest1", [py, str(sc / "rag" / "ingest.py"), "--all-nodes"], env=qenv)
            n_pts = point_count(url, key, coll)
            # CREATE su collection assente -> "RICOSTRUITO"; se un residuo non si e' potuto
            # cancellare, l'ingest fa il bootstrap senza wipe (dichiarato nell'output)
            rep.add("ingest_first", "PASS" if rc == 0 and ("RICOSTRUITO" in out or "bootstrap" in out)
                    and (n_pts or 0) > 0 else "FAIL", f"exit {rc}, {n_pts} punti in {coll}: {tail(out)}")
            # c. zero cambi
            rc, out = sh("ingest2", [py, str(sc / "rag" / "ingest.py"), "--all-nodes"], env=qenv)
            rep.add("ingest_nochange", "PASS" if rc == 0 and "nessuna nota cambiata" in out else "FAIL", f"exit {rc}: {tail(out)}")
            # d. porta chiusa
            before = point_count(url, key, coll)
            cenv = {**qenv, "QDRANT_URL": f"http://127.0.0.1:{closed_port()}"}
            rc, out = sh("closed", [py, str(sc / "rag" / "ingest.py"), "--all-nodes"], env=cenv)
            after = point_count(url, key, coll)
            rep.add("closed_port", "PASS" if rc == 2 and before == after and before is not None else "FAIL",
                    f"exit {rc}, punti prima/dopo {before}/{after}: {tail(out)}")
            # e. semantica
            q, expected = SEMANTIC_Q
            rc, out = sh("semantic", [py, str(sc / "brain.py"), "ask", q, "--semantic", "--json", "--k", "5"], env=qenv)
            try:
                res = json.loads(out.split("\n[stderr]")[0])
                top = res["hits"][0]["stem"] if res["hits"] else None
                conf = res.get("confidence")
            except Exception:  # noqa: BLE001
                top, conf = None, None
            rep.add("semantic_query", "PASS" if top == expected else "FAIL", f"top={top} atteso={expected} confidenza={conf}")
            # f. albero: due nodi finti, zero hit del fratello
            for np_, items in TREE_NODES.items():
                nd = copy / np_
                kc.write_text(nd / "nodo.md", f"---\nnome: \"{np_.split('/')[-1]}\"\nkind: brand\nnode_path: {np_}\n---\n\n# {np_}\n")
                for stem, title, summary, body in items:
                    write_note(nd / folder / f"{stem}.md", folder, stem, title, summary, stems[:3], body)
            rc, out = sh("ingest3", [py, str(sc / "rag" / "ingest.py"), "--all-nodes"], env=qenv)
            tq, from_node, other = TREE_Q
            rc2, out2 = sh("tree", [py, str(sc / "brain.py"), "ask", tq, "--semantic", "--json", "--k", "50",
                                    "--node", from_node, "--no-expand"], env=qenv)
            try:
                res = json.loads(out2.split("\n[stderr]")[0])
                hits = res["hits"]
                leak = [h for h in hits if h.get("node_path") == other]
                doms = sorted({h.get("node_path") for h in hits})
            except Exception:  # noqa: BLE001
                hits, leak, doms = [], None, []
            rep.add("tree_isolation", "PASS" if rc == 0 and leak == [] and hits else "FAIL",
                    f"ingest exit {rc}; {len(hits)} hit dal nodo {from_node} (k=50), {len(leak) if leak is not None else '?'} del fratello {other}; domini {doms}")
            # g. cleanup
            ok, why = drop_collection(url, key, coll)
            rep.add("cleanup", "PASS" if ok else "FAIL", f"collection {coll}: {why}")
    except SystemExit:
        pass
    except Exception as e:  # noqa: BLE001
        rep.add("selftest", "FAIL", f"eccezione {e.__class__.__name__}: {e}")
    finally:
        if qd is not None:
            qd.stop()
        if not a.keep:
            shutil.rmtree(tmp, ignore_errors=True)

    result = {"root": str(root), "brain_id": bid, "checks": rep.rows, "fails": rep.fails,
              "seconds": round(time.time() - t_start, 1), "copy": str(tmp) if a.keep else None}
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"selftest di {root} (brain_id {bid}) -- copia temporanea{' conservata in ' + str(tmp) if a.keep else ''}")
        print(rep.table())
        print(f"durata {result['seconds']}s -> {'OK' if rep.fails == 0 else 'FAIL'}")
    return 0 if rep.fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
