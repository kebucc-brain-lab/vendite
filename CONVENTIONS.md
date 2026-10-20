---
title: "Convenzioni del brain"
summary: "Il rulebook unico di questo brain: le cartelle del profilo, la regola di smistamento, la nota atomica, il frontmatter, i link (anche fra nodi), il flusso sources, il journal, la front door e la scala di retrieval."
tags: [meta, type/rules, status/active]
status: active
created: 2026-10-18
updated: 2026-10-18
related: ""
---

# Convenzioni

Regole vincolanti per ogni nota di questo brain. La fonte unica e' il markdown. Obsidian e' solo
una lente visiva sopra gli STESSI file. Qdrant e' l'indice semantico derivato. Nessuno dei due
scrive verita' che il markdown non contenga gia'.

## Le cartelle

Le cartelle ATTIVE di questo brain sono quelle di `brain.profile.yaml` (`folders`): e' l'allow-list
che lint, `llms.txt` e indice rispettano. Il profilo completo (`company`) ha le 11 cartelle qui
sotto; i profili ridotti ne usano un sottoinsieme. Ognuna ha UN SOLO lavoro. Niente cartelle di
note fuori da qui (`scripts/` e le cartelle operative `ops_dirs` sono infrastruttura, fuori dai gate).

| Cartella | Lavoro unico |
|---|---|
| self     | Identita' del business: missione, ICP, offerta, prezzi, posizionamento, voce, obiettivi, storia. |
| areas    | Responsabilita' continue che non finiscono mai. |
| projects | Lavoro a tempo, con un traguardo. Quando finisce, status:archived. |
| sources  | Inbox grezzo immutabile. NON lintato, NESSUN _index. |
| concepts | Idee atomiche, UNA idea per nota. |
| docs     | SOP, playbook, runbook, procedure multi-sezione tenute intere. |
| entities | Schede di nomi propri: persone, clienti, strumenti, piattaforme, competitor. |
| data     | Dataset strutturati: numeri su cui l'agente calcola. |
| code     | Script e automazioni: la scheda-nota al primo livello, il codice VIVO nelle sottocartelle (i gate lo ignorano). |
| outputs  | Deliverable finiti riusabili come modello e come prova. |
| workspace| Scratch, bozze, journal (daily + sessioni). NON lintato, NESSUN _index. |

## Regola di smistamento (la prima riga che matcha vince)

- idea singola atomica -> concepts
- procedura multi-step tenuta intera -> docs
- responsabilita' o standard che non finisce mai -> areas
- chi siamo, offerta, prezzi, posizionamento, obiettivi -> self
- un nome proprio (strumento, persona, cliente, competitor) -> entities
- numeri e tabelle grezzi -> data

## Pattern: sources -> compile -> cartella

sources/ (drop zone, immutabile) -> compile (skill sync) -> note pulite e linkate nelle cartelle.
Le note compilate sono cio' che viene interrogato. Regola d'oro: ZERO conoscenza inventata; ogni
nota compilata nasce da una fonte reale e la cita nel campo `source:`.

## Compilazione: canon prima, hub prima

Quando un'ondata tocca PIU' fonti, o fonti con numeri: 1) CANON, un file di lavoro unico
(`workspace/canon-<YYYY-MM-DD>.md`, zona grezza) coi fatti duri; i numeri devono QUADRARE; se due
fonti si contraddicono si chiede all'utente quale vince; 2) NOTE, hub prima: prima le note-mappa
(self/, areas/), poi i dettagli, ognuno collegato al suo hub e a 2-3 note vicine. La regola "un
wikilink punta a una nota che esiste" si valuta a FINE ondata, quando gira il lint. Ogni
numero-metrica vive in UNA SOLA nota canonica (di solito in data/): le altre la linkano.

## Nota atomica

1 idea per file. Linea guida ~300 righe (il lint avvisa, non blocca). Se per legare il contenuto
serve "e anche", sono due idee: splitta e collega con un wikilink.

## Frontmatter YAML (obbligatorio su ogni nota compilata, non su sources/ e workspace/)

Il campo a piu' alto ROI per il retrieval e' `summary`.

    ---
    title: "Titolo leggibile della nota"
    summary: "Una frase che dice cosa contiene la nota."
    tags: [<cartella>, channel/x (opzionale), tech/x (opzionale), type/x, status/x]
    status: active        # active | stub | archived | derived
    created: 2026-01-01    # PRESERVA la data created esistente
    updated: 2026-01-01
    source: sources/<file>  # provenienza (raccomandato)
    related:                # lista YAML multi-riga, un wikilink quotato per riga
      - (wikilink quotato alla prima nota collegata)
    ---

- title e summary: se contengono ":" seguito da spazio DEVONO stare tra virgolette doppie, altrimenti
  lo YAML e' invalido (il lint lo blocca con il numero di riga). Regola pratica: quotali sempre.
- tags: primo tag = la cartella; poi tag canale/tecnologia opzionali, poi type/x e status/x.
- related: lista multi-riga di wikilink quotati; MAI stringa inline separata da virgole (Obsidian
  la corrompe). Campo vuoto: `related: ""`.
- Niente BOM in testa al file (il lint avvisa).

## Link

- Wikilink inline al primo mention. Minimo 3 link uscenti unici per nota normale (body + related).
- Zero broken link; zero orfani; profondita' max 4 (relativa al nodo); stem UNICO in tutto il
  brain (il lint blocca i duplicati). Grafo a una sola componente: raccomandazione (il lint avvisa).
- I `[[link]]` dentro blocchi di codice e inline code sono esempi: il lint li ignora.
- Esenti da minimo-link e orfano (ma con frontmatter e zero broken link): `_index.md`, README.md e
  CONVENTIONS.md della radice, i deliverable nelle sottocartelle di outputs/, le schede code/<x>.md,
  le note `type/asset` e `status: derived`. CLAUDE.md, AGENTS.md e nodo.md sono FUORI dal lint.
- **Albero di nodi** (master con nodi interni): una nota puo' linkare SOLO note del proprio nodo o
  dei suoi antenati (fino alla radice). Un link verso un nodo fratello o discendente e' un errore:
  i nodi sono domini separati, lo stesso filtro vale per il retrieval semantico.
- I link risolvono per NOME FILE (stem). Prefissi di dominio sugli stem: cliente-..., kpi-..., progetto-...

## Tag canale e tecnologia

I canali e le tecnologie sono TAG (`channel/...`, `tech/...`), mai cartelle: una strategia valida
per piu' canali si tagga, non si duplica.

## Front door e retrieval (nessun tetto di scala)

- ESATTO: `llms.txt` del nodo (DERIVATO da `scripts/build_llms_txt.py`) + grep sulle note:
  `scripts/brain.py ask "..." --exact`.
- SEMANTICO: Qdrant ibrido denso+sparse, embedding LOCALI, filtrato sul nodo e i suoi antenati:
  `scripts/brain.py ask "..." --semantic`. Nessun LLM nel motore: la sintesi la fa l'agente citando
  gli stem; la confidenza stampata e' misurata, non dichiarata.
- Scala: server Qdrant locale -> self-hosted -> cloud, senza toccare il vault. Da migliaia di note
  valgono di piu' i MOC e le viste derivate: aggiunte, mai ristrutturazioni.

## Journal (la memoria viva)

Due strati: STATICA (le note delle entita') e DINAMICA (il diario in `workspace/journal/`). Ogni
sessione lascia una nota in `journal/sessions/`, ogni giornata un daily in `journal/daily/`,
entrambe agganciate alle entita' coi wikilink. Template in `workspace/journal/_templates/`. Le
sessioni entrano nel RAG (`scripts/rag/ingest.py --append-sessions`). Vale la regola del giorno
logico: 00:00-04:59 appartengono ancora a ieri.

## Derivati: si rigenerano, non si editano

`llms.txt`, le note `status: derived` e l'indice Qdrant sono OUTPUT dei gate: non si modificano a
mano e in un conflitto git si prende una versione qualsiasi e si rilancia il gate.

## I gate (sempre col python del venv del brain, mai quello di sistema)

    scripts/lint_kb.py          # 0 errori obbligatori sulle note compilate
    scripts/build_llms_txt.py   # rigenera llms.txt (derivato), uno per nodo
    scripts/rag/ingest.py       # (ri)costruisce l'indice Qdrant, incrementale
Il comando unico che incatena tutto e' `scripts/brain_sync.py` (skill sync). `scripts/selftest.py`
prova l'intera catena in una copia temporanea del brain.
