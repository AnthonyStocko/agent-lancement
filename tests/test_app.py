"""IHM d'agent-lancement : démarrage à blanc, lien du manager (ticket posé), CLI --help."""
import os
import subprocess
import sys

import dotenv
import pytest
from streamlit.testing.v1 import AppTest

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _app(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("AGENT_AUTH", "0")
    return AppTest.from_file(os.path.join(RACINE, "app.py"), default_timeout=120)


def test_page_lancement_demarre(monkeypatch, tmp_path):
    at = _app(monkeypatch, tmp_path).run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.title[0].value == "🚀 Lancement"


def test_lien_du_manager_pose_le_ticket(monkeypatch, tmp_path):
    """Déplacé de test_lien_manager d'agent-scrum-master : le ticket transmis est posé dans « Lancement »."""
    from agent_socle import jira_tools
    monkeypatch.setattr(jira_tools, "get_jira_issue_summary", lambda key: "Titre du ticket")
    at = _app(monkeypatch, tmp_path)
    at.query_params.update({"source": "manager", "ticket": "ODA-12", "contexte": "Cadrer le ticket."})
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["jira_tickets"] == ["ODA-12"]
    text = " ".join(m.value for m in at.markdown)
    assert "Ouvert depuis le manager" in text and "### 2. Sélection du Ticket" in text


def test_cli_aide():
    sortie = subprocess.run([sys.executable, "main.py", "--help"], capture_output=True, text=True, encoding="utf-8",
                            cwd=RACINE)
    assert sortie.returncode == 0 and "{cadrage,epic-cadrage,epic-sync,refine,patch}" in sortie.stdout


def test_patch_recap_cite_les_patchs():
    """Déplacé de test_knowledge_tools (test_main_production_points_helpers) : récapitulatif des patchs."""
    import importlib
    import io
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        main = importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved
    assert "PROJX-1-a.patch" in main.patch_recap("PROJX-1", ["x/PROJX-1-a.patch"], "récapitulatif")


def test_patchs_joints_au_ticket_par_le_programme(monkeypatch, capsys):
    """L'agent n'a pas d'outil Jira : le programme joint lui-même les patchs, et un échec n'arrête pas l'étape."""
    import importlib
    main = importlib.import_module("main")
    appels = []
    monkeypatch.setattr(main, "attach_files", lambda cle, fichiers: appels.append((cle, fichiers)) or
                        [f"Fichier {fichiers[0]} attaché avec succès au ticket {cle}."])
    assert main.attach_patches("PROJX-1", ["PROJX-1-client.patch"])
    assert appels == [("PROJX-1", ["PROJX-1-client.patch"])]
    monkeypatch.setattr(main, "attach_files", lambda *a: ["Erreur d'ajout (403) : refusé"])
    assert not main.attach_patches("PROJX-1", ["PROJX-1-client.patch"])
    monkeypatch.setattr(main, "attach_files", lambda *a: (_ for _ in ()).throw(RuntimeError("réseau")))
    assert not main.attach_patches("PROJX-1", ["PROJX-1-client.patch"])
    assert "réseau" in capsys.readouterr().out
