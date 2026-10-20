#!/usr/bin/env python3
"""sessions.py -- estrattore delle sessioni di QUESTO brain (Brain Builder v3.2, SPEC par. 2.5 e par. 12 A2-A4).

    python scripts/sessions.py [--root <brain>] [--node <p>] [--days 30] [--sessions 100] [--max-chars 2000]
                               [--source claude|codex|all] [--projects-dir <dir>] [--codex-sessions-dir <dir>]
                               [--list-slugs] [--json]

Copiato e adattato da `archivio/skills dream/look-back/scripts/extract.py` (che leggeva TUTTI i progetti):
  - ambito = il nodo della sessione (`--node`, altrimenti il nodo.md piu' vicino alla cwd) e i suoi discendenti:
    dalla radice l'intero albero (`kb_config.iter_nodes`), da un nodo solo quel nodo; il nodo rilevato va su stderr. Uno script INSTALLATO in `<root>/scripts/` e' vincolato alla propria radice: `--root` fuori
    da quella radice (realpath) = exit 3. Override libero solo dal kit (`kit/engine/`, nessun brain sopra);
  - Claude: una cartella di `<projects>/` e' candidata se il suo nome e' UGUALE, senza distinzione di
    maiuscole (`d--`/`D--`), allo slug della radice o di un nodo (mai per prefisso: `acme` non e' `acme-2`;
    oltre 200 caratteri confronto sui primi 200) oppure a `CLAUDE_CODE_PROJECT_DIR_NAME` se impostato;
  - ogni transcript candidato si verifica riga per riga: TUTTE le righe con `cwd` devono cadere dentro
    l'ambito (realpath, maiuscole indifferenti, separatori normalizzati). Nessun `cwd` = escluso; `cwd` misti
    (dentro e fuori) = escluso come "misto/ambiguo";
  - Codex (`--source codex|all`): `rollout-*.jsonl` ricorsivo, stessa verifica sui `cwd` di `session_meta`
    e `turn_context`; turni utente da `response_item` con `role: user` (fail-soft);
  - solo i turni dell'utente, filtro rumore dell'archivio piu' quello della SPEC, minimo 15 caratteri;
  - segreti mascherati (`secrets_scan.mask_secrets`) PRIMA di troncare e di stampare; conteggio su stderr.

Il formato dei transcript e' interno a Claude Code / Codex e puo' cambiare: righe illeggibili saltate,
mai fatali. Non legge mai `.env`, non scrive nulla. Exit: 0 ok, 1 nessuna sessione con contenuto,
2 configurazione (radice/nodo) non valida, 3 `--root` fuori dal brain dello script.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_config as kc  # noqa: E402
from secrets_scan import find_secrets, mask_secrets  # noqa: E402,F401  (API condivisa, rieportata qui)

SLUG_MAX = 200
MIN_CHARS = 15
EXIT_ROOT_OUTSIDE = 3

# Turni generati dalla macchina che sembrano messaggi dell'utente (archivio + SPEC par. 2.5).
NOISE = (
    "hook additional context", "Caveat: The messages below",
    "<command-name>", "<local-command", "[Artifact comment",
    "This session is non-interactive", "<task-notification>",
    "Base directory for this skill", "Sessione chiusa automaticamente",
    "Session closed automatically", "system-reminder",
    # aggiunte v3.2: avvisi di sessione non interattiva, output di hook, iniezioni di Codex, risultati di tool
    "sessione non interattiva", "Stop hook feedback", "SubagentStop hook feedback",
    "hook blocking error", "[brain-feed]",
    "<environment_context>", "<user_instructions>", "# AGENTS.md instructions",
    "tool_use_id", "<tool_use_error>",
)


class RootOutside(Exception):
    """`--root` punta fuori dalla radice del brain in cui lo script e' installato."""


# --------------------------------------------------------------------------- slug e percorsi

def slug_of(path: str | Path) -> str:
    """Slug di Claude Code: path assoluto con ogni carattere non [A-Za-z0-9] sostituito da `-`."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(str(path)))


def slug_matches(folder_name: str, slug: str) -> bool:
    """Uguaglianza ESATTA senza distinzione di maiuscole; oltre 200 caratteri si confrontano i primi 200
    (Claude Code tronca e aggiunge un suffisso): la verifica del `cwd` resta comunque obbligatoria."""
    a, b = folder_name.lower(), slug.lower()
    if len(b) > SLUG_MAX:
        return len(a) >= SLUG_MAX and a[:SLUG_MAX] == b[:SLUG_MAX]
    return a == b


def norm_path(p: str | Path) -> str:
    s = str(p).strip().replace("\\", "/")
    if s.startswith("//?/"):
        s = s[4:]
    s = os.path.normpath(s).replace("\\", "/").lower()
    return s.rstrip("/") if len(s) > 3 else s


def canon(p: str | Path) -> str:
    """Forma canonica per i confronti: realpath (junction e symlink risolti) + norm_path."""
    s = str(p).strip()
    if s.startswith("\\\\?\\") or s.startswith("//?/"):
        s = s[4:]
    try:
        s = os.path.realpath(s)
    except (OSError, ValueError):
        s = os.path.abspath(s)
    return norm_path(s)


def is_within(child: str | Path, parent: str | Path) -> bool:
    """Confronto su forme gia' normalizzate o grezze (nessuna risoluzione dei link: usare canon prima)."""
    c, p = norm_path(child), norm_path(parent)
    return c == p or c.startswith(p.rstrip("/") + "/")


def script_home() -> Path | None:
    """Radice del brain in cui QUESTO script e' installato (`<root>/scripts/`); None se gira dal kit."""
    return kc.find_brain_root(Path(__file__).resolve().parent)


def bound_root(cli_root: str | None) -> Path:
    """Radice da usare. Script installato: sempre la propria radice; `--root` ammesso solo se cade dentro
    (realpath), altrimenti RootOutside. Script del kit: `kb_config.resolve_root` (override libero)."""
    home = script_home()
    if home is None:
        return kc.resolve_root(cli_root)
    home_c = canon(home)
    if cli_root and not is_within(canon(cli_root), home_c):
        bid = kc.brain_id(home, strict=False) or home.name
        raise RootOutside("script del brain %s: --root fuori dal brain (%s non e' dentro %s)" % (bid, cli_root, home))
    return Path(os.path.realpath(home))


# --------------------------------------------------------------------------- ambito

def scope(root: Path, node_path: str | None) -> tuple[kc.Node, list[tuple[str, Path]]]:
    """(nodo base, [(node_path, cartella)]): il nodo base e' `--node`, altrimenti il nodo.md piu' vicino alla cwd
    (poi BRAIN_NODE, poi la radice: `kb_config.current_node`); l'ambito e' il nodo base e i suoi discendenti.
    Sessione nella radice = l'intero albero; sessione in un nodo = quel nodo (SPEC v3.2 par. 12, correzione nodi)."""
    nodes = kc.iter_nodes(root)
    base_node = kc.current_node(root, override=node_path or None, nodes=nodes)
    if base_node.is_root:
        sel = nodes
    else:
        sel = [n for n in nodes if n.path == base_node.path or n.path.startswith(base_node.path + "/")]
    return base_node, [(n.path, n.dir) for n in sel]


# --------------------------------------------------------------------------- lettura dei transcript

def _read_lines(f: Path) -> list[str]:
    try:
        with open(f, encoding="utf-8", errors="replace") as fh:
            return fh.readlines()
    except OSError:
        return []


def _loads(line: str):
    try:
        return json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None


def line_cwds(d: dict) -> list[str]:
    """`cwd` dichiarati da una riga (Claude: chiave di primo livello; Codex: anche in `payload`)."""
    out = []
    if isinstance(d.get("cwd"), str) and d["cwd"].strip():
        out.append(d["cwd"])
    p = d.get("payload")
    if isinstance(p, dict) and isinstance(p.get("cwd"), str) and p["cwd"].strip():
        out.append(p["cwd"])
    return out


def classify_cwds(lines: list[str], base_c: str, cache: dict) -> str:
    """'ok' (tutti i cwd dentro l'ambito) | 'nessuno' (nessun cwd) | 'fuori' (tutti fuori) | 'misto'."""
    inside = outside = 0
    for line in lines:
        d = _loads(line)
        if not isinstance(d, dict):
            continue
        for cwd in line_cwds(d):
            if cwd not in cache:
                cache[cwd] = is_within(canon(cwd), base_c)
            if cache[cwd]:
                inside += 1
            else:
                outside += 1
    if not inside and not outside:
        return "nessuno"
    if outside and inside:
        return "misto"
    return "fuori" if outside else "ok"


def is_noise(s: str) -> bool:
    if len(s) < MIN_CHARS:
        return True
    if s.startswith("<") or s.startswith("[Request interrupted"):
        return True
    if "tool_result" in s[:60]:
        return True
    head = s[:200]
    if any(k in head for k in NOISE):
        return True
    if s.startswith("---") and "description:" in s[:400]:
        return True
    return False


def claude_turns(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        d = _loads(line)
        if not isinstance(d, dict) or d.get("type") != "user":
            continue
        if d.get("isMeta") or d.get("isSidechain") or "toolUseResult" in d:
            continue
        content = (d.get("message") or {}).get("content") if isinstance(d.get("message"), dict) else None
        if isinstance(content, list):
            if any(isinstance(p, dict) and p.get("type") == "tool_result" for p in content):
                continue
            content = " ".join(str(p.get("text", "")) for p in content
                               if isinstance(p, dict) and p.get("type") == "text")
        if not isinstance(content, str):
            continue
        s = content.strip()
        if s and not is_noise(s):
            out.append(s)
    return out


def codex_meta(lines: list[str]) -> dict | None:
    for line in lines[:50]:
        d = _loads(line)
        if isinstance(d, dict) and d.get("type") == "session_meta":
            return d.get("payload") if isinstance(d.get("payload"), dict) else d
    return None


def codex_turns(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        d = _loads(line)
        if not isinstance(d, dict):
            continue
        if d.get("type") == "response_item" and isinstance(d.get("payload"), dict):
            item = d["payload"]
        elif d.get("type") == "message":          # formato vecchio, senza involucro
            item = d
        else:
            continue
        if item.get("type") not in (None, "message") or item.get("role") != "user":
            continue
        content = item.get("content")
        parts: list[str] = []
        if isinstance(content, str):
            parts = [content]
        elif isinstance(content, list):
            parts = [str(c.get("text", "")) for c in content
                     if isinstance(c, dict) and c.get("type") in ("input_text", "text")]
        for s in (x.strip() for x in parts):
            if s and not is_noise(s):
                out.append(s)
    return out


# --------------------------------------------------------------------------- raccolta

def _mtime(f: Path) -> float:
    try:
        return f.stat().st_mtime
    except OSError:
        return 0.0


def claude_base(cli: str | None) -> Path:
    if cli:
        return Path(cli)
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if env:
        return Path(env) / "projects"
    return Path.home() / ".claude" / "projects"


def codex_base(cli: str | None) -> Path:
    if cli:
        return Path(cli)
    env = os.environ.get("CODEX_HOME", "").strip()
    if env:
        return Path(env) / "sessions"
    return Path.home() / ".codex" / "sessions"


def claude_candidates(base: Path, slugs: list[str], exact_names: list[str]) -> list[Path]:
    """Cartelle di `base` il cui nome combacia con uno slug dell'ambito o con un nome esplicito
    (`CLAUDE_CODE_PROJECT_DIR_NAME`). Solo il nome: la verifica dei cwd viene dopo, file per file."""
    if not base.is_dir():
        return []
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return []
    low = {n.lower() for n in exact_names}
    return [d for d in entries
            if d.is_dir() and (any(slug_matches(d.name, s) for s in slugs) or d.name.lower() in low)]


def collect(root: Path, a) -> tuple[list[dict], dict]:
    """Sessioni dell'ambito, piu' recenti prima, entro la finestra. Ritorna (sessioni, statistiche)."""
    base_node, nodes = scope(root, a.node)
    base_c = canon(base_node.dir)
    slugs = []
    for _np, d in nodes:
        for form in (d, os.path.realpath(d)):
            s = slug_of(form)
            if s not in slugs:
                slugs.append(s)
    override = os.environ.get("CLAUDE_CODE_PROJECT_DIR_NAME", "").strip()
    exact = [override] if override and "/" not in override and "\\" not in override else []
    stats = {"read": 0, "excluded_foreign": 0, "excluded_mixed": 0, "folders": [], "node": base_node.path,
             "nodes": [np_ for np_, _d in nodes]}
    cache: dict = {}
    files: list[tuple[str, Path, str]] = []          # (source, file, slug)

    def admit(lines: list[str]) -> bool:
        verdict = classify_cwds(lines, base_c, cache)
        if verdict == "ok":
            return True
        if verdict == "misto":
            stats["excluded_mixed"] += 1
        else:
            stats["excluded_foreign"] += 1
        return False

    if a.source in ("claude", "all"):
        for folder in claude_candidates(claude_base(a.projects_dir), slugs, exact):
            ok_any = False
            for f in sorted(folder.glob("*.jsonl")):
                if admit(_read_lines(f)):
                    ok_any = True
                    files.append(("claude", f, folder.name))
            if ok_any:
                stats["folders"].append(folder.name)
    if a.source in ("codex", "all"):
        cb = codex_base(a.codex_sessions_dir)
        if cb.is_dir():
            for f in sorted(cb.rglob("rollout-*.jsonl")):
                lines = _read_lines(f)
                meta = codex_meta(lines) or {}
                cwd = meta.get("cwd") if isinstance(meta.get("cwd"), str) else None
                if not cwd:                            # senza session_meta col cwd: non certificabile
                    stats["excluded_foreign"] += 1
                    continue
                if not admit(lines):
                    continue
                label = "codex:" + slug_of(cwd)
                files.append(("codex", f, label))
                if label not in stats["folders"]:
                    stats["folders"].append(label)
    if a.days and a.days > 0:
        cutoff = dt.datetime.now().timestamp() - a.days * 86400
        files = [x for x in files if _mtime(x[1]) >= cutoff]
    files.sort(key=lambda x: _mtime(x[1]), reverse=True)
    files = files[: max(a.sessions, 0)] if a.sessions else files
    stats["read"] = len(files)
    sessions: list[dict] = []
    masked = 0
    for source, f, slug in files:
        lines = _read_lines(f)
        raw = claude_turns(lines) if source == "claude" else codex_turns(lines)
        turns = []
        for t in raw:
            t, n = mask_secrets(t)                    # mascherare PRIMA di troncare
            masked += n
            turns.append(t[: a.max_chars] if a.max_chars and a.max_chars > 0 else t)
        if not turns:
            continue
        sid = f.stem
        if source == "codex":
            meta = codex_meta(lines) or {}
            sid = str(meta.get("id") or meta.get("session_id") or f.stem.split("-")[-1])
        sessions.append({
            "source": source, "slug": slug, "session_id": sid,
            "date": dt.datetime.fromtimestamp(_mtime(f)).strftime("%Y-%m-%d"),
            "turns": turns,
        })
    stats["masked"] = masked
    return sessions, stats


def _node_line(stats: dict) -> str:
    return "nodo rilevato: %s (ambito: %s)" % (stats["node"], ", ".join(stats["nodes"]))


def _excluded_line(stats: dict) -> str:
    return ("%d file esclusi (cwd assente o fuori dal brain), %d esclusi (misti/ambigui)"
            % (stats["excluded_foreign"], stats["excluded_mixed"]))


def main(argv: list[str] | None = None) -> int:
    kc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Turni dell'utente nelle sessioni recenti di QUESTO brain (solo lettura).")
    kc.add_common_args(ap)
    ap.add_argument("--days", type=int, default=30, help="solo sessioni toccate negli ultimi N giorni (0 = tutte; default 30)")
    ap.add_argument("--sessions", type=int, default=100, help="massimo di sessioni lette (0 = nessun limite; default 100)")
    ap.add_argument("--max-chars", type=int, default=2000, help="tronca ogni messaggio DOPO il mascheramento (default 2000)")
    ap.add_argument("--source", choices=["claude", "codex", "all"], default="claude")
    ap.add_argument("--projects-dir", default=None, help="cartella dei progetti Claude (default $CLAUDE_CONFIG_DIR/projects o ~/.claude/projects)")
    ap.add_argument("--codex-sessions-dir", default=None, help="cartella delle sessioni Codex (default $CODEX_HOME/sessions o ~/.codex/sessions)")
    ap.add_argument("--list-slugs", action="store_true", help="stampa solo gli slug/cartelle selezionati")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        root = bound_root(a.root)
        sessions, stats = collect(root, a)
    except RootOutside as e:
        print("[sessions] ERRORE: %s" % e, file=sys.stderr)
        return EXIT_ROOT_OUTSIDE
    except kc.ConfigError as e:
        print("[sessions] ERRORE: %s" % e, file=sys.stderr)
        return 2

    if a.list_slugs:
        for s in stats["folders"]:
            print(s)
        print("%d cartelle/slug del brain; %s; %s" % (len(stats["folders"]), _excluded_line(stats), _node_line(stats)),
              file=sys.stderr)
        return 0 if stats["folders"] else 1

    if a.json:
        print(json.dumps(sessions, ensure_ascii=False, indent=2))
    else:
        for s in sessions:
            print("\n### %s  (%s)  %s" % (s["slug"], s["date"], s["session_id"][:8]))
            for t in s["turns"]:
                print("- " + t.replace("\n", "\n  "))
    print("\n---\n%d sessioni con contenuto, su %d lette; %d segreti mascherati; %s"
          % (len(sessions), stats["read"], stats["masked"], _excluded_line(stats)), file=sys.stderr)
    print(_node_line(stats), file=sys.stderr)
    if not sessions:
        print("Nessuna sessione di questo brain nella finestra: prova un --days piu' ampio.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
