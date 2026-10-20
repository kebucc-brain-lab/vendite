"""repo_check -- controllo applicativo di cio' che finisce su GitHub (Brain Builder v3.2).

SPEC v3.2 par. 4 e par. 12 (G9, G14, G15). Un controllo applicativo forte, non una garanzia: formati
sconosciuti di segreti possono sfuggire; il modello di minaccia sono gli errori, non un avversario.

Cosa rifiuta (un problema per commit + path + tipo; MAI il valore di un segreto):
  - path vietati: `workspace/dream/` (proposte dream), `.memory/`, `.memory-backup/`, memory dir
    personalizzate dei nodi (passate dal chiamante), `.secrets/`, `.env` e `.env.*` TRANNE `.env.example`;
  - estensioni pesanti (`kb_config.HEAVY_EXTENSIONS`) e blob oltre `kb_config.MAX_FILE_MB` MB;
  - contenuti con segreti (`secrets_scan.find_secrets`, cantiere D): tutti i tipi a prefisso noto ovunque;
    le "assegnazioni" (`password: x`, `token=...`) solo nei file NON di codice, e non quando il valore e'
    un riferimento evidente (nome di variabile in maiuscolo, `<segnaposto>`, `${VAR}`, valori vuoti).

Insiemi controllati:
  outgoing  -> ogni blob aggiunto o modificato in OGNI commit in uscita: `git rev-list <ref> --not --remotes`
               (tutta la storia se il remote e' vuoto); un file aggiunto e poi cancellato viene visto
               lo stesso, perche' resta nella storia pubblicata;
  staged    -> i blob nello staging (prima del commit di brain_sync);
  tracked   -> i blob dell'albero di HEAD (workflow `brain-check` in CI).

Uso:
    python scripts/repo_check.py [--root R] --tracked          # CI: albero di HEAD
    python scripts/repo_check.py [--root R] --outgoing [--ref B] # commit non ancora su nessun remote
Exit: 0 nessun problema, 8 problemi trovati, 3 errore di configurazione/git.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_config as kc  # noqa: E402

try:
    from secrets_scan import find_secrets  # noqa: E402  (cantiere D, API stabile)
except ImportError:  # pragma: no cover - il motore installato lo contiene sempre
    find_secrets = None

ALLOWED_ENV_FILES = {".env.example"}
CODE_EXTENSIONS = {".py", ".pyw", ".mjs", ".cjs", ".js", ".ts", ".ps1", ".psm1", ".sh", ".bat", ".cmd"}
FORBIDDEN_SEQUENCES = (("workspace", "dream"),)
FORBIDDEN_SEGMENTS = {".memory", ".memory-backup", ".secrets"}
MAX_SCAN_BYTES = 2 * 1024 * 1024     # oltre: niente scansione dei contenuti (il limite di dimensione vale comunque)
_REFERENCE_VALUE = re.compile(r"^(?:[A-Z][A-Z0-9_]{2,}|<[^>]*>?|\$\{?[A-Za-z_][\w]*\}?|%[A-Za-z_]+%|\*+|x{3,}|\.{3}|"
                              r"null|none|true|false|\[SEGRETO-MASCHERATO:.*)$", re.I)


@dataclass(frozen=True)
class Problem:
    commit: str      # sha corto, "staging" o "HEAD"
    path: str
    kind: str        # descrizione del tipo, mai il valore

    def line(self) -> str:
        return f"  {self.commit:8}  {self.path}  ->  {self.kind}"


class RepoCheckError(RuntimeError):
    pass


def _git(root: Path, *args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, input=input_bytes,
                          stdin=None if input_bytes is not None else subprocess.DEVNULL)


def path_reason(path: str, extra_prefixes: tuple[str, ...] = ()) -> str | None:
    """Motivo per cui `path` (relativo alla radice del repo, separatore /) non puo' andare su GitHub."""
    p = PurePosixPath(path.replace("\\", "/"))
    parts = p.parts
    low = [x.lower() for x in parts]
    for seg in low[:-1] + [low[-1]]:
        if seg in FORBIDDEN_SEGMENTS:
            return f"percorso vietato ({seg}/)"
    for i in range(len(low) - 1):
        for seq in FORBIDDEN_SEQUENCES:
            if tuple(low[i:i + len(seq)]) == seq:
                return "percorso vietato (" + "/".join(seq) + "/: proposte dream)"
    for seg in low:
        if seg.startswith(".env") and seg not in ALLOWED_ENV_FILES:
            return "percorso vietato (.env*: solo .env.example e' ammesso)"
    norm = "/".join(parts)
    for pre in extra_prefixes:
        pre = pre.strip("/")
        if pre and (norm == pre or norm.startswith(pre + "/")):
            return f"percorso vietato (memoria del nodo: {pre}/)"
    if p.suffix.lower() in kc.HEAVY_EXTENSIONS:
        return f"estensione pesante ({p.suffix.lower()}): l'originale sta fuori dal repo"
    return None


def _assignment_is_reference(value: str) -> bool:
    v = value.strip().strip("\"'")
    return not v or bool(_REFERENCE_VALUE.match(v))


def secret_kinds(path: str, data: bytes) -> list[str]:
    """Tipi di segreto trovati nel contenuto (mai i valori). Binari saltati."""
    if find_secrets is None:
        return ["scansione dei segreti non eseguibile: secrets_scan.py assente accanto a repo_check.py"]
    if b"\0" in data[:8192]:
        return []
    text = data.decode("utf-8", errors="replace")
    code = PurePosixPath(path).suffix.lower() in CODE_EXTENSIONS
    kinds: list[str] = []
    for kind, s, e in find_secrets(text):
        if kind == "assegnazione" and (code or _assignment_is_reference(text[s:e])):
            continue
        if kind not in kinds:
            kinds.append(kind)
    return kinds


def _parse_raw(out: bytes) -> list[tuple[str, str, str]]:
    """Output `-z` di diff-tree/diff --raw -> [(status, blob nuovo, path)] per A/M/T (le cancellazioni no)."""
    items: list[tuple[str, str, str]] = []
    toks = out.split(b"\0")
    i = 0
    while i < len(toks):
        meta = toks[i].decode("utf-8", "replace")
        if not meta.startswith(":"):
            i += 1
            continue
        fields = meta[1:].split()
        status = fields[4][:1] if len(fields) >= 5 else "?"
        newsha = fields[3] if len(fields) >= 4 else ""
        path = toks[i + 1].decode("utf-8", "replace") if i + 1 < len(toks) else ""
        i += 2
        if status in ("C", "R"):          # con -M/-C ci sarebbero due path: non li chiediamo mai
            path = toks[i].decode("utf-8", "replace") if i < len(toks) else path
            i += 1
        if status in ("A", "M", "T", "C", "R") and set(newsha) != {"0"}:
            items.append((status, newsha, path))
    return items


def _blob_sizes(root: Path, shas: list[str]) -> dict[str, int]:
    if not shas:
        return {}
    r = _git(root, "cat-file", "--batch-check", input_bytes=("\n".join(shas) + "\n").encode())
    out: dict[str, int] = {}
    for ln in r.stdout.decode("utf-8", "replace").splitlines():
        f = ln.split()
        if len(f) == 3 and f[2].isdigit():
            out[f[0]] = int(f[2])
    return out


def _blob_contents(root: Path, shas: list[str]) -> dict[str, bytes]:
    if not shas:
        return {}
    r = _git(root, "cat-file", "--batch", input_bytes=("\n".join(shas) + "\n").encode())
    data, out, pos = r.stdout, {}, 0
    while pos < len(data):
        nl = data.find(b"\n", pos)
        if nl < 0:
            break
        head = data[pos:nl].decode("utf-8", "replace").split()
        pos = nl + 1
        if len(head) != 3 or not head[2].isdigit():
            continue
        size = int(head[2])
        out[head[0]] = data[pos:pos + size]
        pos += size + 1
    return out


def check_blobs(root: Path, entries: list[tuple[str, str, str]], extra_prefixes: tuple[str, ...] = ()) -> list[Problem]:
    """entries = [(commit-etichetta, blob sha, path)]. Ogni blob letto una volta sola."""
    problems: list[Problem] = []
    limit = kc.MAX_FILE_MB * 1024 * 1024
    shas = sorted({sha for _c, sha, _p in entries})
    sizes = _blob_sizes(root, shas)
    to_scan = sorted({sha for _c, sha, p in entries
                      if path_reason(p, extra_prefixes) is None and sizes.get(sha, 0) <= MAX_SCAN_BYTES})
    contents = _blob_contents(root, to_scan)
    found_cache: dict[tuple[str, str], list[str]] = {}
    for commit, sha, path in entries:
        why = path_reason(path, extra_prefixes)
        if why:
            problems.append(Problem(commit, path, why))
            continue
        size = sizes.get(sha, 0)
        if size > limit:
            problems.append(Problem(commit, path, f"file di {size / 1048576:.1f} MB (> {kc.MAX_FILE_MB} MB)"))
            continue
        if sha in contents:
            key = (sha, PurePosixPath(path).suffix.lower())
            if key not in found_cache:
                found_cache[key] = secret_kinds(path, contents[sha])
            for k in found_cache[key]:
                problems.append(Problem(commit, path, f"possibile segreto nel contenuto ({k})"))
    return problems


def outgoing_commits(root: Path, ref: str = "HEAD") -> list[str]:
    r = _git(root, "rev-list", "--reverse", ref, "--not", "--remotes")
    if r.returncode != 0:
        raise RepoCheckError(f"git rev-list fallito: {r.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return [x for x in r.stdout.decode().split() if x]


def outgoing_problems(root: Path, ref: str = "HEAD", extra_prefixes: tuple[str, ...] = ()) -> tuple[list[str], list[Problem]]:
    """(commit in uscita, problemi): ogni blob aggiunto/modificato di ogni commit non ancora su un remote."""
    commits = outgoing_commits(root, ref)
    entries: list[tuple[str, str, str]] = []
    for c in commits:
        r = _git(root, "diff-tree", "-r", "--no-commit-id", "--root", "--no-renames", "-z", c)
        if r.returncode != 0:
            raise RepoCheckError(f"git diff-tree {c[:8]} fallito")
        entries += [(c[:8], sha, p) for _st, sha, p in _parse_raw(r.stdout)]
    return commits, check_blobs(root, entries, extra_prefixes)


def staged_problems(root: Path, extra_prefixes: tuple[str, ...] = ()) -> list[Problem]:
    has_head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD").returncode == 0
    args = ["diff", "--cached", "--raw", "--no-renames", "-z", "--abbrev=40"]
    if not has_head:
        args.append("4b825dc642cb6eb9a060e54bf8d69288fbee4904")   # albero vuoto
    r = _git(root, *args)
    return check_blobs(root, [("staging", sha, p) for _st, sha, p in _parse_raw(r.stdout)], extra_prefixes)


def tracked_problems(root: Path, ref: str = "HEAD", extra_prefixes: tuple[str, ...] = ()) -> list[Problem]:
    r = _git(root, "ls-tree", "-r", "-z", ref)
    if r.returncode != 0:
        raise RepoCheckError(f"git ls-tree {ref} fallito: {r.stderr.decode('utf-8', 'replace').strip()[:200]}")
    entries = []
    for rec in r.stdout.split(b"\0"):
        if not rec:
            continue
        meta, _, path = rec.partition(b"\t")
        f = meta.decode().split()
        if len(f) == 3 and f[1] == "blob":
            entries.append((ref, f[2], path.decode("utf-8", "replace")))
    return check_blobs(root, entries, extra_prefixes)


def memory_prefixes(root: Path) -> tuple[str, ...]:
    """Memory dir dei nodi (autoMemoryDirectory) che cadono dentro la radice: path relativi vietati."""
    out: list[str] = []
    try:
        nodes = kc.iter_nodes(root)
    except kc.ConfigError:
        nodes = []
    for n in nodes:
        try:
            md = kc.memory_dir(root, n).resolve()
            rel = md.relative_to(Path(root).resolve()).as_posix()
        except (ValueError, OSError):
            continue
        if rel and rel != "." and rel not in out:
            out.append(rel)
    return tuple(out)


def report(problems: list[Problem], header: str) -> None:
    print(header)
    for p in problems:
        print(p.line())


def main(argv: list[str] | None = None) -> int:
    kc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Controllo applicativo dei file destinati a GitHub (exit 8 se trova problemi).")
    ap.add_argument("--root", default=".", help="radice del repository (default: cwd)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tracked", action="store_true", help="blob dell'albero di --ref (default HEAD): usato dal workflow brain-check")
    g.add_argument("--outgoing", action="store_true", help="blob di ogni commit non ancora su un remote")
    ap.add_argument("--ref", default="HEAD")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    try:
        prefixes = memory_prefixes(root)
        if a.tracked:
            probs = tracked_problems(root, a.ref, prefixes)
            what = f"file tracciati in {a.ref}"
        else:
            commits, probs = outgoing_problems(root, a.ref, prefixes)
            what = f"{len(commits)} commit in uscita"
    except RepoCheckError as e:
        print(f"ERRORE: {e}")
        return 3
    if probs:
        report(probs, f"RIFIUTATO: {len(probs)} problemi nei {what} (commit, path, tipo):")
        return 8
    print(f"OK: nessun file vietato, pesante o con segreti nei {what}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
