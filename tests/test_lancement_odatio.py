"""Doc ODATiO dans les modèles de Lancement (cas déplacés de test_odatio_docs d'agent-scrum-master)."""
import importlib
import io
import os
import re
import sys

import pytest

RULES = {
    "cadrage": "N'écris rien dans Jira", "refine": "Ne génère AUCUN patch", "epic_cadrage": "Tu ne poses aucune question",
    "patch": "Tu ne dois JAMAIS poser de questions",
}

BASE = "https://docs.exemple.test/doc/produit/development/index.html"


@pytest.fixture(scope="module")
def main_module():
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved


@pytest.mark.parametrize("name", sorted(RULES))
def test_prompts_carry_the_doc_block_and_keep_their_rules(main_module, name):
    with open(os.path.join(main_module.DEFAULT_PROMPTS_DIR, f"{name}.md"), encoding="utf-8") as f:
        names = set(re.findall(r"\$\{(\w+)\}", f.read()))
    assert "odatio_docs_block" in names
    text = main_module.render_prompt(name, **{k: k for k in names - {"odatio_docs_block"}},
                                     odatio_docs_block="BLOC_DOC_ODATIO")
    assert "BLOC_DOC_ODATIO" in text and RULES[name] in text
    # Appel sans la doc : bloc vide, aucune variable inconnue
    assert "odatio_docs_block" not in main_module.render_prompt(name, **{k: k for k in names - {"odatio_docs_block"}})


def test_cadrage_prompt_cites_the_consulted_documentation(main_module):
    with open(os.path.join(main_module.DEFAULT_PROMPTS_DIR, "cadrage.md"), encoding="utf-8") as f:
        assert "**Documentation consultée**" in f.read()
