# agent-lancement

Lancement d'un ticket : cadrage (analyse et chiffrage), révision, découpage d'épopée, validation et publication dans
Jira, patch et release note. Code repris tel quel d'agent-scrum-master (phase 5 du découpage) ; `main()` élagué par
script.

| | |
|---|---|
| IHM | `streamlit run app.py` (port 8506) ; lien du manager `?source=manager&ticket=…` appliqué |
| CLI | `python main.py --step cadrage\|refine\|epic-cadrage\|epic-sync\|patch --issue <TICKET> --project <PROJET> …` |
| Dépendance interne | `agent-socle` (seule) : `pip install -e ../agent-socle` pendant le chantier |
| Venv | `venv\` de ce dépôt (le manager lance l'agent avec lui) : `py -3.12 -m venv venv`, `requirements.txt`, socle en éditable |
| Données | `<TICKET>-cadrage.md`, patchs, `.tmp/` et `.env` de `AGENT_DATA_DIR` (`C:\agent-data` ; défaut `..\agent-data`) ; CLI et IHM s'y placent |
| Conventions | `odatio-specific-code-conventions-dev.md` de `AGENT_DATA_DIR` (aussi lu par agent-revue-pr) |

Point d'entrée unique de ces étapes : le manager redirige vers cette IHM (8506). agent-scrum-master, qui y déléguait,
est supprimé.
