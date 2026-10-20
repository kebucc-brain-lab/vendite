---
title: "Audit mensile"
summary: "Controllo mensile dell'audit log e audit semestrale degli accessi con github_org.py audit."
tags: [docs, type/asset, status/active]
status: active
created: 2026-10-20
updated: 2026-10-20
related: ""
---

# Audit mensile

Ogni mese (audit log dell'org: Settings, Logs, Audit log; conservazione 180 giorni), cinque query:

1. Nuovi Owner (`action:org.update_member` con ruolo admin).
2. Ruleset modificati o disattivati (`action:repository_ruleset`).
3. Base permission cambiata (`action:org.update_default_repository_permission`).
4. Repo resi pubblici (`action:repo.access`).
5. App installate o autorizzate (`action:integration_installation`).

Ogni sei mesi: `github_org.py audit --config org.yaml` (Owner, Admin per repo, accessi diretti, outside
collaborator con scadenza, base permission, CODEOWNERS validi) e si tolgono le voci elencate.
