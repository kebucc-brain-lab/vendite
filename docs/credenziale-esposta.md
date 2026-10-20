---
title: "Credenziale esposta"
summary: "Cosa fare se una chiave finisce in un commit, in una PR o in una issue."
tags: [docs, type/asset, status/active]
status: active
created: 2026-10-20
updated: 2026-10-20
related: ""
---

# Credenziale esposta

Una credenziale finita in un commit resta nella storia anche se il file viene cancellato dopo.

1. **Revoca subito** la credenziale dal servizio che l'ha emessa (prima di ogni pulizia).
2. Creane una nuova e mettila solo in `.env` / `.secrets/` o nei Secrets di Actions.
3. Avvisa chi risponde del brain e chi usa quella credenziale.
4. Se il commit non e' ancora pushato: correggi la storia locale; se e' pushato, la storia si riscrive solo
   con l'ok di chi risponde del brain (e tutti riclonano).
5. Controlla l'audit log e i log del servizio per usi nel periodo di esposizione.
6. Annota l'accaduto e la causa in `decisioni/` o nel journal.

| Tipo | Dove si revoca |
|---|---|
| Token GitHub | Settings, Developer settings, Personal access tokens |
| Chiave di Qdrant o di un'API | console del servizio o rotazione della chiave del server |
| Chiave di un modello (Anthropic, OpenAI) | console del fornitore, sezione API keys |
| Deploy key | Settings del repo, Deploy keys |
