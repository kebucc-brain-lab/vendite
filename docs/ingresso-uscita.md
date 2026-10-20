---
title: "Ingresso e uscita delle persone"
summary: "Passi per far entrare e uscire una persona chiudendo tutte e 5 le strade di accesso."
tags: [docs, type/asset, status/active]
status: active
created: 2026-10-20
updated: 2026-10-20
related: ""
---

# Ingresso e uscita

## Ingresso

1. Riga in `TEAM.md` (persona, ruolo, cosa serve davvero) e in `org.yaml` nel team giusto.
2. `github_org.py plan`, conferma di chi risponde, poi `github_org.py apply` (invito come Member).
3. La persona accetta l'invito, attiva la 2FA (app o passkey, mai SMS) e verifica `gh auth status`.
4. `github_org.py verify --as-current` lanciato dalla persona: vede solo i brain attesi (gli altri 404).

## Uscita (9 passi)

1. `github_org.py offboard --login <u>`: elenco delle 5 strade (team, team padre, accesso diretto,
   membership, outside collaborator) e degli inviti pendenti.
2. Conferma, poi `offboard --login <u> --execute`.
3. Togli la persona da `org.yaml` (altrimenti il prossimo `apply` la reinvita) e da `TEAM.md`.
4. Revoca token, deploy key e GitHub App che usava per l'org.
5. Ruota le credenziali che conosceva (Secrets di Actions, chiavi in `.env` delle macchine condivise).
6. Riassegna le sue zone in `.github/CODEOWNERS` (con una PR).
7. `github_org.py verify`: la matrice non la contiene piu'.
8. `github_org.py audit`: nessuna voce residua.
9. Il giorno dopo: audit log dell'org (`docs/audit-mensile.md`) per accessi dopo l'uscita.

Cosa GitHub non verifica: clone gia' scaricati sui suoi computer, copie locali di `.env`, token creati fuori dall'org.
