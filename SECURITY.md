# Sicurezza del brain vendite

- Segreti, chiavi e token non entrano MAI nel repo: stanno in `.env` / `.secrets/` (ignorati da git) o nei
  Secrets di GitHub Actions. `brain_sync` e il workflow `brain-check` rifiutano `.env*` (tranne `.env.example`),
  `.secrets/`, file pesanti e testo che sembra un segreto: e' un controllo applicativo forte, non una garanzia.
- Una credenziale finita in un commit, anche cancellata dopo, resta nella storia: segui
  `docs/credenziale-esposta.md` (revoca prima, pulizia dopo).
- Accessi: solo via team; gli esterni sono outside collaborator con scadenza in `docs/ACCESSI.md`.
- Gli agenti lavorano col token personale di chi li usa (e lo dichiarano); agenti non supervisionati solo
  con una GitHub App dedicata, senza push su main (fuori dal kit).
- Segnalazioni: apri una issue con il template "Segnalazione" senza incollare il segreto; se serve, scrivi
  direttamente a chi risponde del brain (`TEAM.md`).
