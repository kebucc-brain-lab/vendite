#!/usr/bin/env python3
"""dream.py -- revisione della memoria automatica del brain (Brain Builder v3.2, SPEC par. 2.6-2.7 e par. 12).

    python scripts/dream.py paths         [--root R] [--node N] [--json]
    python scripts/dream.py memory-report [--root R] [--node N] [--json] [--content]
    python scripts/dream.py new-proposal  [--root R] [--node N] [--sessions-read N]
    python scripts/dream.py apply         [--root R] [--node N] [--proposal FILE] [--dry-run]
    python scripts/dream.py schedule-cmd  [--root R] [--node N] [--day MON] [--time 09:00] [--platform auto|windows|posix]

Sostituisce la parte "a mano" delle skill dream dell'archivio (`cp -r`, `$(date)`, modifiche dirette della
memoria): la memoria cambia SOLO con `apply` e SOLO per le voci spuntate `[x]` della proposta.

- Nodo corrente: `--node` > nodo.md piu' vicino alla cwd > radice. Uno script installato in `<root>/scripts/`
  lavora solo sulla propria radice (`--root` fuori = exit 3, come sessions.py).
- Memory dir AUTOREVOLE: `autoMemoryDirectory` del `.claude/settings.json` del nodo. `settings.json` assente o
  corrotto, chiave assente, oppure `settings.local.json` che ne dichiara una diversa: `paths` lo segnala,
  `new-proposal` e `apply` si fermano (exit 2). Nessun fallback silenzioso in scrittura (i comandi di sola
  lettura ripiegano su `BRAIN_MEMORY_DIR`, poi `<nodo>/.memory`, e lo dicono).
- Proposte separate per nodo: `<root>/workspace/dream/<node-key>/<YYYY-MM-DD>-<HHMMSS>.md` (node-key: `root`
  oppure node_path con `/` -> `__`), gitignorate. La prima riga e' l'intestazione macchina
  `<!-- dream-meta {json} -->` (brain_id, node_path, memory_dir, review_id, sha256 di ogni file di memoria al
  momento della review), scritta da `new-proposal`: `apply` la verifica.
- Backup prima di qualunque scrittura: copia completa della memory dir in
  `<root>/.memory-backup/<node-key>/<YYYYMMDD-HHMMSS>/` (`shutil.copytree(symlinks=True)`); senza modifiche
  da fare, nessun backup. Link/junction nella memory dir o memory dir condivisa con un altro nodo = exit 2.
- Gruppi atomici: le voci con lo stesso prefisso (`m1` e `m1r`, `c1-tieni`/`c1-correggi`) si applicano tutte o
  nessuna; validazione completa prima di scrivere; registro (`Voce: <id>`) scritto dopo ogni gruppo.
- `schedule-cmd` STAMPA il comando per Task Scheduler / crontab che lancerebbe la review: non lo registra.
  `apply` non si schedula mai.

Exit di `apply`: 0 nessun errore, 1 almeno una voce in errore (le altre restano applicate), 2 proposta assente,
illeggibile o di un altro brain/nodo/memoria, oppure memoria non autorevole/insicura; 3 `--root` fuori dal brain.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_config as kc  # noqa: E402
from secrets_scan import find_secrets, mask_secrets  # noqa: E402
from sessions import RootOutside, bound_root, canon, is_within, EXIT_ROOT_OUTSIDE  # noqa: E402

MAX_LINES = 200
MAX_BYTES = 25 * 1024
WARN_RATIO = 0.8
CONTENT_LIMIT = 200 * 1024
INDEX_NAME = "MEMORY.md"
ACTIONS = ("AGGIUNGI", "AGGIORNA", "RIMUOVI", "TIENI", "VAULT")
WRITE_ACTIONS = ("AGGIUNGI", "AGGIORNA", "RIMUOVI")
MEMORY_TYPES = ("user", "feedback", "project", "reference")
META_PREFIX = "<!-- dream-meta "
META_SUFFIX = " -->"

ITEM_RE = re.compile(r"^\s*- \[(?P<mark>[ xX])\] \{(?P<id>[A-Za-z0-9_-]+)\}\s+(?P<action>[A-Z]+)\b(?P<rest>.*)$")
FENCE_RE = re.compile(r"^(?P<indent>\s*)(?P<ticks>`{3,})memoria\s*$")
HEADING_RE = re.compile(r"^#{1,6}\s")
APPLIED_RE = re.compile(r"^##\s+Applicato\b")
VOCI_RE = re.compile(r"^Voc[ei]:\s*(?P<ids>.*)$")
FILE_RE = re.compile(r"`([^`]+)`")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.md$")
LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
CONFLICT_RE = re.compile(r"^(?P<key>c\d+)-(?P<side>tieni|correggi)$")
GROUP_RE = re.compile(r"^([A-Za-z]+\d+)")
PROPOSAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{6}(?:-\d+)?\.md$")
DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
DAY_NAME = {"MON": "Monday", "TUE": "Tuesday", "WED": "Wednesday", "THU": "Thursday", "FRI": "Friday",
            "SAT": "Saturday", "SUN": "Sunday"}
CRON_DAY = {"MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6, "SUN": 0}


class Stop(Exception):
    """Condizione che ferma il comando con exit 2 (messaggio pronto per l'utente)."""


# --------------------------------------------------------------------------- utilita'

def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_link(p: Path | str) -> bool:
    """Symlink, junction o reparse point (Windows)."""
    try:
        st = os.lstat(p)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    if getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        return True
    isj = getattr(os.path, "isjunction", None)
    return bool(isj and isj(p))


def links_inside(d: Path) -> list[str]:
    found: list[str] = []
    if not d.is_dir():
        return found
    stack = [d]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for e in entries:
            if is_link(e.path):
                found.append(Path(e.path).relative_to(d).as_posix())
            elif e.is_dir(follow_symlinks=False):
                stack.append(Path(e.path))
    return sorted(found)


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _append(p: Path, text: str) -> None:
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass


# --------------------------------------------------------------------------- radice, nodo, memoria

def resolve(a) -> tuple[Path, kc.Node]:
    root = bound_root(a.root)
    node = kc.current_node(root, override=a.node)
    return root, node


@dataclass
class MemInfo:
    path: Path
    source: str
    problems: list[str] = field(default_factory=list)

    @property
    def authoritative(self) -> bool:
        return self.source == "autoMemoryDirectory" and not self.problems


def _declared(settings: Path) -> tuple[str | None, str]:
    """(autoMemoryDirectory dichiarato o None, problema o '')."""
    try:
        data = json.loads(kc.read_text(settings))
    except (OSError, ValueError) as e:
        return None, "%s corrotto o illeggibile (%s)" % (settings.name, e.__class__.__name__)
    if not isinstance(data, dict):
        return None, "%s non e' un oggetto JSON" % settings.name
    amd = data.get("autoMemoryDirectory")
    if isinstance(amd, str) and amd.strip():
        return os.path.expanduser(amd.strip()), ""
    return None, ""


def memory_info(node: kc.Node) -> MemInfo:
    problems: list[str] = []
    sp = node.dir / ".claude" / "settings.json"
    declared = None
    if not sp.is_file():
        problems.append("%s assente" % sp.as_posix())
    else:
        declared, prob = _declared(sp)
        if prob:
            problems.append(prob)
        elif declared is None:
            problems.append("autoMemoryDirectory assente in %s" % sp.as_posix())
    lp = node.dir / ".claude" / "settings.local.json"
    if lp.is_file():
        local, prob = _declared(lp)
        if prob:
            problems.append(prob)
        elif local is not None and (declared is None or canon(local) != canon(declared)):
            problems.append("settings.local.json dichiara un autoMemoryDirectory diverso (%s): Claude userebbe "
                            "quello, non %s" % (local, declared or "(nessuno)"))
    if declared:
        return MemInfo(Path(declared), "autoMemoryDirectory", problems)
    env = os.environ.get("BRAIN_MEMORY_DIR", "").strip()
    if env:
        return MemInfo(Path(env), "BRAIN_MEMORY_DIR (ripiego, non autorevole)", problems)
    return MemInfo(node.dir / ".memory", "default (ripiego, non autorevole)", problems)


def other_nodes_sharing(root: Path, node: kc.Node, mem: Path) -> list[str]:
    target = canon(mem)
    out = []
    for n in kc.iter_nodes(root):
        if n.path == node.path:
            continue
        sp = n.dir / ".claude" / "settings.json"
        if sp.is_file():
            d, _p = _declared(sp)
            if d and canon(d) == target:
                out.append(n.path)
    return out


def require_safe_memory(root: Path, node: kc.Node) -> MemInfo:
    """Memoria autorevole, non link, senza link dentro, non condivisa con altri nodi. Altrimenti Stop."""
    mi = memory_info(node)
    if not mi.authoritative:
        raise Stop("memory dir non autorevole per il nodo %s: %s" % (node.path, "; ".join(mi.problems) or mi.source))
    if is_link(mi.path):
        raise Stop("la memory dir %s e' un link/junction: rifiutata" % mi.path.as_posix())
    bad = links_inside(mi.path)
    if bad:
        raise Stop("nella memory dir ci sono link/junction (%s): rifiutata" % ", ".join(bad[:5]))
    shared = other_nodes_sharing(root, node, mi.path)
    if shared:
        raise Stop("la memory dir %s e' dichiarata anche dai nodi %s: memoria condivisa, rifiutata"
                   % (mi.path.as_posix(), ", ".join(shared)))
    return mi


def node_key(node: kc.Node) -> str:
    return kc.node_key(node.path)


def proposals_dir(root: Path, node: kc.Node) -> Path:
    return root / "workspace" / "dream" / node_key(node)


def backups_dir(root: Path, node: kc.Node) -> Path:
    return root / ".memory-backup" / node_key(node)


def list_proposals(pdir: Path) -> list[Path]:
    if not pdir.is_dir():
        return []
    return sorted(p for p in pdir.iterdir() if p.is_file() and PROPOSAL_RE.match(p.name))


def latest_proposal(pdir: Path) -> Path | None:
    props = list_proposals(pdir)
    return props[-1] if props else None


def brain_id_of(root: Path) -> str:
    return kc.brain_id(root, strict=False) or root.name


def cmd_paths(a) -> int:
    root, node = resolve(a)
    mi = memory_info(node)
    pdir = proposals_dir(root, node)
    latest = latest_proposal(pdir)
    info = {
        "brain_id": brain_id_of(root),
        "root": root.as_posix(),
        "node_path": node.path,
        "node_key": node_key(node),
        "node_dir": node.dir.as_posix(),
        "memory_dir": mi.path.as_posix(),
        "memory_source": mi.source,
        "memory_authoritative": mi.authoritative,
        "memory_problems": mi.problems,
        "proposals_dir": pdir.as_posix(),
        "latest_proposal": latest.as_posix() if latest else None,
        "backup_dir": backups_dir(root, node).as_posix(),
    }
    if a.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    rows = [("brain", info["brain_id"]), ("radice", info["root"]), ("nodo rilevato", info["node_path"]),
            ("cartella del nodo", info["node_dir"]),
            ("memoria", info["memory_dir"]), ("origine memoria", info["memory_source"]),
            ("memoria autorevole", "si" if mi.authoritative else "NO (apply si fermerebbe)"),
            ("cartella proposte", info["proposals_dir"]),
            ("ultima proposta", info["latest_proposal"] or "(nessuna)"), ("backup", info["backup_dir"])]
    for k, v in rows:
        print("%s: %s" % (k, v))
    for p in mi.problems:
        print("PROBLEMA: " + p)
    return 0


# --------------------------------------------------------------------------- memory-report

def index_targets(line: str) -> list[str]:
    """Basename dei file a cui punta una riga d'indice (`[testo](file.md)`)."""
    out = []
    for t in LINK_RE.findall(line):
        t = t.split("#", 1)[0].replace("\\", "/")
        if t:
            out.append(t.rsplit("/", 1)[-1])
    return out


def size_status(lines: int, size: int) -> str:
    if lines > MAX_LINES or size > MAX_BYTES:
        return "over"
    if lines > MAX_LINES * WARN_RATIO or size > MAX_BYTES * WARN_RATIO:
        return "warn"
    return "ok"


STATUS_IT = {"ok": "ok", "warn": "ATTENZIONE: oltre l'80% della soglia",
             "over": "OLTRE LA SOGLIA: la parte in eccesso non si carica", "absent": "assente (memoria nuova)"}


def memory_type(fm: dict) -> str:
    """`type` al livello principale (formato canonico); in lettura accetta anche `metadata.type`."""
    meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    return kc.fm_str(fm.get("type") or meta.get("type"))


def memory_report(mem: Path, with_content: bool = False) -> dict:
    rep: dict = {"memory_dir": mem.as_posix(), "exists": mem.is_dir(), "files": [], "index_missing": [],
                 "not_indexed": [], "content_truncated": False, "secrets_masked": 0,
                 "memory_md": {"exists": False, "path": (mem / INDEX_NAME).as_posix(), "lines": 0, "bytes": 0,
                               "kb": 0.0, "max_lines": MAX_LINES, "max_kb": MAX_BYTES // 1024, "status": "absent"}}
    if not mem.is_dir():
        return rep
    budget = [CONTENT_LIMIT]

    def take(text: str) -> str | None:
        masked, n = mask_secrets(text)
        rep["secrets_masked"] += n
        size = len(masked.encode("utf-8"))
        if size > budget[0]:
            rep["content_truncated"] = True
            return None
        budget[0] -= size
        return masked

    names = sorted(p.name for p in mem.iterdir()
                   if p.is_file() and p.suffix.lower() == ".md" and p.name != INDEX_NAME)
    idx = mem / INDEX_NAME
    indexed: set[str] = set()
    if idx.is_file():
        raw = idx.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        n_lines = len(text.splitlines())
        rep["memory_md"].update({"exists": True, "lines": n_lines, "bytes": len(raw), "kb": round(len(raw) / 1024, 1),
                                 "status": size_status(n_lines, len(raw)), "sha256": sha256_file(idx)})
        if with_content:
            rep["memory_md"]["content"] = take(text)
        for line in text.splitlines():
            for t in index_targets(line):
                indexed.add(t)
                if not (mem / t).is_file() and t not in rep["index_missing"]:
                    rep["index_missing"].append(t)
    for name in names:
        p = mem / name
        text = kc.read_text(p)
        entry = {"name": name, "path": p.as_posix(), "type": "", "description": "", "lines": len(text.splitlines()),
                 "sha256": sha256_file(p), "frontmatter_error": "", "type_location": ""}
        try:
            fm, _ = kc.read_frontmatter(text)
            entry["type"] = memory_type(fm)
            entry["type_location"] = "type" if fm.get("type") else ("metadata.type" if entry["type"] else "")
            entry["description"] = kc.fm_str(fm.get("description"))
        except kc.FrontmatterError as e:
            entry["frontmatter_error"] = str(e)
        if with_content:
            entry["content"] = take(text)
        rep["files"].append(entry)
    rep["not_indexed"] = [n for n in names if n not in indexed]
    return rep


def cmd_memory_report(a) -> int:
    _root, node = resolve(a)
    mi = memory_info(node)
    rep = memory_report(mi.path, with_content=a.content)
    rep["memory_source"] = mi.source
    rep["memory_problems"] = mi.problems
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    print("Nodo rilevato: %s (%s)" % (node.path, node.dir.as_posix()))
    print("Memoria: %s (da %s)" % (rep["memory_dir"], mi.source))
    for p in mi.problems:
        print("PROBLEMA: " + p)
    if not rep["exists"]:
        print("La cartella non esiste ancora: memoria nuova, niente da confrontare (e' normale all'inizio).")
        return 0
    mm = rep["memory_md"]
    if mm["exists"]:
        print("MEMORY.md: %d righe / %.1f KB (si caricano fino a %d righe / %d KB) - %s"
              % (mm["lines"], mm["kb"], MAX_LINES, MAX_BYTES // 1024, STATUS_IT[mm["status"]]))
    else:
        print("MEMORY.md: " + STATUS_IT["absent"])
    print("File di memoria: %d" % len(rep["files"]))
    for f in rep["files"]:
        extra = (" [frontmatter non valido: %s]" % f["frontmatter_error"]) if f["frontmatter_error"] else ""
        if f["type_location"] == "metadata.type":
            extra += " [type sotto metadata: il formato canonico lo vuole al livello principale]"
        print("- %s  [%s]  %d righe  - %s%s" % (f["name"], f["type"] or "?", f["lines"],
                                                 f["description"] or "(senza description)", extra))
    print("Voci d'indice senza file: " + (", ".join(rep["index_missing"]) or "(nessuna)"))
    print("File non indicizzati in MEMORY.md: " + (", ".join(rep["not_indexed"]) or "(nessuno)"))
    if a.content:
        print("\n# Contenuto (mascherato; limite %d KB; provenienza = path)" % (CONTENT_LIMIT // 1024))
        blocks = ([(mm["path"], mm.get("content"))] if mm["exists"] else []) + [(f["path"], f.get("content")) for f in rep["files"]]
        for path, content in blocks:
            print("\n=== %s ===" % path)
            print(content.rstrip("\n") if content is not None else "(omesso: limite di contenuto raggiunto)")
        print("\n%d segreti mascherati%s" % (rep["secrets_masked"], "; contenuto TRONCATO al limite" if rep["content_truncated"] else ""))
    return 0


# --------------------------------------------------------------------------- new-proposal

def snapshot_hashes(mem: Path) -> dict[str, str]:
    if not mem.is_dir():
        return {}
    return {p.name: sha256_file(p) for p in sorted(mem.iterdir()) if p.is_file() and p.suffix.lower() == ".md"}


def cmd_new_proposal(a) -> int:
    root, node = resolve(a)
    mi = require_safe_memory(root, node)
    pdir = proposals_dir(root, node)
    for d in (root / "workspace" / "dream", pdir):
        if is_link(d):
            raise Stop("%s e' un link/junction: rifiutata" % d.as_posix())
    now = dt.datetime.now()
    review_id = now.strftime("%Y%m%d-%H%M%S")
    path = pdir / (now.strftime("%Y-%m-%d-%H%M%S") + ".md")
    n = 2
    while path.exists():
        path = pdir / ("%s-%d.md" % (now.strftime("%Y-%m-%d-%H%M%S"), n))
        n += 1
    bid = brain_id_of(root)
    meta = {"brain_id": bid, "node_path": node.path, "memory_dir": mi.path.as_posix(), "review_id": review_id,
            "created": now.isoformat(timespec="seconds"), "hashes": snapshot_hashes(mi.path)}
    rep = memory_report(mi.path)
    mm = rep["memory_md"]
    salute = ("MEMORY.md: %d righe / %.1f KB (si caricano fino a %d righe / %d KB) · File di memoria: %d"
              % (mm["lines"], mm["kb"], MAX_LINES, MAX_BYTES // 1024, len(rep["files"]))) if mm["exists"] else \
        ("MEMORY.md: assente (memoria nuova) · File di memoria: %d" % len(rep["files"]))
    sessions = str(a.sessions_read) if a.sessions_read is not None else "?"
    nodo = "" if node.is_root else " (nodo %s)" % node.path
    text = (META_PREFIX + json.dumps(meta, ensure_ascii=False, sort_keys=True) + META_SUFFIX + "\n"
            "# Dream — %s — brain %s%s\n" % (now.date().isoformat(), bid, nodo) +
            "Sessioni lette: %s · Memoria: %s\n\n" % (sessions, mi.path.as_posix()) +
            "## Da aggiungere (mai scritto)\n\n"
            "## Doppioni\n\n"
            "## Contraddizioni (decidi tu)\n\n"
            "## Probabilmente morto\n\n"
            "## Per il vault (via %s-feed)\n\n" % bid +
            "## Salute\n" + salute + "\n")
    _write(path, text)
    print(path.as_posix())
    return 0


# --------------------------------------------------------------------------- proposta: parsing

@dataclass
class Item:
    id: str
    action: str
    checked: bool
    rest: str
    line_no: int
    filename: str = ""
    block: str | None = None
    index_line: str | None = None
    error: str = ""

    @property
    def label(self) -> str:
        return "{" + self.id + "} " + self.action + ((" " + self.filename) if self.filename else "")

    @property
    def group(self) -> str:
        m = GROUP_RE.match(self.id)
        return m.group(1) if m else self.id


def parse_meta(text: str) -> dict | None:
    first = text.lstrip("\ufeff").split("\n", 1)[0].strip()
    if not (first.startswith(META_PREFIX) and first.endswith(META_SUFFIX)):
        return None
    try:
        data = json.loads(first[len(META_PREFIX):-len(META_SUFFIX)])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_proposal(text: str) -> tuple[list[Item], set[str]]:
    """Voci (righe `- [ ] {id} AZIONE ...` con il loro blocco ```memoria e la riga `Indice:`) e id gia' applicati
    (righe `Voce:`/`Voci:` delle sezioni `## Applicato`)."""
    lines = text.splitlines()
    items: list[Item] = []
    applied: set[str] = set()
    cur: Item | None = None
    in_applied = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if HEADING_RE.match(line):
            cur = None
            in_applied = bool(APPLIED_RE.match(line))
            i += 1
            continue
        if in_applied:
            m = VOCI_RE.match(line.strip())
            if m:
                applied |= {x.strip() for x in m.group("ids").split(",") if x.strip() and not x.strip().startswith("(")}
            i += 1
            continue
        m = ITEM_RE.match(line)
        if m:
            fm = FILE_RE.search(m.group("rest"))
            cur = Item(id=m.group("id"), action=m.group("action"), checked=m.group("mark") in "xX",
                       rest=m.group("rest").strip(), line_no=i + 1, filename=fm.group(1).strip() if fm else "")
            if cur.action not in ACTIONS:
                cur.error = "azione sconosciuta %r (ammesse: %s)" % (cur.action, ", ".join(ACTIONS))
            items.append(cur)
            i += 1
            continue
        fence = FENCE_RE.match(line)
        if fence and cur is not None:
            ticks, ind = fence.group("ticks"), len(fence.group("indent"))
            body: list[str] = []
            closed = False
            i += 1
            while i < len(lines):
                cl = lines[i]
                if cl.strip() == ticks:
                    closed = True
                    i += 1
                    break
                body.append(cl[ind:] if cl[:ind].strip() == "" else cl.lstrip())
                i += 1
            if cur.block is not None:
                cur.error = cur.error or "due blocchi memoria nella stessa voce"
            cur.block = "\n".join(body).strip("\n") + "\n"
            if not closed:
                cur.error = cur.error or "blocco memoria non chiuso"
            continue
        if cur is not None:
            s = line.strip()
            if s.startswith("Indice:"):
                v = s[len("Indice:"):].strip()
                if len(v) >= 2 and v[0] == "`" and v[-1] == "`":
                    v = v[1:-1].strip()
                cur.index_line = v
        i += 1
    return items, applied


def valid_name(name: str) -> str:
    """'' se il nome file e' un basename ammesso dentro la memory dir, altrimenti il motivo."""
    if not name:
        return "nome file assente (atteso fra backtick)"
    if "/" in name or "\\" in name or ":" in name or ".." in name or name.startswith("."):
        return "nome file non ammesso %r: solo basename, niente separatori, '..' o path assoluti" % name
    if not NAME_RE.match(name):
        return "nome file non ammesso %r: atteso [A-Za-z0-9._-]+.md" % name
    if name == INDEX_NAME:
        return "MEMORY.md non si modifica con una voce: si aggiorna solo tramite le righe Indice"
    return ""


def validate_block(block: str) -> str:
    """'' se il blocco e' una memoria valida: frontmatter con name, description e type (livello principale)."""
    try:
        fm, body = kc.read_frontmatter(block)
    except kc.FrontmatterError as e:
        return "frontmatter del blocco non valido: %s" % e
    if not fm:
        return "il blocco non ha frontmatter (--- name/description/type ---)"
    missing = [k for k in ("name", "description") if not kc.fm_str(fm.get(k))]
    if missing:
        return "frontmatter del blocco senza %s" % ", ".join(missing)
    t = kc.fm_str(fm.get("type"))
    if not t:
        return "frontmatter del blocco senza `type:` al livello principale"
    if t not in MEMORY_TYPES:
        return "type %r non ammesso (ammessi: %s)" % (t, ", ".join(MEMORY_TYPES))
    if find_secrets(block):
        return "il blocco contiene un segreto non mascherato"
    return ""


def target_ok(mem: Path, name: str) -> str:
    p = mem / name
    if is_link(p):
        return "%s e' un link/junction: rifiutato" % name
    if not is_within(canon(p), canon(mem)) or canon(p) == canon(mem):
        return "%s uscirebbe dalla memory dir" % name
    return ""


def set_index_line(lines: list[str], filename: str, new_line: str) -> list[str]:
    out: list[str] = []
    replaced = False
    for line in lines:
        if filename in index_targets(line):
            if not replaced:
                out.append(new_line)
                replaced = True
            continue
        out.append(line)
    if not replaced:
        out.append(new_line)
    return out


def drop_index_line(lines: list[str], filename: str) -> list[str]:
    return [line for line in lines if filename not in index_targets(line)]


def make_backup(mem: Path, bdir: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = bdir / stamp
    n = 2
    while dest.exists():
        dest = bdir / ("%s-%d" % (stamp, n))
        n += 1
    bdir.mkdir(parents=True, exist_ok=True)
    if mem.is_dir():
        shutil.copytree(mem, dest, symlinks=True)
    else:
        dest.mkdir(parents=True)
    return dest


# --------------------------------------------------------------------------- apply

def cmd_apply(a) -> int:
    root, node = resolve(a)
    mi = require_safe_memory(root, node)
    mem = mi.path
    bid = brain_id_of(root)
    pdir = proposals_dir(root, node)
    prop = Path(a.proposal).resolve() if a.proposal else latest_proposal(pdir)
    if prop is None or not prop.is_file():
        raise Stop("nessuna proposta da applicare (%s). Prima una review: /%s-dream review"
                   % (a.proposal or pdir.as_posix(), bid))
    if is_link(prop):
        raise Stop("la proposta %s e' un link: rifiutata" % prop.as_posix())
    bdir = backups_dir(root, node)
    for d in (root / ".memory-backup", bdir):
        if is_link(d):
            raise Stop("%s e' un link/junction: backup rifiutato" % d.as_posix())
    try:
        text = prop.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise Stop("proposta illeggibile %s: %s" % (prop, e)) from e
    meta = parse_meta(text)
    if meta is None:
        raise Stop("proposta senza intestazione dream-meta valida (prima riga): rigenerala con /%s-dream review" % bid)
    mism = []
    if meta.get("brain_id") != bid:
        mism.append("brain %r invece di %r" % (meta.get("brain_id"), bid))
    if meta.get("node_path") != node.path:
        mism.append("nodo %r invece di %r" % (meta.get("node_path"), node.path))
    if not isinstance(meta.get("memory_dir"), str) or canon(meta["memory_dir"]) != canon(mem):
        mism.append("memoria %r invece di %r" % (meta.get("memory_dir"), mem.as_posix()))
    if mism:
        raise Stop("la proposta non e' di questo brain/nodo/memoria: " + "; ".join(mism))
    hashes = meta.get("hashes") if isinstance(meta.get("hashes"), dict) else {}
    items, applied = parse_proposal(text)

    # ---- validazione completa, prima di qualunque scrittura
    seen: dict[str, int] = {}
    for it in items:
        seen[it.id] = seen.get(it.id, 0) + 1
    sides: dict[str, set[str]] = {}
    for it in items:
        m = CONFLICT_RE.match(it.id)
        if m and it.checked and it.id not in applied:
            sides.setdefault(m.group("key"), set()).add(m.group("side"))
    conflicts = {k for k, s in sides.items() if s == {"tieni", "correggi"}}
    existing = {p.name for p in mem.iterdir() if p.is_file()} if mem.is_dir() else set()
    status: dict[int, str] = {}
    todo: list[Item] = []
    for it in items:
        k = id(it)
        if not it.checked:
            status[k] = "saltata (non spuntata)"
            continue
        if it.id in applied:
            status[k] = "saltata (gia' applicata)"
            continue
        err = it.error
        cm = CONFLICT_RE.match(it.id)
        if not err and seen[it.id] > 1:
            err = "id duplicato nella proposta"
        if not err and cm and cm.group("key") in conflicts:
            err = ("{%s-tieni} e {%s-correggi} entrambe spuntate: nessuna modifica per %s"
                   % (cm.group("key"), cm.group("key"), cm.group("key")))
        if not err and it.action in WRITE_ACTIONS:
            err = valid_name(it.filename) or target_ok(mem, it.filename)
        if not err and it.action in ("AGGIUNGI", "AGGIORNA"):
            err = "manca il blocco ```memoria con il contenuto completo" if it.block is None else validate_block(it.block)
        if not err and it.action == "AGGIUNGI" and it.filename in existing:
            err = "%s esiste gia': AGGIUNGI non sovrascrive (usa AGGIORNA)" % it.filename
        if not err and it.action in ("AGGIORNA", "RIMUOVI"):
            if it.filename not in existing:
                err = "%s non esiste nella memoria" % it.filename
            elif hashes.get(it.filename) != sha256_file(mem / it.filename):
                err = "%s cambiato dopo la review (hash diverso): rifai la review" % it.filename
        if not err and it.index_line is not None and it.action in ("AGGIUNGI", "AGGIORNA"):
            if it.filename not in index_targets(it.index_line):
                err = "la riga Indice non punta a %s" % it.filename
        if err:
            status[k] = "errore: " + err
            continue
        if it.action == "VAULT":
            status[k] = "da passare a %s-feed (apply non tocca il vault)" % bid
            continue
        todo.append(it)
        if it.action == "AGGIUNGI":
            existing.add(it.filename)
        elif it.action == "RIMUOVI":
            existing.discard(it.filename)
    # gruppi atomici: un errore in una voce spuntata blocca tutte le voci spuntate dello stesso gruppo
    failed_groups: dict[str, str] = {}
    for it in items:
        if status.get(id(it), "").startswith("errore") and it.checked and it.id not in applied:
            failed_groups.setdefault(it.group, it.id)
    for it in list(todo):
        if it.group in failed_groups:
            status[id(it)] = "errore: gruppo %s non applicato (errore in {%s})" % (it.group, failed_groups[it.group])
            todo.remove(it)
    for it in todo:
        status[id(it)] = "da applicare (dry-run)" if a.dry_run else "in corso"

    writes = [it for it in todo if it.action in WRITE_ACTIONS]
    backup: Path | None = None
    if todo and not a.dry_run:
        if writes:
            try:
                backup = make_backup(mem, bdir)
            except OSError as e:
                raise Stop("backup fallito (%s): nessuna modifica applicata" % e) from e
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _append(prop, ("" if text.endswith("\n") else "\n") + "\n## Applicato %s\nBackup: %s\n"
                % (stamp, backup.as_posix() if backup else "nessuno (nessuna modifica)"))
        idx = mem / INDEX_NAME
        idx_text = kc.read_text(idx) if idx.is_file() else None
        idx_lines = idx_text.splitlines() if idx_text is not None else []
        groups: list[str] = []
        for it in todo:
            if it.group not in groups:
                groups.append(it.group)
        for g in groups:
            members = [it for it in todo if it.group == g]
            touched: list[str] = []
            new_lines = list(idx_lines)
            try:
                for it in members:
                    if it.action not in WRITE_ACTIONS:
                        continue
                    why = target_ok(mem, it.filename)
                    if why:
                        raise OSError(why)
                    target = mem / it.filename
                    touched.append(it.filename)
                    if it.action == "RIMUOVI":
                        target.unlink()
                        new_lines = drop_index_line(new_lines, it.filename)
                    else:
                        _write(target, it.block or "")
                        if it.index_line:
                            new_lines = set_index_line(new_lines, it.filename, it.index_line)
                if new_lines != idx_lines:
                    if is_link(idx):
                        raise OSError("MEMORY.md e' un link: rifiutato")
                    _write(idx, "\n".join(new_lines) + "\n")
            except OSError as e:
                for name in touched:                    # ripristino del gruppo dal backup
                    src = (backup / name) if backup else None
                    try:
                        if src is not None and src.is_file():
                            shutil.copyfile(src, mem / name)
                        elif (mem / name).exists():
                            (mem / name).unlink()
                    except OSError:
                        pass
                if new_lines != idx_lines:
                    try:
                        if idx_text is None:
                            if idx.exists():
                                idx.unlink()
                        else:
                            _write(idx, idx_text)
                    except OSError:
                        pass
                for it in members:
                    status[id(it)] = "errore: gruppo %s non applicato (%s); file del gruppo ripristinati" % (g, e)
                continue
            idx_lines = new_lines
            idx_text = "\n".join(idx_lines) + "\n" if idx.is_file() else idx_text
            _append(prop, "".join("Voce: %s\n" % it.id for it in members))   # registro dopo ogni gruppo
            for it in members:
                status[id(it)] = "applicata"

    errors = 0
    for it in items:
        s = status[id(it)]
        if s.startswith("errore"):
            errors += 1
        print(it.label + ": " + s)
    if not items:
        print("(nessuna voce nella proposta)")
    if a.dry_run:
        print("dry-run: nessuna modifica, nessun backup")
    elif backup is not None:
        print("backup: " + backup.as_posix())
    else:
        print("backup: nessuno (nessuna modifica da fare)")
    print("proposta: " + prop.as_posix())
    return 1 if errors else 0


# --------------------------------------------------------------------------- schedule-cmd

def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _fwd(p: Path) -> str:
    s = str(p).replace("\\", "/")
    if re.match(r"^[a-zA-Z]:", s):
        s = s[0].upper() + s[1:]
    return s


def _rule_path(p: Path) -> str:
    """Forma dei path assoluti nelle regole dei permessi di Claude: `D:/x` -> `//d/x`; `/home/x` -> `//home/x`."""
    s = _fwd(p)
    m = re.match(r"^([A-Za-z]):/?(.*)$", s)
    if m:
        return "//" + m.group(1).lower() + "/" + m.group(2).strip("/")
    return "/" + s


def cmd_schedule(a) -> int:
    root, node = resolve(a)
    bid = brain_id_of(root)
    day = a.day.upper()
    if day not in DAYS:
        raise Stop("--day ammette %s" % ", ".join(DAYS))
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", a.time):
        raise Stop("--time nel formato HH:MM (24 ore)")
    plat = a.platform if a.platform != "auto" else ("windows" if os.name == "nt" else "posix")
    prompt = "/%s-dream review" % bid
    rf = _fwd(root)
    rules = ["Skill", "Read",
             "Bash(*%s/scripts/sessions.py*)" % rf, "Bash(*%s/scripts/dream.py*)" % rf,
             "Write(%s/workspace/dream/**)" % _rule_path(root), "Edit(%s/workspace/dream/**)" % _rule_path(root)]
    print("# dream.py NON registra nulla: copia e lancia tu i comandi qui sotto se vuoi la review periodica.")
    print("# Solo la review si schedula: apply resta sempre manuale (/%s-dream apply). Log: .tmp/dream-schedule.log" % bid)
    print("# Strumenti ammessi (minimi): Skill, Read, Bash solo per sessions.py e dream.py, Write/Edit solo in workspace/dream.")
    if plat == "windows":
        cwd = str(node.dir).replace("/", "\\")
        tools = " ".join(r if r in ("Skill", "Read") else '"%s"' % r for r in rules)
        arg = ('/c cd /d "%s" && (if not exist .tmp mkdir .tmp) && claude -p "%s" --setting-sources project '
               '--output-format stream-json --verbose --allowedTools %s > .tmp\\dream-schedule.log 2>&1'
               % (cwd, prompt, tools))
        print("$azione = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument %s -WorkingDirectory %s"
              % (_ps_quote(arg), _ps_quote(cwd)))
        print("$quando = New-ScheduledTaskTrigger -Weekly -DaysOfWeek %s -At %s" % (DAY_NAME[day], _ps_quote(a.time)))
        print("Register-ScheduledTask -TaskName %s -Action $azione -Trigger $quando -Description %s"
              % (_ps_quote("brain-%s-dream" % bid), _ps_quote("Dream review settimanale del brain %s (solo proposta)" % bid)))
    else:
        hh, mm = a.time.split(":")
        tools = " ".join(r if r in ("Skill", "Read") else _sh_quote(r) for r in rules)
        print("%d %d * * %d cd %s && mkdir -p .tmp && claude -p %s --setting-sources project --output-format stream-json "
              "--verbose --allowedTools %s >> .tmp/dream-schedule.log 2>&1"
              % (int(mm), int(hh), CRON_DAY[day], _sh_quote(node.dir.as_posix()), _sh_quote(prompt), tools))
    return 0


# --------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    kc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Dream: percorsi, stato della memoria, proposta, applicazione.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("paths", help="memory dir (e se e' autorevole), cartella proposte, ultima proposta, backup")
    kc.add_common_args(p)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("memory-report", help="stato della memoria (solo lettura)")
    kc.add_common_args(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--content", action="store_true", help="anche il contenuto dei file (mascherato, max 200 KB)")
    p = sub.add_parser("new-proposal", help="crea la proposta vuota con l'intestazione macchina e ne stampa il path")
    kc.add_common_args(p)
    p.add_argument("--sessions-read", type=int, default=None, help="numero di sessioni lette da look-back")
    p = sub.add_parser("apply", help="applica SOLO le voci spuntate [x] della proposta")
    kc.add_common_args(p)
    p.add_argument("--proposal", default=None, help="file della proposta (default: la piu' recente del nodo)")
    p.add_argument("--dry-run", action="store_true", help="mostra cosa farebbe, senza scrivere ne' fare backup")
    p = sub.add_parser("schedule-cmd", help="STAMPA i comandi per schedulare la review (non li registra)")
    kc.add_common_args(p)
    p.add_argument("--day", default="MON", help="giorno della settimana: " + ", ".join(DAYS))
    p.add_argument("--time", default="09:00", help="ora HH:MM")
    p.add_argument("--platform", choices=["auto", "windows", "posix"], default="auto")
    a = ap.parse_args(argv)
    try:
        return {"paths": cmd_paths, "memory-report": cmd_memory_report, "new-proposal": cmd_new_proposal,
                "apply": cmd_apply, "schedule-cmd": cmd_schedule}[a.cmd](a)
    except RootOutside as e:
        print("ERRORE: %s" % e, file=sys.stderr)
        return EXIT_ROOT_OUTSIDE
    except (kc.ConfigError, Stop) as e:
        print("ERRORE: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
