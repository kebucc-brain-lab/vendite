---
title: "Pagella"
summary: "Le 12 voci di governance del brain d'organizzazione, da spuntare a ogni audit."
tags: [docs, type/asset, status/active]
status: active
created: 2026-10-20
updated: 2026-10-20
related: ""
---

# Pagella (12 voci)

| # | Voce | Come si verifica |
|---|---|---|
| 1 | Piano GitHub Team se i brain sono privati (ruleset e protezioni valgono solo cosi') | `github_org.py plan` |
| 2 | Base permission = No permission | `github_org.py verify` |
| 3 | Owner 2-3 con motivazione | `github_org.py audit` |
| 4 | 2FA obbligatoria (app o passkey) con preavviso di una settimana: si attiva a mano, non dal kit | impostazioni dell'org |
| 5 | Accessi solo via team, nessun accesso diretto non dichiarato | `verify` (matrice) |
| 6 | Esterni come outside collaborator con scadenza in `docs/ACCESSI.md` | `audit` |
| 7 | Ruleset `main-protetto` attivo, bypass vuota, check `brain-check` obbligatorio | `verify` |
| 8 | Push ruleset `niente-file-pesanti` (Team + privati) | `verify` |
| 9 | Secret scanning, push protection e Dependabot alerts attivi | `verify` |
| 10 | CODEOWNERS su main con team che hanno Write e almeno 2 persone | `verify` |
| 11 | Token fine-grained con scadenza; agenti non supervisionati con GitHub App | revisione manuale |
| 12 | Audit log mensile e audit semestrale fatti | `docs/audit-mensile.md` |
