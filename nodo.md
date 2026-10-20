---
brain_id: "vendite"
name: "Offerte, listini e clienti dell'area vendite"
mode: federated
kind: company
node_path: "."
template_version: "3.2.0-rc1"
created: 2026-10-20
---

# Offerte, listini e clienti dell'area vendite

Identita' di questo nodo. La leggono `doctor.py`, `brain_open.py` e il motore (`kb_config.find_node`)
per fissare `node_path` e il dominio della sessione. Non spostare e non rinominare questo file.

| Campo | Significato |
|---|---|
| `brain_id` | identificativo del brain; per un nodo interno e' quello del master |
| `mode` | `master`, `federated` o `internal` |
| `kind` | `project`, `brand`, `business`, `client`, `company`: decide il profilo del vault e le skill di tipo |
| `node_path` | `.` per la radice; per un nodo interno il percorso relativo al master, separatore `/` |
| `template_version` | stringa quotata (mai un numero): versione dei template che hanno generato il nodo |
