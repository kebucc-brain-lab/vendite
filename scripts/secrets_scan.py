#!/usr/bin/env python3
"""secrets_scan.py -- riconoscimento e mascheramento dei segreti nel testo (Brain Builder v3.2).

API STABILE (usata da sessions.py e dream.py; prevista per la scansione dei commit in uscita di brain_sync):

    find_secrets(text: str) -> list[tuple[str, int, int]]
        Elenco ordinato e SENZA sovrapposizioni di (tipo, start, end): `text[start:end]` e' il valore da
        nascondere (per le assegnazioni il nome della chiave resta fuori dallo span).
    mask_secrets(text: str) -> tuple[str, int]
        Testo con ogni span sostituito da `[SEGRETO-MASCHERATO:<tipo>]` e numero di sostituzioni.
    MASK_PREFIX = "[SEGRETO-MASCHERATO:"

Tipi: chiave-privata, url-credenziali, authorization, bearer, jwt, github, anthropic, openai, aws, slack,
google, assegnazione. Riconosce: blocchi `-----BEGIN ... PRIVATE KEY-----` anche multilinea (fino a END o alla
fine del testo), credenziali negli URL (`://user:pass@`), header `Authorization: Bearer|Basic|Token ...`,
token con prefisso noto, e assegnazioni a chiavi sensibili (`api_key`, `token`, `secret`, `password`, `passwd`,
`pwd`, `access_key`, `private_key`, `client_secret`, anche con prefisso come `QDRANT_API_KEY`) in forma
`k=v`, `k: v`, JSON/YAML con chiave e/o valore quotati (`"password": "a b"`, `password: 'a b'`), valori da
1 carattere in su.

Mascherare SEMPRE prima di troncare. Solo stdlib. Modello di minaccia: errori, non un avversario: e' un
controllo applicativo forte, non una garanzia (formati sconosciuti possono sfuggire).
"""
from __future__ import annotations

import re

MASK_PREFIX = "[SEGRETO-MASCHERATO:"

_KEYWORDS = r"(?:api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|token|secret|password|passwd|pwd)"

# (tipo, regex, gruppo del valore: 0 = tutto il match)
_PATTERNS: list[tuple[str, re.Pattern, str | int]] = [
    ("chiave-privata", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)"), 0),
    ("url-credenziali", re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://(?P<v>[^\s/@:]+:[^\s/@]+)@"), "v"),
    ("authorization", re.compile(r"(?i)\bauthorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic|token|digest)\s+(?P<v>[^\s\"',;]+)"), "v"),
    ("bearer", re.compile(r"(?i)\bbearer\s+(?P<v>[A-Za-z0-9._~+/=-]{8,})"), "v"),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), 0),
    ("github", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), 0),
    ("github", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), 0),
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}"), 0),
    ("openai", re.compile(r"\bsk-proj-[A-Za-z0-9_-]{10,}"), 0),
    ("openai", re.compile(r"\bsk-[A-Za-z0-9]{20,}"), 0),
    ("aws", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 0),
    ("slack", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), 0),
    ("google", re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"), 0),
    # assegnazioni: chiave (eventualmente quotata) + separatore + valore quotato o nudo (>= 1 carattere)
    ("assegnazione", re.compile(
        r"(?i)(?<![\w.-])[\"']?[\w.-]*?" + _KEYWORDS + r"[\"']?\s*[:=]\s*"
        r"(?:\"(?P<dq>(?:[^\"\\\n]|\\.)+)\"|'(?P<sq>[^'\n]+)'|(?P<bare>[^\s\"',;}\]]+))"), ("dq", "sq", "bare")),
]


def find_secrets(text: str) -> list[tuple[str, int, int]]:
    """Span (tipo, start, end) dei segreti, in ordine di posizione e senza sovrapposizioni. In caso di
    sovrapposizione vince il pattern che compare prima nella tabella (il piu' specifico)."""
    spans: list[tuple[str, int, int]] = []
    for kind, rx, grp in _PATTERNS:
        for m in rx.finditer(text):
            if isinstance(grp, tuple):
                g = next((g for g in grp if m.group(g) is not None), None)
                if g is None:
                    continue
                s, e = m.span(g)
            elif grp == 0:
                s, e = m.span()
            else:
                s, e = m.span(grp)
            if e <= s:
                continue
            if text.startswith(MASK_PREFIX, s):
                continue
            if any(s < pe and ps < e for _k, ps, pe in spans):
                continue
            spans.append((kind, s, e))
    spans.sort(key=lambda x: x[1])
    return spans


def mask_secrets(text: str) -> tuple[str, int]:
    """Sostituisce ogni segreto riconosciuto con `[SEGRETO-MASCHERATO:<tipo>]`. Ritorna (testo, quanti)."""
    spans = find_secrets(text)
    out = text
    for kind, s, e in reversed(spans):
        out = out[:s] + MASK_PREFIX + kind + "]" + out[e:]
    return out, len(spans)


if __name__ == "__main__":  # filtro da riga di comando: stdin -> stdout mascherato, conteggio su stderr
    import sys
    data = sys.stdin.read()
    masked, n = mask_secrets(data)
    sys.stdout.write(masked)
    print(f"{n} segreti mascherati", file=sys.stderr)
