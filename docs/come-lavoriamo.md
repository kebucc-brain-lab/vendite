---
title: "Come lavoriamo"
summary: "Regola 1: su main non si scrive mai. Flusso issue, branch, PR, review, squash."
tags: [docs, type/asset, status/active]
status: active
created: 2026-10-20
updated: 2026-10-20
related: ""
---

# Come lavoriamo sul brain vendite

**Regola 1: su `main` non si scrive mai.** Ogni modifica passa da una pull request approvata da un'altra
persona con permesso di scrittura (ruleset `main-protetto`, bypass vuota: vale anche per gli owner e per gli agenti).

## Flusso

1. **Issue** con il template "Segnalazione" (cosa succede, cosa dovrebbe succedere, impatto).
2. **Branch** dal `main` aggiornato (`git fetch` + `git status -sb` prima di crearlo): nome in kebab-case che
   descrive il lavoro, non la persona. `brain_sync` usa da solo i branch `sync/<giorno>-<login>`.
3. **PR** verso `main` con il template (Cosa cambia / Perche' / Come si verifica / Rischio; `Closes #n`).
4. **Review di merito** di un code owner diverso dall'autore; il check `brain-check` deve essere verde.
5. **Squash and merge** (unico metodo ammesso); il branch si cancella da solo.

## Con brain_sync

`brain_sync.py` in modalita' `org` committa sul branch `sync/<giorno>-<login>`, lo pusha, apre la PR e si
ferma. Se la PR e' aperta i giri successivi la aggiornano; dopo il merge riparte da `main`.

## Regole per chi usa un agente

- Mostra il diff e aspetta l'ok prima del push; sui conflitti ti fermi e mostri i marcatori.
- Mai reset o force push su branch condivisi: si annulla con `git revert`.
- Se qualcosa fallisce ci si ferma e si spiega; si verifica sul server (`gh pr view`, `git ls-remote`).
