---
title: "Policy degli accessi"
summary: "Chi vede cosa: accessi solo via team, ruolo minimo, owner 2-3, esterni con scadenza."
tags: [docs, type/asset, status/active]
status: active
created: 2026-10-20
updated: 2026-10-20
related: ""
---

# Policy degli accessi

Data di revisione: 2026-10-20 - Responsabile: (da compilare, vedi `TEAM.md`)

1. Base permission dell'organizzazione = **No permission**; un brain = un repo; i muri si mettono fra repo.
2. Accessi **solo tramite team** (padre `azienda` + figli); un team figlio eredita i permessi del padre,
   quindi il padre ha accesso solo al brain comune, in lettura.
3. Il **ruolo piu' basso** che permette il lavoro; Write a pochi; Admin solo a chi risponde del brain.
4. **Owner 2-3**, mai uno solo; chi deve "leggere tutto" sta nel team `direzione` con il ruolo org
   All-repository read (dove il piano lo consente), non diventa Owner.
5. **Esterni**: outside collaborator su un solo repo, permesso minimo, scadenza scritta in `docs/ACCESSI.md`.
6. Nessuna persona e nessun agente pubblica su `main` senza review (ruleset con bypass vuota).
7. Token solo fine-grained, sui repo necessari, scadenza di 90 giorni al massimo; agenti non supervisionati
   solo con una GitHub App dedicata.
