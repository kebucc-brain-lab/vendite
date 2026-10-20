---
title: "Offerte, listini e clienti dell'area vendite"
summary: "Cos'e' questo brain (vendite), le cartelle attive del profilo company e i comandi per usarlo."
tags: [meta, type/readme]
status: active
created: 2026-10-20
updated: 2026-10-20
related: ""
---

# Offerte, listini e clienti dell'area vendite

Brain `vendite` (modalita' `federated`, tipo `company`, nodo `.`), generato con
Brain Builder 3.2.0-rc1 il 2026-10-20. Fonte unica di verita' in markdown + indice
semantico locale (Qdrant, embedding sul proprio computer). Le regole stanno in `CONVENTIONS.md`.

## Cartelle attive (dal profilo `brain.profile.yaml`)

- `self/`: Identita' dell'azienda: missione, ICP, offerta, prezzi, posizionamento, voce, obiettivi, storia.
- `areas/`: Responsabilita' continue che non finiscono mai.
- `projects/`: Lavoro a tempo, con un traguardo; quando finisce, status: archived.
- `concepts/`: Idee atomiche, una idea per nota.
- `docs/`: SOP, playbook, runbook, procedure multi-sezione tenute intere.
- `entities/`: Schede di nomi propri: persone, clienti, strumenti, piattaforme, competitor.
- `data/`: Dataset strutturati: numeri su cui l'agente calcola, una nota canonica per metrica.
- `code/`: Script, skill, automazioni che l'azienda possiede, come schede-nota.
- `outputs/`: Deliverable finiti riusabili come modello e come prova.
- `sources/`: Inbox grezzo immutabile. Non lintato, nessun _index.
- `workspace/`: Scratch, bozze, journal (daily + sessioni). Non lintato, nessun _index.

Ogni cartella ha UN SOLO lavoro (tabella in `CONVENTIONS.md`) e un `_index.md`; `sources/` e
`workspace/` sono zone grezze (non lintate). `scripts/` e' il motore, le cartelle operative
(`ops_dirs`) sono il lavoro corrente osservato dalla sentinella.

## Comandi (dal python del venv del brain)

| Skill / comando | Cosa fa |
|---|---|
| `vendite-sync` -> `scripts/brain_sync.py` | compila `sources/`, poi lint -> llms.txt -> indice -> commit con pathspec |
| `vendite-query` -> `scripts/brain.py ask` | risposta dal brain: esatto (`--exact`) poi semantico (`--semantic`) |
| `vendite-journal` | buongiorno (briefing), chiudi sessione, fine giornata |
| `vendite-feed` -> `scripts/feed_check.py` | porta nel vault il lavoro operativo segnalato dalla sentinella Stop |
| `vendite-doctor` -> `scripts/doctor.py` | diagnosi del nodo (settings, guardia, memoria, antenati) |
| `scripts/selftest.py --root .` | prova l'intera catena in una copia temporanea del brain |

Primi passi dopo la generazione: compilare `.env` (da `.env.example`), avviare o indicare il
server Qdrant, installare la guardia, poi `scripts/selftest.py` e `scripts/doctor.py`.
