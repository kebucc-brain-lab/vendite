"""kb_config -- costanti e helper condivisi del motore del brain (Brain Builder v3.1).

Forkato da `scripts/kb_config.py` di VMR Brain (HEAD 0efa9254) e genericizzato:
  - la radice NON e' piu' una costante cablata: `resolve_root()` la deriva dalla
    posizione di `scripts/` (motore installato), da `--root`, da `BRAIN_ROOT` (solo se
    il motore non e' dentro un brain) o risalendo dalla cwd fino a `nodo.md`/`brain.yaml`;
  - le cartelle compilate vengono dal profilo del brain (`brain.profile.yaml`), non da
    una lista fissa; `EXCLUDE_DIRS` resta solo per le cartelle di sistema;
  - frontmatter letto con PyYAML (BOM tollerato); in mancanza di PyYAML (hook stdlib)
    si ripiega sul parser a regex di VMR;
  - `.env` caricato SOLO per path esplicito (`<root>/.env`, poi `<nodo>/.env` con
    override), mai `find_dotenv`;
  - identita' (`BRAIN_ID`, `node_path`) e alberi di nodi (`nodo.md`).

Importabile senza venv: PyYAML e python-dotenv sono opzionali (import pigri).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ENGINE_DIR = Path(__file__).resolve().parent
NODE_MARKERS = ("nodo.md", "brain.yaml")
PROFILE_FILE = "brain.profile.yaml"

# Profilo "company" completo: usato SOLO se brain.profile.yaml manca (e lo si dice).
DEFAULT_FOLDERS = ["self", "areas", "projects", "concepts", "docs",
                   "entities", "data", "code", "outputs"]
DEFAULT_RAW_DIRS = ["sources", "workspace"]
DEFAULT_OPS_DIRS = ["ops"]
DEFAULT_CORPUS_ROOTS = ["sources/corpus"]

# Cartelle di SISTEMA: mai note del vault, qualunque sia il profilo.
EXCLUDE_DIRS = {".git", ".obsidian", ".claude", ".codex", ".agents", ".venv", "venv",
                ".memory", ".secrets", ".tmp", ".pytest_cache", "node_modules",
                "__pycache__", "scripts", "_showcase", "_template", "_tipi",
                "_derived", "attachments"}
# File harness alla radice di un nodo: fuori da lint, llms.txt e RAG.
ROOT_SKIP = {"CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "CONTEXT.md", "nodo.md",
             "PLAN.md", "PLAN-REVIEW-LOG.md"}
# Meta di radice: lintati (frontmatter), esenti da minimo-link e orfano.
ROOT_META = {"README.md", "CONVENTIONS.md", "ONBOARDING.md"}
SESSIONS_REL = Path("workspace") / "journal" / "sessions"
DAILY_REL = Path("workspace") / "journal" / "daily"

# GitHub (v3.2, SPEC par. 4-5): UNICO elenco delle estensioni "pesanti" che non vanno nel repo.
# Lo usano brain_sync.py (controllo prima del push, exit 8), repo_check.py (workflow brain-check)
# e kit/github/github_org.py (push ruleset `niente-file-pesanti`).
HEAVY_EXTENSIONS = (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".zip", ".7z", ".rar",
                    ".mp4", ".mov", ".mp3", ".wav", ".png", ".jpg", ".jpeg", ".gif", ".psd")
MAX_FILE_MB = 10
GITHUB_MODES = ("none", "personal", "org")

_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)
_BOM = "﻿"


class ConfigError(RuntimeError):
    """Configurazione del brain assente o incoerente: il comando si ferma con messaggio."""


class FrontmatterError(ValueError):
    """Frontmatter YAML non valido (es. `title: a: b` non quotato)."""


# ----------------------------------------------------------------------------
# Testo e frontmatter
# ----------------------------------------------------------------------------
def read_text(p: Path) -> str:
    """UTF-8 con BOM tollerato (il BOM viene tolto), byte invalidi sostituiti."""
    return Path(p).read_text(encoding="utf-8-sig", errors="replace")


def has_bom(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            return f.read(3) == b"\xef\xbb\xbf"
    except OSError:
        return False


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Parser a regex di VMR (fallback senza PyYAML): valori sempre stringa,
    liste a blocco appiattite in `a,b,c`."""
    if text.startswith(_BOM):
        text = text[1:]
    m = _FM.match(text)
    if not m:
        return {}, text
    block, body = m.group(1), m.group(2)
    fm: dict[str, Any] = {}
    key = None
    for line in block.split("\n"):
        km = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if km:
            key = km.group(1)
            fm[key] = km.group(2).strip()
            continue
        lm = re.match(r"^\s+-\s*(.*)$", line)
        if lm and key:
            item = lm.group(1).strip().strip("\"'")
            fm[key] = f"{fm[key]},{item}" if fm[key] else item
    return fm, body


def _yaml_module():
    try:
        import yaml  # noqa: PLC0415
        return yaml
    except Exception:  # noqa: BLE001
        return None


def read_frontmatter(source: Path | str) -> tuple[dict, str]:
    """Frontmatter YAML (PyYAML safe_load) + corpo. `source` = path o testo.

    - BOM UTF-8 tollerato (tolto prima del parse).
    - YAML invalido -> FrontmatterError (il lint lo riporta come errore).
    - Senza PyYAML ripiega su split_frontmatter() (hook stdlib-only).
    - Il frontmatter deve essere una mappa; altrimenti FrontmatterError.
    """
    text = read_text(source) if isinstance(source, Path) else source
    if text.startswith(_BOM):
        text = text[1:]
    m = _FM.match(text)
    if not m:
        return {}, text
    block, body = m.group(1), m.group(2)
    yaml = _yaml_module()
    if yaml is None:
        return split_frontmatter(text)
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as e:  # type: ignore[attr-defined]
        mark = getattr(e, "problem_mark", None)
        where = f" (riga {mark.line + 1} del frontmatter)" if mark is not None else ""
        raise FrontmatterError(f"YAML non valido{where}: {getattr(e, 'problem', e)}") from e
    if data is None:
        return {}, body
    if not isinstance(data, dict):
        raise FrontmatterError("il frontmatter deve essere una mappa chiave: valore")
    return {str(k): v for k, v in data.items()}, body


def fm_str(v: Any) -> str:
    """Valore di frontmatter come testo: liste unite da virgola, date ISO, None -> ''."""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ",".join(fm_str(x) for x in v)
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()
    return str(v).strip()


# ----------------------------------------------------------------------------
# YAML ristretto (copiato da kit/federation/federate.py): serve a leggere
# brain.profile.yaml e brain.yaml SENZA PyYAML (hook e adapter stdlib-only).
# Supporta mappe, liste a blocco, liste in linea, scalari, commenti.
# ----------------------------------------------------------------------------
class YamlError(ValueError):
    pass


_INT_RE = re.compile(r"^[-+]?\d+$")
_FLOAT_RE = re.compile(r"^[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?$")


def _strip_comment(line: str) -> str:
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def _scalar(raw: str) -> Any:
    s = raw.strip()
    if s == "" or s in ("null", "~", "Null", "NULL"):
        return None
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        body = s[1:-1]
        if s[0] == "'":
            return body.replace("''", "'")
        return bytes(body, "utf-8").decode("unicode_escape") if "\\" in body else body
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if _INT_RE.match(s):
        return int(s)
    if _FLOAT_RE.match(s):
        return float(s)
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        items, cur, quote = [], "", None
        for ch in inner:
            if quote:
                cur += ch
                if ch == quote:
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
                cur += ch
            elif ch == ",":
                items.append(cur)
                cur = ""
            else:
                cur += ch
        items.append(cur)
        return [_scalar(it) for it in items]
    if s.startswith("{"):
        raise YamlError("mappe in linea `{}` non supportate dal parser ristretto")
    return s


def _split_key(content: str) -> tuple[str, str] | None:
    if content.startswith(("'", '"')):
        return None
    m = re.match(r"^([^:#]+?):(?:\s+(.*))?$", content)
    if not m:
        return None
    return m.group(1).strip(), (m.group(2) or "")


def mini_yaml_load(text: str) -> Any:
    lines: list[tuple[int, str]] = []
    for n, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise YamlError(f"riga {n}: tab nell'indentazione")
        body = _strip_comment(raw).rstrip()
        if not body.strip() or body.strip() == "---":
            continue
        lines.append((len(body) - len(body.lstrip(" ")), body.strip()))
    if not lines:
        return None
    value, i = _parse_block(lines, 0, lines[0][0])
    if i != len(lines):
        raise YamlError(f"contenuto inatteso dopo la riga {i}: {lines[i][1][:40]!r}")
    return value


def _parse_block(lines, i, indent):
    if lines[i][1] == "-" or lines[i][1].startswith("- "):
        return _parse_seq(lines, i, indent)
    return _parse_map(lines, i, indent)


def _parse_map(lines, i, indent):
    out: dict[str, Any] = {}
    while i < len(lines) and lines[i][0] == indent and not (lines[i][1] == "-" or lines[i][1].startswith("- ")):
        kv = _split_key(lines[i][1])
        if kv is None:
            raise YamlError(f"attesa coppia chiave: valore, trovato {lines[i][1][:40]!r}")
        key, rest = kv
        if key in out:
            raise YamlError(f"chiave duplicata {key!r}")
        if rest.strip():
            out[key] = _scalar(rest)
            i += 1
        elif i + 1 < len(lines) and lines[i + 1][0] > indent:
            out[key], i = _parse_block(lines, i + 1, lines[i + 1][0])
        elif i + 1 < len(lines) and lines[i + 1][0] == indent and lines[i + 1][1].startswith("- "):
            out[key], i = _parse_seq(lines, i + 1, indent)
        else:
            out[key] = None
            i += 1
    if i < len(lines) and lines[i][0] > indent:
        raise YamlError(f"indentazione inattesa: {lines[i][1][:40]!r}")
    return out, i


def _parse_seq(lines, i, indent):
    out: list[Any] = []
    while i < len(lines) and lines[i][0] == indent and (lines[i][1] == "-" or lines[i][1].startswith("- ")):
        rest = lines[i][1][1:]
        stripped = rest.strip()
        if not stripped:
            if i + 1 < len(lines) and lines[i + 1][0] > indent:
                val, i = _parse_block(lines, i + 1, lines[i + 1][0])
            else:
                val, i = None, i + 1
        elif _split_key(stripped) is not None and not stripped.startswith("["):
            sub_indent = indent + 1 + (len(rest) - len(rest.lstrip(" ")))
            lines[i] = (sub_indent, stripped)
            val, i = _parse_map(lines, i, sub_indent)
        else:
            val, i = _scalar(stripped), i + 1
        out.append(val)
    return out, i


def load_yaml_file(p: Path) -> Any:
    """PyYAML se c'e', altrimenti il parser ristretto. File assente -> None."""
    p = Path(p)
    if not p.is_file():
        return None
    text = read_text(p)
    yaml = _yaml_module()
    if yaml is not None:
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError as e:  # type: ignore[attr-defined]
            raise ConfigError(f"{p.name}: YAML non valido: {getattr(e, 'problem', e)}") from e
    try:
        return mini_yaml_load(text)
    except YamlError as e:
        raise ConfigError(f"{p.name}: YAML non valido (parser ristretto): {e}") from e


# ----------------------------------------------------------------------------
# Radice del brain e nodi
# ----------------------------------------------------------------------------
def is_node_dir(d: Path) -> bool:
    return any((d / m).is_file() for m in NODE_MARKERS)


def _marker_chain(start: Path) -> list[Path]:
    """Cartelle con nodo.md/brain.yaml da `start` (inclusa) verso l'alto, dal basso."""
    out: list[Path] = []
    cur = Path(start).resolve()
    for d in (cur, *cur.parents):
        if is_node_dir(d):
            out.append(d)
    return out


def find_brain_root(start: Path | str | None = None) -> Path | None:
    """Risale da `start` (default cwd) fino alla RADICE del brain.

    La radice e' la cartella marcata piu' in alto che ha `brain.yaml` oppure
    `scripts/kb_config.py` (il motore installato); se nessuna la soddisfa, la
    cartella marcata piu' in alto. None se nella catena non c'e' nessun marcatore.
    """
    chain = _marker_chain(Path(start) if start else Path.cwd())
    if not chain:
        return None
    for d in reversed(chain):
        if (d / "brain.yaml").is_file() or (d / "scripts" / "kb_config.py").is_file():
            return d
    return chain[-1]


def _node_path_of(node_dir: Path, root: Path) -> str:
    try:
        rel = node_dir.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return "."
    return rel or "."


def deployed_root() -> Path | None:
    """Radice del brain in cui il motore e' installato (`<root>/scripts/kb_config.py`)."""
    cand = ENGINE_DIR.parent
    return cand if is_node_dir(cand) else None


def resolve_root(cli_root: str | Path | None = None, start: Path | None = None) -> Path:
    """Ordine: --root esplicito > motore installato nel brain > BRAIN_ROOT (solo se il
    motore NON e' installato in un brain) > risalita dalla cwd. Altrimenti ConfigError.
    Se il motore e' installato e BRAIN_ROOT indica un'altra cartella, avvisa su stderr
    (il controllo bloccante e' di doctor)."""
    if cli_root:
        r = Path(cli_root).resolve()
        if not is_node_dir(r):
            raise ConfigError(f"--root {r}: manca nodo.md/brain.yaml (non e' la radice di un brain)")
        return r
    dep = deployed_root()
    env_root = os.environ.get("BRAIN_ROOT", "").strip()
    if dep is not None:
        if env_root and Path(env_root).resolve() != dep.resolve():
            print(f"[kb_config] AVVISO: BRAIN_ROOT={env_root} diverso dalla radice del motore {dep}",
                  file=sys.stderr)
        return dep
    if env_root:
        r = Path(env_root).resolve()
        if not is_node_dir(r):
            raise ConfigError(f"BRAIN_ROOT={env_root}: manca nodo.md/brain.yaml")
        return r
    found = find_brain_root(start or Path.cwd())
    if found is None:
        raise ConfigError("radice del brain non trovata: nessun nodo.md/brain.yaml risalendo dalla cwd "
                          "(usa --root o esegui dentro il brain)")
    return found


@dataclass
class Profile:
    """Profilo del brain (brain.profile.yaml, copiato dallo scaffolder da tipi/<kind>/profile.yaml).

    Schema (deciso dal coordinatore, integra SPEC par. 4):
        profile_version: 1
        kind: company                      # project | brand | business | client | company
        profile: full                      # full | <kind>
        folders:                           # allow-list COMPILATA (lint, llms.txt, ingest), in ordine
          - name: self                     #   forma canonica: oggetti {name, summary}
            summary: "Identita' del business ..."
          - docs                           #   ammessa anche la stringa nuda (summary vuoto)
        ops_dirs: [ops]                    # cartelle operative osservate da feed_check (relative al nodo)
        corpus_roots: [sources/corpus]     # radici del corpus per ingest_corpus (relative al nodo)
        skills_extra: []                   # skill aggiuntive del tipo (le installa lo scaffolder)
        raw_dirs: [sources, workspace]     # (facoltativo, default cosi') zone grezze: committate, non lintate
    """
    folders: list[str] = field(default_factory=lambda: list(DEFAULT_FOLDERS))
    raw_dirs: list[str] = field(default_factory=lambda: list(DEFAULT_RAW_DIRS))
    ops_dirs: list[str] = field(default_factory=lambda: list(DEFAULT_OPS_DIRS))
    corpus_roots: list[str] = field(default_factory=lambda: list(DEFAULT_CORPUS_ROOTS))
    skills_extra: list[str] = field(default_factory=list)
    kind: str = "company"
    profile: str = "full"
    folder_summaries: dict[str, str] = field(default_factory=dict)
    source: Path | None = None      # file da cui e' stato letto; None = default

    @property
    def is_default(self) -> bool:
        return self.source is None


def _rel_dir(x: str, what: str) -> str:
    x = x.strip().strip("/").replace("\\", "/")
    if not x or x.startswith("..") or x.startswith("/") or ":" in x:
        raise ConfigError(f"{PROFILE_FILE}: `{what}` contiene un path non relativo o vuoto: {x!r}")
    return x


def _str_list(v: Any, what: str) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise ConfigError(f"{PROFILE_FILE}: `{what}` deve essere una lista di stringhe")
    out = []
    for x in v:
        x = _rel_dir(x, what)
        if x not in out:
            out.append(x)
    return out


def _folders_list(v: Any) -> tuple[list[str], dict[str, str]]:
    """`folders`: lista di oggetti {name, summary} (canonica) oppure di stringhe (summary vuoto),
    anche miste. Ritorna (nomi in ordine, {nome: summary})."""
    if v is None:
        return [], {}
    if isinstance(v, (str, dict)):
        v = [v]
    if not isinstance(v, list):
        raise ConfigError(f"{PROFILE_FILE}: `folders` deve essere una lista")
    names: list[str] = []
    summaries: dict[str, str] = {}
    for item in v:
        if isinstance(item, str):
            name, summary = item, ""
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            name, summary = item["name"], fm_str(item.get("summary"))
        else:
            raise ConfigError(f"{PROFILE_FILE}: ogni voce di `folders` e' una stringa o un oggetto "
                              f"con le chiavi name e summary; trovato {item!r}")
        name = _rel_dir(name, "folders")
        if name not in names:
            names.append(name)
        if summary:
            summaries[name] = summary
    return names, summaries


def load_profile(root: Path, node_dir: Path | None = None) -> Profile:
    """`brain.profile.yaml` del nodo se esiste, altrimenti quello della radice,
    altrimenti il profilo completo di default (segnalato da `Profile.is_default`)."""
    candidates = []
    if node_dir is not None:
        candidates.append(Path(node_dir) / PROFILE_FILE)
    candidates.append(Path(root) / PROFILE_FILE)
    for p in candidates:
        if p.is_file():
            data = load_yaml_file(p)
            if not isinstance(data, dict):
                raise ConfigError(f"{p}: atteso un documento mappa")
            folders, summaries = _folders_list(data.get("folders"))
            if not folders:
                raise ConfigError(f"{p}: `folders` vuoto o assente")
            legacy = data.get("folder_summaries")
            if isinstance(legacy, dict):   # forma vecchia (mappa separata): la canonica vince
                summaries = {**{str(k): str(v) for k, v in legacy.items()}, **summaries}
            prof = Profile(
                folders=folders,
                raw_dirs=_str_list(data.get("raw_dirs", DEFAULT_RAW_DIRS), "raw_dirs"),
                ops_dirs=_str_list(data.get("ops_dirs", DEFAULT_OPS_DIRS), "ops_dirs"),
                corpus_roots=_str_list(data.get("corpus_roots", []), "corpus_roots"),
                skills_extra=[str(s) for s in (data.get("skills_extra") or []) if str(s).strip()],
                kind=str(data.get("kind", "company")),
                profile=str(data.get("profile", "full")),
                folder_summaries=summaries,
                source=p,
            )
            return prof
    return Profile()


def profile_folders(profile: Profile | None) -> list[str]:
    """Allow-list delle cartelle compilate (COMPILED_FOLDERS del profilo)."""
    return list((profile or Profile()).folders)


@dataclass
class Node:
    path: str            # node_path: "." per la radice, altrimenti "a/b" (separatore /)
    dir: Path
    root: Path
    profile: Profile
    meta: dict = field(default_factory=dict)   # frontmatter di nodo.md
    ancestor_paths: list[str] | None = None    # nodi antenati REALI (li imposta iter_nodes)

    @property
    def is_root(self) -> bool:
        return self.path == "."

    @property
    def key(self) -> str:
        return node_key(self.path)

    @property
    def name(self) -> str:
        return fm_str(self.meta.get("nome") or self.meta.get("name")) or (
            brain_name(self.root) if self.is_root else self.dir.name)

    def prefix_ancestors(self) -> list[str]:
        """Tutti i prefissi di cartella del node_path, dal piu' vicino alla radice ("." inclusa)."""
        if self.is_root:
            return []
        parts = self.path.split("/")
        out = ["/".join(parts[:i]) for i in range(len(parts) - 1, 0, -1)]
        out.append(".")
        return out

    def ancestors(self) -> list[str]:
        """node_path dei nodi ANTENATI (solo cartelle che sono davvero nodi), dal piu' vicino
        alla radice ("." inclusa). Senza la lista dei nodi (ancestor_paths None) ripiega sui
        prefissi di cartella: un sovrainsieme innocuo (nessun punto ha quei node_path)."""
        if self.is_root:
            return []
        if self.ancestor_paths is not None:
            return list(self.ancestor_paths)
        return self.prefix_ancestors()

    def visible_paths(self) -> list[str]:
        """Il nodo stesso + gli antenati: il dominio dei link e del filtro RAG."""
        return [self.path, *self.ancestors()]

    def rel(self, p: Path) -> str:
        return Path(p).resolve().relative_to(self.dir.resolve()).as_posix()

    def rel_root(self, p: Path) -> str:
        return Path(p).resolve().relative_to(self.root.resolve()).as_posix()


def node_key(node_path: str) -> str:
    return "root" if node_path in (".", "") else node_path.replace("/", "__")


def _read_node_meta(d: Path) -> dict:
    nm = d / "nodo.md"
    if not nm.is_file():
        return {}
    try:
        fm, _ = read_frontmatter(nm)
        return fm
    except (FrontmatterError, OSError):
        return {}


def _walk_node_dirs(root: Path) -> list[Path]:
    """Cartelle con `nodo.md` sotto la radice, potando cartelle di sistema e dot-dir."""
    found: list[Path] = []
    for cur, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith("."))
        if "nodo.md" in files and Path(cur) != root:
            found.append(Path(cur))
    return found


def iter_nodes(root: Path) -> list[Node]:
    """Radice (".") + ogni cartella con `nodo.md` sotto la radice (cartelle di sistema
    escluse), ordinati per profondita'. Il profilo di un nodo e' il suo
    brain.profile.yaml se c'e', altrimenti quello della radice. Ogni nodo riceve la lista
    dei suoi antenati REALI (`ancestor_paths`)."""
    root = Path(root).resolve()
    nodes = [Node(".", root, root, load_profile(root), _read_node_meta(root), [])]
    found = _walk_node_dirs(root)
    for d in sorted(found, key=lambda p: (len(p.relative_to(root).parts), p.as_posix())):
        nodes.append(Node(_node_path_of(d, root), d, root, load_profile(root, d), _read_node_meta(d)))
    paths = {n.path for n in nodes}
    for n in nodes:
        n.ancestor_paths = [a for a in n.prefix_ancestors() if a in paths]
    return nodes


def node_of(path: Path, nodes: list[Node]) -> Node:
    """Nodo di appartenenza di un file: il nodo antenato piu' vicino."""
    rp = Path(path).resolve()
    best = nodes[0]
    best_len = -1
    for n in nodes:
        nd = n.dir.resolve()
        try:
            rp.relative_to(nd)
        except ValueError:
            continue
        depth = len(nd.parts)
        if depth > best_len:
            best, best_len = n, depth
    return best


def find_node(start: Path | str | None = None, root: Path | None = None,
              nodes: list[Node] | None = None) -> Node | None:
    """Il nodo piu' vicino a `start` (cwd di default): la cartella con `nodo.md`
    (o `brain.yaml` per la radice) risalendo. None se fuori da qualunque brain."""
    chain = _marker_chain(Path(start) if start else Path.cwd())
    if not chain:
        return None
    root = Path(root).resolve() if root else find_brain_root(chain[0])
    if root is None:
        return None
    d = chain[0]
    try:
        d.resolve().relative_to(root.resolve())
    except ValueError:
        d = root
    np_ = _node_path_of(d, root)
    nodes = nodes or iter_nodes(root)
    for n in nodes:
        if n.path == np_:
            return n
    return Node(np_, d, root, load_profile(root, None if np_ == "." else d), _read_node_meta(d))


def current_node(root: Path, start: Path | None = None, override: str | None = None,
                 nodes: list[Node] | None = None) -> Node:
    """Nodo della sessione: `--node` esplicito > nodo.md piu' vicino alla cwd (se la cwd e'
    dentro il brain) > BRAIN_NODE in ambiente > radice."""
    nodes = nodes or iter_nodes(root)
    by_path = {n.path: n for n in nodes}
    if override:
        key = override.strip().replace("\\", "/").strip("/") or "."
        if key not in by_path:
            raise ConfigError(f"nodo {override!r} inesistente (nodi: {', '.join(by_path)})")
        return by_path[key]
    start = Path(start) if start else Path.cwd()
    try:
        start.resolve().relative_to(Path(root).resolve())
        near = find_node(start, root, nodes)
        if near is not None and near.path in by_path:
            return by_path[near.path]
    except ValueError:
        pass
    env_node = os.environ.get("BRAIN_NODE", "").strip().replace("\\", "/").strip("/")
    if env_node and env_node in by_path:
        return by_path[env_node]
    return by_path["."]


# ----------------------------------------------------------------------------
# Identita'
# ----------------------------------------------------------------------------
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


def _brain_yaml(root: Path) -> dict:
    try:
        data = load_yaml_file(Path(root) / "brain.yaml")
    except ConfigError:
        return {}
    return data if isinstance(data, dict) else {}


def brain_id(root: Path, strict: bool = True) -> str:
    """BRAIN_ID: ambiente (da .env gia' caricato o dal processo) > nodo.md della radice
    (`brain_id`) > brain.yaml (`brain_id`). Nessuno -> ConfigError (strict) o ''."""
    env = os.environ.get("BRAIN_ID", "").strip()
    cand = env or fm_str(_read_node_meta(Path(root)).get("brain_id")) or \
        fm_str(_brain_yaml(root).get("brain_id"))
    if cand:
        if not _ID_RE.match(cand):
            raise ConfigError(f"BRAIN_ID {cand!r} non valido: atteso " + _ID_RE.pattern)
        return cand
    if strict:
        raise ConfigError("BRAIN_ID assente: mettilo in .env (BRAIN_ID=...), in nodo.md (brain_id:) "
                          "o in brain.yaml (brain_id:)")
    return ""


def github_config(root: Path) -> dict:
    """Sezione `github:` di brain.yaml (v3.2): {mode, owner, repo, visibility, role}; se brain.yaml non c'e'
    (clone non ancora inizializzato: in un brain GitHub brain.yaml e' un file LOCALE) la si legge dalla copia
    tracciata `brain.config.yaml`. Assente ovunque -> {}. Valori sempre stringhe."""
    gh = _brain_yaml(root).get("github")
    if not isinstance(gh, dict):
        try:
            cfg = load_yaml_file(Path(root) / "brain.config.yaml")
        except ConfigError:
            cfg = None
        gh = cfg.get("github") if isinstance(cfg, dict) else None
    if not isinstance(gh, dict):
        return {}
    out = {k: fm_str(gh.get(k)) for k in ("mode", "owner", "repo", "visibility", "role")}
    if out["mode"] and out["mode"] not in GITHUB_MODES:
        raise ConfigError(f"brain.yaml: github.mode {out['mode']!r} non valido (ammessi: {', '.join(GITHUB_MODES)})")
    return out


def brain_name(root: Path) -> str:
    """Nome leggibile: brain.yaml `name` > nodo.md `nome`/`name` > BRAIN_ID > nome cartella."""
    y = _brain_yaml(root)
    nm = fm_str(y.get("name"))
    if nm:
        return nm
    meta = _read_node_meta(Path(root))
    nm = fm_str(meta.get("nome") or meta.get("name"))
    if nm:
        return nm
    return brain_id(root, strict=False) or Path(root).name


# ----------------------------------------------------------------------------
# .env per path esplicito
# ----------------------------------------------------------------------------
def _parse_env_file(p: Path) -> dict[str, str]:
    """Parser minimo (fallback senza python-dotenv): KEY=VALUE, commenti, virgolette."""
    out: dict[str, str] = {}
    for raw in read_text(p).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        else:
            v = v.split(" #", 1)[0].strip()
        if k:
            out[k] = v
    return out


def load_env(root: Path, node_dir: Path | None = None) -> list[Path]:
    """Carica `<root>/.env` (senza sovrascrivere l'ambiente) e poi `<nodo>/.env`
    (override=True) SOLO per path esplicito. Mai find_dotenv, mai la cwd.
    Ritorna i file effettivamente caricati."""
    loaded: list[Path] = []
    files = [(Path(root) / ".env", False)]
    if node_dir is not None and Path(node_dir).resolve() != Path(root).resolve():
        files.append((Path(node_dir) / ".env", True))
    try:
        from dotenv import load_dotenv  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        load_dotenv = None
    for p, override in files:
        if not p.is_file():
            continue
        if load_dotenv is not None:
            load_dotenv(dotenv_path=str(p), override=override, encoding="utf-8")
        else:
            for k, v in _parse_env_file(p).items():
                if override or k not in os.environ:
                    os.environ[k] = v
        loaded.append(p)
    return loaded


def require_env(name: str, hint: str = "") -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise ConfigError(f"{name} assente nell'ambiente/.env{(': ' + hint) if hint else ''}")
    return v


# ----------------------------------------------------------------------------
# Note del vault
# ----------------------------------------------------------------------------
def _under_descendant_node(p: Path, node: Node, nodes: list[Node]) -> bool:
    for other in nodes:
        if other is node or other.path == node.path:
            continue
        if not other.path.startswith(node.path + "/") and node.path != ".":
            continue
        try:
            p.resolve().relative_to(other.dir.resolve())
            return True
        except ValueError:
            continue
    return False


def iter_notes(node: Node, nodes: list[Node] | None = None) -> list[Path]:
    """Note COMPILATE del nodo: `*.md` nelle cartelle del profilo sotto la cartella del
    nodo, escluse le cartelle di sistema, le sottocartelle di `code/` (codice vivo) e
    tutto cio' che appartiene a un nodo discendente."""
    nodes = nodes or [node]
    out: list[Path] = []
    for folder in node.profile.folders:
        d = node.dir / folder
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.md")):
            rel = p.relative_to(node.dir).parts
            if any(seg in EXCLUDE_DIRS or seg.startswith(".") for seg in rel[:-1]):
                continue
            if folder == "code" and len(rel) > 2:
                continue
            if _under_descendant_node(p, node, nodes):
                continue
            out.append(p)
    return out


def root_meta_files(node: Node) -> list[Path]:
    return [node.dir / n for n in sorted(ROOT_META) if (node.dir / n).is_file()]


def iter_sessions(node: Node) -> list[Path]:
    d = node.dir / SESSIONS_REL
    return sorted(d.rglob("*.md")) if d.is_dir() else []


def index_dir(root: Path) -> Path:
    return Path(root) / "scripts" / "rag" / "index"


def state_file(root: Path, node_path: str, kind: str = "vault_state") -> Path:
    k = node_key(node_path)
    return index_dir(root) / (f"{kind}.json" if k == "root" else f"{kind}.{k}.json")


def memory_dir(root: Path, node: Node | None = None) -> Path:
    """BRAIN_MEMORY_DIR > autoMemoryDirectory del settings del nodo > <root>/.memory."""
    env = os.environ.get("BRAIN_MEMORY_DIR", "").strip()
    if env:
        return Path(env)
    d = (node.dir if node else Path(root)) / ".claude" / "settings.json"
    if d.is_file():
        try:
            data = json.loads(read_text(d))
            amd = data.get("autoMemoryDirectory")
            if isinstance(amd, str) and amd.strip():
                return Path(amd)
        except (OSError, ValueError):
            pass
    return Path(root) / ".memory"


def default_guard_home() -> Path:
    """Default di piattaforma della casa della guardia (ultima istanza)."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "brain-guard"
    return Path.home() / ".brain-guard"


def _guard_script_in(text: str) -> str | None:
    """Path di `brain-guard.mjs` dentro una stringa di hook (intero, quotato o token nudo)."""
    marker = "brain-guard.mjs"
    i = text.find(marker)
    if i < 0:
        return None
    end = i + len(marker)
    if text.endswith(marker) and '"' not in text and "'" not in text:
        return text                                      # elemento di `args` o comando nudo
    q = max(text.rfind('"', 0, i), text.rfind("'", 0, i))
    if q >= 0:
        cand = text[q + 1:end]
        if '"' not in cand and "'" not in cand and "\n" not in cand:
            return cand                                  # path quotato (puo' avere spazi)
    start = max(text.rfind(" ", 0, i), text.rfind("\t", 0, i)) + 1
    return text[start:end]


def guard_dir_from_settings(settings: Path) -> Path | None:
    """Cartella dello script `brain-guard.mjs` referenziato negli hook di un settings.json."""
    try:
        data = json.loads(read_text(settings))
    except (OSError, ValueError):
        return None
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return None
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for h in entry.get("hooks") or []:
                if not isinstance(h, dict):
                    continue
                texts = [str(h.get("command") or "")]
                if isinstance(h.get("args"), list):
                    texts += [str(a) for a in h["args"]]
                for t in texts:
                    found = _guard_script_in(t)
                    if found:
                        return Path(found.replace("\\", "/")).parent
    return None


def guard_home(root: Path | None = None, node: "Node | None" = None) -> Path:
    """Casa della guardia: BRAIN_GUARD_HOME > cartella di `brain-guard.mjs` referenziato negli
    hook di `.claude/settings.json` (prima quello del nodo, poi quello della radice) > default
    di piattaforma (%LOCALAPPDATA%/brain-guard, POSIX ~/.brain-guard)."""
    env = os.environ.get("BRAIN_GUARD_HOME", "").strip()
    if env:
        return Path(env)
    candidates: list[Path] = []
    if node is not None:
        candidates.append(node.dir / ".claude" / "settings.json")
    if root is not None:
        candidates.append(Path(root) / ".claude" / "settings.json")
    for s in candidates:
        if s.is_file():
            d = guard_dir_from_settings(s)
            if d is not None:
                return d
    return default_guard_home()


def logical_today(now: _dt.datetime | None = None) -> _dt.date:
    """Giorno LOGICO del journal: 00:00-04:59 appartengono ancora a ieri."""
    now = now or _dt.datetime.now()
    return now.date() - _dt.timedelta(days=1) if now.hour < 5 else now.date()


def utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower() != "utf-8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def write_text(p: Path, text: str) -> None:
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def add_common_args(ap) -> None:
    """--root/--node condivisi da tutti gli script del motore."""
    ap.add_argument("--root", default=None, help="radice del brain (default: dedotta dal motore/cwd)")
    ap.add_argument("--node", default=None, help="node_path del nodo (default: nodo.md piu' vicino alla cwd)")


def _as_list(x: Iterable[str] | str | None) -> list[str]:
    if x is None:
        return []
    return [x] if isinstance(x, str) else list(x)
