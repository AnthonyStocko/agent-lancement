# agent-lancement

Lancement d'un ticket : cadrage (analyse et chiffrage), révision, découpage d'épopée, validation et publication dans
Jira, patch et release note. Code repris tel quel d'agent-scrum-master (phase 5 du découpage) ; `main()` élagué par
script.

| | |
|---|---|
| IHM | `streamlit run app.py` (port 8506) ; lien du manager `?source=manager&ticket=…` appliqué |
| CLI | `python main.py --step cadrage\|refine\|epic-cadrage\|epic-sync\|patch --issue <TICKET> --project <PROJET> …` |
| Dépendance interne | `agent-socle` (seule) : `pip install -e ../agent-socle` pendant le chantier |
| Données | `<TICKET>-cadrage.md`, patchs, `.tmp/` et `.env` de `AGENT_DATA_DIR` (défaut `..\agent-scrum-master`) ; CLI et IHM s'y placent |
| Conventions | `odatio-specific-code-conventions-dev.md` de `AGENT_DATA_DIR` (aussi lu par agent-revue-pr) |

`agent-scrum-master` délègue : `python main.py --step cadrage|refine|epic-cadrage|epic-sync|patch …` relance ce `main.py`
(`AGENT_LANCEMENT_DIR`).
