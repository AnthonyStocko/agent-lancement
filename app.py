"""IHM d'agent-lancement (port 8506) : onglet Lancement d'agent-scrum-master repris tel quel.

Recherche et sélection du ticket, cadrage, révision, validation et publication de l'analyse, épopées, patch ;
réglages du chiffrage. Lancée depuis son dépôt, l'IHM se place dans AGENT_DATA_DIR (fichiers '<TICKET>-cadrage.md',
patchs… au même endroit qu'avant) et lance son propre main.py. Lien du manager (?ticket=…) appliqué au ticket.
"""
import logging
import os
import streamlit as st
import sys
import time
from agent_socle import jenkins_tools, lien_manager
from agent_socle.ihm import render_technical_journal, run_main_step, search_criteria_form, search_jira_tickets
from agent_socle.jenkins_tools import trigger_cucumber_job
from agent_socle.jira_tools import JiraSearchError, get_jira_issue_summary, std_only_projects
from agent_socle.memory_tools import find_similar_tickets, get_project_lessons, load_ticket_memory, record_manual_edit, restore_cadrage_file, should_open_revision
from tools import analysis_publication, estimation
from tools.epic_tools import draft_tasks, load_epic_manifest, missing_analyses, task_analysis
from dotenv import load_dotenv

from agent_socle import auth_tools, config_commune
from agent_socle.chemins import DATA_DIR

st.set_page_config(page_title="Lancement", page_icon="🚀", layout="wide")

# CLI de l'agent (chemin absolu : l'IHM travaille dans AGENT_DATA_DIR)
AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_PY = os.path.join(AGENT_DIR, "main.py")
if os.path.abspath(os.getcwd()) == AGENT_DIR:
    os.chdir(DATA_DIR)

# Même chargement que les autres agents : .env de AGENT_DATA_DIR, puis les accès transmis par le manager
ENV_PATH = os.path.join(DATA_DIR, ".env")
os.environ.setdefault("AGENT_SHELL_CLAUDE_CONFIG_DIR", os.environ.get("CLAUDE_CONFIG_DIR", ""))
logging.getLogger("dotenv.main").setLevel(logging.ERROR)
load_dotenv(ENV_PATH, override=True)
config_commune.reappliquer()
if not os.getenv("CLAUDE_CONFIG_DIR", "").strip():
    os.environ["CLAUDE_CONFIG_DIR"] = os.environ["AGENT_SHELL_CLAUDE_CONFIG_DIR"]

# Connexion OIDC (AGENT_AUTH) : rien n'est affiché ni exécuté avant elle
CONNECTED = auth_tools.require_login(st)
NAV_KEY = "nav_page"
st.session_state[NAV_KEY] = "lancement"


def apply_manager_link() -> None:
    """Lien prérempli d'agent-manager-savoye (ticket posé dans « Lancement », contexte affiché), une fois par session."""
    link = lien_manager.lire(st.query_params.to_dict(), ("lancement",))
    if not link or st.session_state.get("manager_link_signature") == lien_manager.signature(link):
        return
    st.session_state["manager_link_signature"] = lien_manager.signature(link)
    st.session_state["manager_link"] = link
    ticket = link.get("ticket")
    if ticket:
        title = get_jira_issue_summary(ticket)
        project = lien_manager.projet_jira(ticket)
        st.session_state.update(jira_tickets=[ticket], jira_titles={ticket: title} if title else {},
                                jira_context={}, selected_index=0, selected_jira_project=project,
                                workflow_step="INIT", search_project=project)
        st.session_state.pop("ticket_selectbox", None)


# Variables indispensables : leur absence est signalée dans l'onglet Lancement
REQUIRED_ENV = ["JIRA_DOMAIN", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY",
                "BITBUCKET_URL", "BITBUCKET_HTTP_TOKEN", "BITBUCKET_STD_REPO_PATH"]


def format_ticket_option(key: str) -> str:
    """Formatte l'affichage d'un ticket Jira sous la forme 'CLÉ - Titre/Summary'."""
    if not key:
        return ""
    jira_titles = st.session_state.get("jira_titles", {})
    summary = jira_titles.get(key)
    if not summary:
        summary = get_jira_issue_summary(key)
        if summary:
            if "jira_titles" not in st.session_state:
                st.session_state["jira_titles"] = {}
            st.session_state["jira_titles"][key] = summary
    return f"{key} - {summary}" if summary else key


def show_release_note(ticket: str, since: float) -> None:
    """Affiche la release note rédigée avec le patch (ajoutée par main.py en fin de description Jira)."""
    path = f"{ticket}-release-note.md"
    if not os.path.exists(path) or os.path.getmtime(path) < since:
        st.warning(f"Aucune release note produite pour {ticket} lors de cette exécution.", icon=":material/campaign:")
        return
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        note = f.read().strip()
    with st.container(border=True):
        st.markdown("**:material/campaign: Release note** :gray[(ajoutée à la fin de la description Jira)]")
        st.markdown(note or "_Fichier vide._")


def show_estimation(text: str, project: str) -> None:
    """Résumé du chiffrage de l'analyse (total probable et fourchette) et problèmes de la section."""
    from tools import estimation
    if not text or os.getenv("ESTIMATION_ENABLED", "1").strip() == "0":
        return
    config = estimation.settings(project)
    # Total et unité recalculés sans IA à chaque écriture : seuls les autres manques sont signalés
    text = estimation.normalize(text, config["unite"])[0]
    parsed = estimation.parse(text)
    problems = estimation.check(text, config)
    if parsed["lignes"]:
        sums = estimation.totals(parsed)
        st.caption(f"Chiffrage : {sums['probable']:g} {config['unite']} probables (fourchette {sums['basse']:g} – "
                   f"{sums['haute']:g}), confiance {parsed['confiance'] or '?'} — écrit dans Jira "
                   f"({config['champ']}) seulement à la validation de l'analyse.")
    if problems:
        st.warning("Chiffrage à compléter : " + " ; ".join(problems), icon=":material/calculate:")


def show_epic_estimation(tasks: list, project: str) -> None:
    """Chiffrage de l'épopée : total de chaque tâche et total général (somme des tâches), sans changer le découpage."""
    from tools import estimation
    if not tasks or os.getenv("ESTIMATION_ENABLED", "1").strip() == "0":
        return
    texts = []
    for index, task in enumerate(tasks, 1):
        text = task_analysis(task)[1]
        texts.append((f"{task.get('key') or f'Tâche {index}'} — {task.get('summary', '')}", text))
    missing = [title for title, text in texts if text.strip() and not estimation.parse(text)["lignes"]]
    st.markdown(estimation.epic_summary(texts, project))
    if missing:
        st.caption("Sans chiffrage : " + ", ".join(missing) + " — demandez une révision (ex. « Ajoute le chiffrage de "
                   "chaque tâche, ne modifie pas le découpage »).")


def show_publication_status(issue_key: str, text: str = None) -> None:
    """Statut de l'analyse : brouillon local ou publiée dans Jira (redevient brouillon si révisée après)."""
    state = analysis_publication.status(issue_key, text)
    if state["publiee"]:
        st.markdown(f":green[:material/cloud_done: {state['libelle']}]")
    else:
        st.markdown(f":orange[:material/edit_note: {state['libelle']}] — publiée dans Jira seulement à la validation.")


def validate_epic_split(epic_key: str, project: str, client_repo: str, db_schema: str) -> bool:
    """Validation du découpage : crée les tâches manquantes dans Jira et publie les analyses modifiées (epic-sync)."""
    st.info(f"Validation du découpage de {epic_key} : création des tâches et publication des analyses dans Jira...")
    returncode = run_main_step([
        sys.executable, MAIN_PY,
        "--issue", epic_key,
        "--project", project,
        "--client-repo", client_repo,
        "--step", "epic-sync",
        "--db-schema", db_schema
    ])
    if returncode != 0:
        st.error("La création des tâches a échoué : consultez le journal technique (en haut de l'onglet). Les tâches "
                 "déjà créées sont conservées et réutilisées à la relance.")
    return returncode == 0


def run_epic_patches(epic_key: str, project: str, client_repo: str, db_schema: str) -> bool:
    """Génère le patch de chaque tâche enfant de l'épopée, dans l'ordre du découpage. S'arrête au premier échec."""
    tasks = [t for t in load_epic_manifest(epic_key).get("tasks", []) if t.get("key")]
    if not tasks:
        st.error(f"Aucune tâche enfant créée pour {epic_key} : lancez d'abord l'analyse de l'épopée.")
        return False
    for index, task in enumerate(tasks, 1):
        st.info(f"[{index}/{len(tasks)}] Génération du patch de {task['key']} — {task.get('summary', '')}...")
        patch_started_at = time.time()
        returncode = run_main_step([
            sys.executable, MAIN_PY,
            "--issue", task["key"],
            "--project", project,
            "--client-repo", client_repo,
            "--step", "patch",
            "--db-schema", db_schema
        ])
        if returncode != 0:
            remaining = ", ".join(t["key"] for t in tasks[index:]) or "aucune"
            st.error(f"La génération du patch de {task['key']} a échoué (code {returncode}). Tâches non traitées : {remaining}.")
            return False
        st.success(f"Patch généré et attaché au ticket Jira {task['key']}.")
        show_release_note(task["key"], patch_started_at)
    return True


def render_manager_link() -> None:
    """Bandeau du lien du manager : ce qui a été transmis, masquable."""
    link = st.session_state.get("manager_link")
    if not link:
        return
    with st.container(border=True):
        details = " · ".join(x for x in (link.get("ticket"), link.get("projet")) if x)
        st.markdown("**Ouvert depuis le manager**" + (f" — {details}" if details else ""))
        if link.get("contexte"):
            st.caption(link["contexte"])
        if link.get("ticket") and st.session_state.get(NAV_KEY) == "lancement":
            st.caption(f"Ticket {link['ticket']} posé dans « Lancement » : rien n'est lancé sans toi.")
        if st.button("Masquer", key="manager_link_hide", type="tertiary"):
            st.session_state.pop("manager_link", None)
            st.rerun()


def render_estimation_settings() -> None:
    """Réglages du chiffrage par projet : unité, champ Jira cible, activités incluses."""
    from tools import estimation
    with st.expander("Chiffrage des analyses, par projet", icon=":material/calculate:"):
        project = st.text_input("Projet Jira", value=os.getenv("JIRA_PROJECT_KEY", ""), key="estimation_project")
        if not project.strip():
            return
        config = estimation.settings(project)
        with st.form(f"estimation_settings_{project.strip().upper()}", border=False):
            unit = st.radio("Unité", list(estimation.UNITS), horizontal=True,
                            index=list(estimation.UNITS).index(config["unite"]))
            field = st.text_input("Champ Jira de l'estimation", value=config["champ"],
                                  help="timeoriginalestimate (estimation initiale) ou champ des points d'histoire "
                                       "(ex. customfield_10016). La valeur probable y est écrite à la validation "
                                       "de l'analyse, jamais avant.")
            hours = st.number_input("Heures par jour-homme", min_value=1.0, max_value=24.0, step=0.5,
                                    value=float(config["heures_par_jour"]))
            labels = dict(estimation.ACTIVITIES)
            chosen = st.multiselect("Activités incluses", list(labels), default=config["activites"],
                                    format_func=lambda k: labels[k])
            if st.form_submit_button("Enregistrer", icon=":material/save:"):
                estimation.save_settings(project, {"unite": unit, "champ": field.strip(), "activites": chosen,
                                                   "heures_par_jour": hours})
                st.toast(f"Chiffrage du projet {project.strip().upper()} enregistré.", icon=":material/check_circle:")


st.title("🚀 Lancement")
if CONNECTED:
    st.caption(f"Connecté : {auth_tools.label(CONNECTED)}")
apply_manager_link()
render_manager_link()
render_technical_journal()
missing_env = [name for name in REQUIRED_ENV if not os.getenv(name, "").strip()]
if missing_env:
    st.warning("Configuration incomplète : renseignez " + ", ".join(missing_env)
               + " dans l'onglet « ⚙️ Configuration ».", icon=":material/settings:")

st.markdown("### 1. Critères de Recherche Jira")

search_criteria = search_criteria_form("")
project_key = search_criteria["project_key"]

search_clicked = st.button("🔍 Rechercher les tickets", type="primary", use_container_width=True)

if search_clicked:
    with st.spinner(f"Recherche Jira sur le projet {project_key}..."):
        try:
            tickets_data = search_jira_tickets(search_criteria)
            search_error = None
        except JiraSearchError as e:
            tickets_data, search_error = [], str(e)

        if search_error:
            st.session_state["jira_tickets"] = []
            st.session_state["jira_titles"] = {}
            st.error(f"❌ {search_error}")
        elif tickets_data:
            found_keys = [t["key"] for t in tickets_data]
            jira_titles = {t["key"]: t["summary"] for t in tickets_data if t.get("summary")}
            st.session_state["jira_tickets"] = found_keys
            st.session_state["jira_titles"] = jira_titles
            st.session_state["jira_context"] = {t["key"]: t for t in tickets_data}
            st.session_state["selected_index"] = 0
            st.session_state["selected_jira_project"] = project_key
            st.session_state["workflow_step"] = "INIT"
            
            formatted_list = [f"{t['key']} - {t['summary']}" if t.get('summary') else t['key'] for t in tickets_data]
            st.success(f"{len(found_keys)} ticket(s) trouvé(s) :\n" + "\n".join([f"- {item}" for item in formatted_list]))
        else:
            st.session_state["jira_tickets"] = []
            st.session_state["jira_titles"] = {}
            st.warning(f"Aucun ticket trouvé avec ces critères sur le projet {project_key}.")

if "jira_tickets" in st.session_state and st.session_state["jira_tickets"]:
    tickets = st.session_state["jira_tickets"]
    current_project = st.session_state.get("selected_jira_project", os.getenv("JIRA_PROJECT_KEY", ""))
    
    st.markdown("---")
    st.markdown("### 2. Sélection du Ticket & Mode d'Exécution")
    
    std_only = (current_project or "").upper() in std_only_projects()
    if std_only:
        st.info(f"ℹ️ Projet {current_project} (STD_ONLY_PROJECTS) : analyse basée uniquement sur le socle "
                f"Standard (branche dev).")
        client_repo_path = "STD_ONLY"
    else:
        client_repo_path = st.text_input(
            "Chemin local ou URL Bitbucket du dépôt Client :",
            value=os.getenv("BITBUCKET_CLIENT_REPO_PATH", ""),
            help="Ex: https://git.kiscloud.io/projects/PHEACR ou C:\\development\\PHEACR"
        )

    db_schema_input = st.text_input(
        "Schéma BDD cible :",
        value=os.getenv("DB_SCHEMA", ""),
        key="db_schema_input",
        help="Schéma de la base client consultée en lecture seule par l'agent (structure des tables, paramétrage, "
             "données du cas). Laissez vide pour que l'agent n'utilise pas la base."
    ).strip()

    if "selected_index" not in st.session_state:
        st.session_state["selected_index"] = 0

    selected_ticket = st.selectbox(
        "Sélectionne le ticket Jira à traiter :",
        options=tickets,
        index=st.session_state["selected_index"],
        key="ticket_selectbox",
        format_func=format_ticket_option
    )
    
    st.session_state["selected_index"] = tickets.index(selected_ticket)

    # Indicateur de capitalisation : mêmes critères que ceux utilisés par main.py
    ticket_ctx = st.session_state.get("jira_context", {}).get(selected_ticket, {})
    similar = find_similar_tickets(
        current_project,
        component_name=ticket_ctx.get("components", []),
        query_text=f"{ticket_ctx.get('summary', '')}\n{ticket_ctx.get('description', '')}",
        exclude_issue_key=selected_ticket,
    )
    lessons = get_project_lessons(current_project, exclude_issue_key=selected_ticket)
    own_feedbacks = load_ticket_memory(selected_ticket).get("feedbacks", [])

    with st.container(border=True):
        st.markdown("**:material/psychology: Mémoire de l'agent**")
        with st.container(horizontal=True):
            st.metric("Tickets similaires", len(similar))
            st.metric("Consignes projet", len(lessons))
            st.metric("Retours sur ce ticket", len(own_feedbacks))
        if similar:
            with st.expander("Analyses et patchs similaires injectés dans le prompt"):
                for item in similar:
                    mem = item["memory"]
                    st.markdown(f"**{mem['issue_key']}** — {mem.get('summary', '')}  \n"
                                f":gray[{mem.get('status')} · score {item['score']} · {', '.join(item['reasons'])}]")
                    files = mem.get("patch_info", {}).get("modified_files", [])
                    if files:
                        st.caption("Fichiers du patch : " + ", ".join(files[:8]))
        if lessons:
            with st.expander("Consignes récurrentes appliquées"):
                st.markdown("\n".join(f"- {l}" for l in lessons))
        if not similar and not lessons:
            st.caption("Aucune expérience mémorisée pertinente pour ce ticket.")

    # Vérification si une analyse existe déjà localement pour ce ticket
    cadrage_file = f"{selected_ticket}-cadrage.md"
    # Épopée : l'analyse la découpe en tâches enfants, chacune avec sa propre analyse puis son patch
    is_epic = bool(ticket_ctx.get("is_epic"))
    epic_manifest_tasks = load_epic_manifest(selected_ticket).get("tasks", []) if is_epic else []
    epic_tasks = [t for t in epic_manifest_tasks if t.get("key")]
    # Découpage rédigé : ses tâches ne sont créées dans Jira qu'à la validation du découpage par l'utilisateur
    epic_pending = len(epic_manifest_tasks) - len(epic_tasks)
    # Analyse déjà faite : fichier du workspace, sinon dernière version mémorisée par l'agent (restaurée)
    has_existing_cadrage = (bool(epic_manifest_tasks) if is_epic
                            else restore_cadrage_file(selected_ticket, cadrage_file))
    # Analyse existante d'une tâche : la saisie de révision s'ouvre directement, sans relancer l'analyse
    if should_open_revision(is_epic, has_existing_cadrage, st.session_state.get("workflow_step"),
                            st.session_state.get("current_ticket", ""), selected_ticket):
        st.session_state["workflow_step"] = "REVIEW_ANALYSIS"
        st.session_state["current_ticket"] = selected_ticket
        st.session_state["current_client_repo"] = client_repo_path.strip()

    if is_epic:
        if epic_pending:
            st.warning(f"Le découpage de {selected_ticket} est rédigé ({len(epic_manifest_tasks)} tâche(s)) : "
                       f"brouillon non validé, {epic_pending} tâche(s) pas encore créées dans Jira. Relisez-le, "
                       f"puis validez le découpage pour créer les tâches.")
        elif epic_tasks:
            st.success(f"💡 {format_ticket_option(selected_ticket)} est déjà découpée en {len(epic_tasks)} tâche(s) : "
                       + ", ".join(t["key"] for t in epic_tasks) + ".")
        else:
            st.info("ℹ️ Épopée : l'analyse propose la ou les tâches enfants nécessaires (une seule si un découpage "
                    "n'est pas utile), avec leur analyse fonctionnelle et technique. Les tâches ne sont créées dans "
                    "Jira qu'après validation du découpage.")
    elif has_existing_cadrage:
        st.success(f"💡 Une analyse existe déjà pour {format_ticket_option(selected_ticket)} : "
                   "elle est affichée ci-dessous pour révision.")
        show_publication_status(selected_ticket)

    col_cadrage, col_direct_patch, col_next = st.columns([2, 2, 1])

    with col_cadrage:
        if is_epic:
            btn_cadrage_label = "📝 Relire le découpage" if has_existing_cadrage else "⚡ 1. Analyser l'épopée"
        else:
            btn_cadrage_label = "♻️ Refaire l'analyse complète" if has_existing_cadrage else "⚡ 1. Lancer l'analyse"
        run_cadrage_btn = st.button(btn_cadrage_label, use_container_width=True,
                                    help="Remplace l'analyse existante par une nouvelle analyse complète. Pour "
                                         "l'ajuster, utilisez plutôt la révision ci-dessous." if (
                                             has_existing_cadrage and not is_epic) else None)

    with col_direct_patch:
        if is_epic:
            btn_patch_label = "🛠️ Générer les Patchs des tâches"
        else:
            btn_patch_label = "🛠️ Générer le Patch (Analyse prête)" if has_existing_cadrage else "🚀 Générer le Patch directement"
        run_direct_patch_btn = st.button(btn_patch_label, type="primary", use_container_width=True,
                                         disabled=is_epic and (not epic_tasks or bool(epic_pending)),
                                         help="Découpage à valider d'abord (création des tâches)." if (
                                             is_epic and (not epic_tasks or epic_pending)) else None)

    with col_next:
        has_next = st.session_state["selected_index"] < len(tickets) - 1
        if st.button("➡️ Suivant", disabled=not has_next, use_container_width=True):
            st.session_state["selected_index"] += 1
            st.session_state["workflow_step"] = "INIT"
            st.rerun()

    # Épopée : analyse avec création des tâches enfants, relecture, ou patchs de toutes les tâches
    if is_epic and (run_cadrage_btn or run_direct_patch_btn):
        if not std_only and not client_repo_path.strip():
            st.error("Veuillez renseigner le chemin local ou l'URL Bitbucket du dépôt client.")
        elif run_direct_patch_btn:
            if run_epic_patches(selected_ticket, current_project, client_repo_path.strip(), db_schema_input):
                st.success(f"🎉 Patchs générés pour toutes les tâches de l'épopée {selected_ticket} !")
                st.session_state["workflow_step"] = "EPIC_FINISHED"
                st.session_state["current_ticket"] = selected_ticket
                st.session_state["current_client_repo"] = client_repo_path.strip()
        else:
            returncode = 0
            if not has_existing_cadrage:
                st.info(f"Analyse de l'épopée {selected_ticket} en cours (découpage en brouillon : rien n'est "
                        f"créé dans Jira avant votre validation)...")
                returncode = run_main_step([
                    sys.executable, MAIN_PY,
                    "--issue", selected_ticket,
                    "--project", current_project,
                    "--client-repo", client_repo_path.strip(),
                    "--step", "epic-cadrage",
                    "--db-schema", db_schema_input
                ])
            if returncode == 0:
                st.session_state["workflow_step"] = "REVIEW_EPIC"
                st.session_state["current_ticket"] = selected_ticket
                st.session_state["current_client_repo"] = client_repo_path.strip()
                st.rerun()
            else:
                st.error("Une erreur est survenue lors de l'analyse de l'épopée : consultez le journal technique "
                         "(en haut de l'onglet).")
        run_cadrage_btn = run_direct_patch_btn = False

    # Option A : Lancer ou revoir l'analyse
    if run_cadrage_btn:
        if not std_only and not client_repo_path.strip():
            st.error("Veuillez renseigner le chemin local ou l'URL Bitbucket du dépôt client.")
        else:
            st.info(f"Analyse/Cadrage en cours pour {selected_ticket}...")
            cmd_cadrage = [
                sys.executable, MAIN_PY,
                "--issue", selected_ticket,
                "--project", current_project,
                "--client-repo", client_repo_path.strip(),
                "--step", "cadrage",
                "--db-schema", db_schema_input
            ]

            returncode = run_main_step(cmd_cadrage)

            if returncode == 0:
                st.success("Analyse terminée et sauvegardée.")
                st.session_state["workflow_step"] = "REVIEW_ANALYSIS"
                st.session_state["current_ticket"] = selected_ticket
                st.session_state["current_client_repo"] = client_repo_path.strip()
                st.rerun()
            else:
                st.error("Une erreur est survenue lors du cadrage.")

    # Option B : Génération directe du Patch (sans passer par la relecture)
    if run_direct_patch_btn:
        if not std_only and not client_repo_path.strip():
            st.error("Veuillez renseigner le chemin local ou l'URL Bitbucket du dépôt client.")
        else:
            st.info(f"Génération directe du patch et envoi sur Jira pour {selected_ticket}...")
            cmd_patch = [
                sys.executable, MAIN_PY,
                "--issue", selected_ticket,
                "--project", current_project,
                "--client-repo", client_repo_path.strip(),
                "--step", "patch",
                "--db-schema", db_schema_input
            ]
            patch_started_at = time.time()

            returncode = run_main_step(cmd_patch)

            if returncode == 0:
                st.success(f"🎉 Patch généré et attaché avec succès au ticket Jira {selected_ticket} !")
                show_release_note(selected_ticket, patch_started_at)
                st.session_state["workflow_step"] = "FINISHED"
            else:
                st.error("Une erreur est survenue lors de la génération du patch.")

# ---------------------------------------------------------
# PHASE 2 : RELECTURE, REVISION ET VALIDATION DU PATCH
# ---------------------------------------------------------
if st.session_state.get("workflow_step") in ["REVIEW_ANALYSIS", "FINISHED"]:
    active_ticket = st.session_state.get("current_ticket", "")
    active_repo = st.session_state.get("current_client_repo", "")
    active_project = st.session_state.get("selected_jira_project", os.getenv("JIRA_PROJECT_KEY", ""))

    st.markdown("---")
    st.subheader(f"📝 Relecture & Ajustement de l'Analyse ({active_ticket})")

    cadrage_file = f"{active_ticket}-cadrage.md"
    cadrage_content = ""
    if os.path.exists(cadrage_file):
        with open(cadrage_file, "r", encoding="utf-8") as f:
            cadrage_content = f.read()

    flash = st.session_state.pop("publication_flash", None)
    if flash and flash[0] == active_ticket:
        st.success(flash[1])
    show_publication_status(active_ticket, cadrage_content)
    show_estimation(cadrage_content, active_project)

    user_analysis = st.text_area(
        "Analyse enregistrée (Modifiable directement avant de générer le patch) :",
        value=cadrage_content,
        height=300
    )

    user_feedback = st.text_input(
        "Consignes de révision pour l'agent (Optionnel) :",
        placeholder="Ex: Préciser l'impact sur les retours, ajuster le composant..."
    )

    col_review, col_publish, col_validate = st.columns(3)

    with col_review:
        if st.button("🔄 Demander une révision à l'agent", use_container_width=True):
            st.info("Révision du cadrage par l'agent...")

            if record_manual_edit(active_ticket, cadrage_content, user_analysis):
                st.toast("Vos corrections manuelles ont été mémorisées.", icon=":material/edit_note:")

            analysis_publication.save_draft(active_ticket, user_analysis)

            cmd_refine = [
                sys.executable, MAIN_PY,
                "--issue", active_ticket,
                "--project", active_project,
                "--client-repo", active_repo,
                "--step", "refine",
                "--feedback", user_feedback.strip(),
                "--db-schema", st.session_state.get("db_schema_input", "").strip()
            ]

            returncode = run_main_step(cmd_refine)

            if returncode == 0:
                st.toast("Analyse révisée (brouillon, non publiée dans Jira).", icon=":material/check_circle:")
                st.rerun()
            else:
                # Pas de rerun : les logs restent affichés pour diagnostiquer l'échec
                st.error(f"La révision a échoué (code {returncode}). Rien n'a été enregistré en mémoire : consultez le journal technique (en haut de l'onglet).")

    with col_publish:
        if st.button("📤 Valider l'analyse et la publier dans Jira", use_container_width=True,
                     help="Publie l'analyse validée dans la description Jira, sans générer le patch."):
            if record_manual_edit(active_ticket, cadrage_content, user_analysis):
                st.toast("Vos corrections manuelles ont été mémorisées.", icon=":material/edit_note:")
            try:
                state = analysis_publication.validate(active_ticket, user_analysis)
                st.session_state["publication_flash"] = (active_ticket, f"Analyse validée. {state['libelle']}.")
                st.rerun()
            except Exception as e:
                st.error(f"Analyse enregistrée localement, mais non publiée dans Jira : {e}")

    with col_validate:
        if st.button("✅ Valider l'analyse & Générer le Patch", type="primary", use_container_width=True):
            if record_manual_edit(active_ticket, cadrage_content, user_analysis):
                st.toast("Vos corrections manuelles ont été mémorisées.", icon=":material/edit_note:")

            # Validation : publication de l'analyse dans Jira, puis patch
            try:
                published = analysis_publication.validate(active_ticket, user_analysis)
            except Exception as e:
                st.error(f"Analyse enregistrée localement, mais non publiée dans Jira : {e}. Patch non généré.")
                st.stop()
            st.info(f"{published['libelle']}. Génération du patch Git et envoi sur Jira...")

            cmd_val_patch = [
                sys.executable, MAIN_PY,
                "--issue", active_ticket,
                "--project", active_project,
                "--client-repo", active_repo,
                "--step", "patch",
                "--db-schema", st.session_state.get("db_schema_input", "").strip()
            ]
            patch_started_at = time.time()

            returncode = run_main_step(cmd_val_patch)

            if returncode == 0:
                st.success(f"🎉 Traitement terminé avec succès ! Le patch est attaché au ticket {active_ticket}.")
                show_release_note(active_ticket, patch_started_at)
                st.session_state["workflow_step"] = "FINISHED"
            else:
                st.error("Une erreur est survenue lors de la création du patch.")
                
# ---------------------------------------------------------
# PHASE 2 (ÉPOPÉE) : RELECTURE DES TÂCHES ENFANTS ET PATCHS
# ---------------------------------------------------------
if st.session_state.get("workflow_step") in ["REVIEW_EPIC", "EPIC_FINISHED"]:
    active_epic = st.session_state.get("current_ticket", "")
    active_repo = st.session_state.get("current_client_repo", "")
    active_project = st.session_state.get("selected_jira_project", os.getenv("JIRA_PROJECT_KEY", ""))
    active_schema = st.session_state.get("db_schema_input", "").strip()
    epic_manifest = load_epic_manifest(active_epic)
    review_tasks = epic_manifest.get("tasks", [])
    pending_tasks = draft_tasks(epic_manifest)

    st.markdown("---")
    st.subheader(f"📝 Relecture du découpage de l'épopée ({active_epic})")
    if epic_manifest.get("justification"):
        st.caption(f"Découpage en {len(review_tasks)} tâche(s) — {epic_manifest['justification']}")
    if pending_tasks:
        st.markdown(f":orange[:material/edit_note: Découpage non validé (brouillon) : {len(pending_tasks)} "
                    f"tâche(s) pas encore créées dans Jira.]")

    task_edits = {}
    missing_tasks = missing_analyses(epic_manifest)
    if missing_tasks:
        st.error(f"{len(missing_tasks)} tâche(s) sans analyse : générer ou réviser le découpage avant de le "
                 f"valider.", icon=":material/error:")
    for index, task in enumerate(review_tasks, 1):
        path, task_content = task_analysis(task)
        label = task.get("key") or f"Tâche {index} (à créer)"
        with st.expander(f"{label} — {task.get('summary', '')}",
                         expanded=len(review_tasks) == 1 or not task_content.strip()):
            if not task_content.strip():
                st.error(f"Analyse absente ({path or 'aucun fichier'}) : générer ou réviser.",
                         icon=":material/error:")
            if task.get("key"):
                show_publication_status(task["key"], task_content)
            else:
                st.markdown(":orange[:material/edit_note: Brouillon : tâche créée dans Jira à la validation "
                            "du découpage.]")
            show_estimation(task_content, active_project)
            task_edits[path] = (task.get("key", ""), task_content, st.text_area(
                f"Analyse de {label} (Modifiable directement avant validation) :",
                value=task_content,
                height=300
            ))

    show_epic_estimation(review_tasks, active_project)

    def save_task_edits():
        """Corrections manuelles enregistrées localement (fichier, mémoire) ; publiées à la validation."""
        for path, (task_key, before, after) in task_edits.items():
            # Rien d'écrit sans modification, et jamais une analyse vide sur un fichier
            if not path or after == before or not after.strip():
                continue
            with open(path, "w", encoding="utf-8") as f:
                f.write(after)
            if task_key and record_manual_edit(task_key, before, after):
                st.toast(f"Vos corrections sur {task_key} ont été mémorisées (publiées à la validation).",
                         icon=":material/edit_note:")

    epic_feedback = st.text_input(
        "Consignes de révision pour l'agent (Optionnel) :",
        placeholder="Ex: Regrouper les tâches 1 et 2, séparer la partie Studio, préciser l'impact sur les retours...",
        key="epic_feedback"
    )

    col_epic_review, col_epic_split, col_epic_validate = st.columns(3)

    with col_epic_review:
        if st.button("🔄 Demander une révision à l'agent", use_container_width=True, key="epic_refine_btn"):
            st.info("Révision du découpage et des analyses par l'agent...")
            save_task_edits()
            returncode = run_main_step([
                sys.executable, MAIN_PY,
                "--issue", active_epic,
                "--project", active_project,
                "--client-repo", active_repo,
                "--step", "epic-cadrage",
                "--feedback", epic_feedback.strip(),
                "--db-schema", active_schema
            ])
            if returncode == 0:
                st.toast("Découpage révisé (brouillon, rien n'est publié dans Jira).", icon=":material/check_circle:")
                st.rerun()
            else:
                st.error(f"La révision a échoué (code {returncode}) : consultez le journal technique (en haut de l'onglet).")

    with col_epic_split:
        if st.button("📌 Valider le découpage et créer les tâches", use_container_width=True,
                     key="epic_split_btn", disabled=not review_tasks or bool(missing_tasks),
                     help="Crée dans Jira les tâches du découpage et publie les analyses modifiées, "
                          "sans générer les patchs."):
            save_task_edits()
            if validate_epic_split(active_epic, active_project, active_repo, active_schema):
                st.toast("Découpage validé : tâches créées et analyses publiées dans Jira.",
                         icon=":material/check_circle:")
                st.rerun()

    with col_epic_validate:
        if st.button("✅ Valider le découpage & Générer les Patchs", type="primary", use_container_width=True,
                     key="epic_validate_btn", disabled=not review_tasks or bool(missing_tasks)):
            save_task_edits()
            if validate_epic_split(active_epic, active_project, active_repo, active_schema) and \
                    run_epic_patches(active_epic, active_project, active_repo, active_schema):
                st.success(f"🎉 Traitement terminé : un patch est attaché à chaque tâche de l'épopée {active_epic}.")
                st.session_state["workflow_step"] = "EPIC_FINISHED"

# Dans app.py, sous le bouton de validation du patch :
if st.session_state.get("workflow_step") == "FINISHED":
    st.markdown("---")
    st.subheader("🧪 Validation Cucumber / Jenkins")
    
    job_name = st.text_input("Nom du Job Jenkins Cucumber :", value=os.getenv("JENKINS_CUCUMBER_JOB", ""))
    
    if st.button("🚀 Lancer les tests Cucumber sur Jenkins", use_container_width=True):
        # Un seul build Cucumber à la fois (runs et vérifications de l'onglet Cucumber compris)
        busy = jenkins_tools.busy_builds(jenkins_tools.exclusive_jobs(job_name))
        if busy:
            st.warning("Un build Cucumber est déjà en cours (un seul à la fois) : " + " ; ".join(busy)
                       + ". Relancez une fois terminé.")
            st.stop()
        with st.spinner("Déclenchement du build Jenkins..."):
            build_url = trigger_cucumber_job(
                job_name=job_name,
                parameters={
                    "ISSUE_KEY": active_ticket,
                    "PROJECT": active_project,
                    "PATCH_APPLIED": "true"
                }
            )
            if build_url:
                st.success(f"Job lancé ! Suivi du build : [{build_url}]({build_url})")
            else:
                st.error("Échec du lancement du job Jenkins.")

st.divider()
render_estimation_settings()
