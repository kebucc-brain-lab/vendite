"""eval -- misura l'affidabilita' del retrieval su un golden set (eval_set.jsonl), senza LLM.

Forkato da `scripts/rag/eval.py` di VMR Brain (HEAD 0efa9254). Cosa cambia:
  - niente LLM: la CITATION-RATE (che misurava la sintesi) e' tolta; restano le metriche
    del RETRIEVAL, le uniche misurabili nel core;
  - HIT-RATE@k: almeno un file atteso tra le FONTI; MRR: posizione del primo file atteso;
  - REFUSAL su due livelli, perche' senza LLM il "rifiuto" e' un segnale, non una frase:
    stretto = confidenza `non_presente`/`bassa`; largo = qualunque cosa diversa da `alta`
    (l'agente che legge non deve affermare un fatto con confidenza non alta);
  - golden set per brain: `scripts/rag/eval_set.jsonl` (vedi eval_set.example.jsonl),
    override con `--set`.

NB: "100%" ha senso SOLO rispetto al set. Aggiungi le domande reali del brain.
Uso:  python scripts/rag/eval.py [--k 8] [--set FILE] [--node P]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kb_config as kc  # noqa: E402
import query as Q  # noqa: E402

DEFAULT_SET = Path(__file__).resolve().parent / "eval_set.jsonl"
REFUSE_LEVELS = {"non_presente", "bassa"}


def load_cases(path: Path) -> list[dict]:
    if not path.exists():
        print(f"Manca {path}. Crealo con righe JSON: question / expected_files / should_refuse "
              f"(esempio: eval_set.example.jsonl).")
        sys.exit(1)
    cases = []
    for ln in kc.read_text(path).splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            cases.append(json.loads(ln))
    return cases


def main(argv: list[str] | None = None) -> int:
    kc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Metriche di retrieval sul golden set.")
    kc.add_common_args(ap)
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--set", default=None, help="file jsonl (default scripts/rag/eval_set.jsonl)")
    a = ap.parse_args(argv)
    root = kc.resolve_root(a.root)
    node = kc.current_node(root, override=a.node)
    kc.load_env(root, node.dir)
    cases = load_cases(Path(a.set) if a.set else DEFAULT_SET)
    r = Q.Retriever(root, node)

    hits_ok = hits_tot = 0
    rr_sum = 0.0
    refuse_ok = refuse_loose = refuse_tot = 0
    for c in cases:
        res = Q.answer(c["question"], k=a.k, retriever=r)
        if c.get("should_refuse"):
            refuse_tot += 1
            ok = res["confidence"] in REFUSE_LEVELS
            refuse_ok += 1 if ok else 0
            refuse_loose += 1 if res["confidence"] != "alta" else 0
        else:
            hits_tot += 1
            exp = set(c.get("expected_files", []))
            got = res["files"]
            ok = bool(exp & set(got))
            hits_ok += 1 if ok else 0
            rank = next((i for i, f in enumerate(got, 1) if f in exp), 0)
            rr_sum += (1.0 / rank) if rank else 0.0
        print(f"[{'OK' if ok else '--'}] {c['question'][:60]}  -> {', '.join(res['files'][:4])}  ({res['confidence']})")

    def pct(x, y):
        return f"{100 * x / y:.0f}%" if y else "n/a"

    print("-" * 60)
    print(f"HIT-RATE@{a.k}: {pct(hits_ok, hits_tot)}  ({hits_ok}/{hits_tot})")
    print(f"MRR@{a.k}:      {(rr_sum / hits_tot):.2f}" if hits_tot else "MRR: n/a")
    print(f"REFUSAL stretto: {pct(refuse_ok, refuse_tot)}  ({refuse_ok}/{refuse_tot}) "
          f"[confidenza in {sorted(REFUSE_LEVELS)}]")
    print(f"REFUSAL largo:   {pct(refuse_loose, refuse_tot)}  ({refuse_loose}/{refuse_tot}) [confidenza != alta]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
