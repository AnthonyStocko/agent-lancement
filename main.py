"""
CLI d'agent-lancement : --step cadrage | refine | epic-cadrage | epic-sync | patch. Copie élaguée par script
(scripts/elaguer_main.py de l'ancien agent-scrum-master) de main() : branches des autres étapes retirées.
Lancée par l'IHM de cet agent (8506) ; agent-scrum-master, qui y déléguait ces étapes, est supprimé.
"""
import io
import logging
import argparse
import json
import os
import re
import sys
import time
from agent_socle import bitbucket_std, cli, jira_guard, knowledge_tools, livraisons
from agent_socle.jira_cli import attach_files
from agent_socle.cli import CLAUDE_EFFORT_ENV, CLAUDE_MODEL_ENV, ODATIO_DOCS_STEPS, SOCLE_DIR, _env_int, _read_text, _safe_memory, build_db_block, check_jira_untouched, claude_effort, claude_env, consult_knowledge, flush_knowledge_outbox, format_jira_context, odatio_docs_block, print_draft_status, produced, render_prompt, report_token_usage, run_claude, share_file, share_knowledge, ticket_client
from agent_socle.depots import STD_LOCAL_ROOT, STD_READER, detect_odatio_version, ensure_cucumber_std_repo, ensure_local_repo
from agent_socle.jira_tools import get_epic_children, get_full_jira_issue_context, std_only_projects, update_jira_release_note
from agent_socle.memory_tools import _extract_recap, get_capitalized_experience, save_cadrage_memory, save_patch_memory, save_refine_feedback
from tools import analysis_publication, estimation
from tools.epic_tools import _restore_keys as restore_epic_keys, attach_existing_analyses, draft_tasks, epic_manifest_path, load_epic_manifest, missing_analyses, save_epic_manifest, sync_epic_tasks
from dotenv import load_dotenv

from agent_socle import cli, env_tools
from agent_socle.chemins import DATA_DIR

# Modèles de prompts de l'agent (prompts/*.md), puis ceux du socle
DEFAULT_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
cli.DEFAULT_PROMPTS_DIR = DEFAULT_PROMPTS_DIR

# Force l'encodage UTF-8 sous Windows
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# Même .env que les autres agents (AGENT_DATA_DIR), jamais copié
ENV_PATH = os.path.join(DATA_DIR, ".env")
logging.getLogger("dotenv.main").setLevel(logging.ERROR)
load_dotenv(ENV_PATH)
if env_tools.check_env(ENV_PATH):
    print(f"[CONFIGURATION] ⚠️ {env_tools.check_line(env_tools.check_env(ENV_PATH))}")


def main():
    parser = argparse.ArgumentParser(description="Agent Lancement - cadrage, révision, épopées et patch")
    parser.add_argument("--issue", default="", help="Clé du ticket Jira (ex: PROJ-123) ; inutile pour generate-cucumber-client")
    parser.add_argument("--project", default="", help="Clé du projet Jira (ex: PROJ) ; inutile pour generate-cucumber-client")
    parser.add_argument("--client-repo", default="", help="Chemin local ou URL du dépôt client")
    parser.add_argument("--std-repo", default="", help="Chemin local ou URL du dépôt socle standard")
    parser.add_argument("--step", choices=["cadrage", "epic-cadrage", "epic-sync", "refine", "patch"], default="cadrage", help="Étape du traitement (epic-cadrage : découpe une épopée en tâches enfants analysées ; besoin : reformulation du besoin et questions ouvertes ; cadrage-reponses : complète le cadrage avec les réponses de l'onglet Intégration ; generate-cucumber-client : patch de tests Cucumber couvrant tous les spécifiques d'un client)")
    parser.add_argument("--pr-url", action="append", default=[],
                        help="review-pr : adresse d'une pull request de la fonctionnalité (option répétable : toutes les "
                             "PR de la fonctionnalité sont relues ensemble)")
    parser.add_argument("--feature", default="", help="review-pr : ticket Jira de la fonctionnalité relue")
    parser.add_argument("--source-schema", default="", help="restore-config : schéma de la config client (base client)")
    parser.add_argument("--target-schema", default="", help="restore-config : schéma des tests Cucumber à remplacer")
    parser.add_argument("--backup", default="", help="restore-config : sauvegarde à remettre en place (retour arrière)")
    parser.add_argument("--copied-reference", action="store_true", help="restore-config : configuration copiée — copie dans le schéma de test des runs du profil puis instantané de référence")
    parser.add_argument("--question", default="", help="ask : question posée à l'assistant (onglet Assistant)")
    parser.add_argument("--answer-file", default="", help="ask : fichier où écrire la réponse")
    parser.add_argument("--no-init", action="store_true", help="generate-cucumber-client : aucune init lancée avant de juger les lots (données du client déjà présentes dans l'environnement des runs Jenkins)")
    parser.add_argument("--specifics", default="", help="generate-cucumber-client : ids des spécifiques à traiter, séparés par des virgules (défaut : non couverts et partiels de l'inventaire)")
    parser.add_argument("--resume", action="store_true", help="generate-cucumber-client : reprend les lots non terminés de la dernière génération")
    parser.add_argument("--reprise", action="store_true", help="generate-cucumber-client : relance --specifics déjà générés en reprenant leur test existant (vérification puis correction, jamais régénéré) ; un test déjà vert n'est pas relancé ; un spécifique sans test est généré en complément")
    parser.add_argument("--complete", action="store_true", help="generate-cucumber-client : complète la dernière génération (worktree et lots conservés) par de nouveaux lots pour --specifics, à défaut pour les spécifiques non couverts faute de step, d'endpoint, d'interaction ou de vérification")
    parser.add_argument("--max-lots", type=int, default=0, help="generate-cucumber-client : nombre maximal de lots traités par cette exécution (0 = tous), les suivants restent à reprendre")
    parser.add_argument("--inventory-only", action="store_true", help="generate-cucumber-client : pré-calculs seuls (inventaire, catalogues, couverture), sans appel à Claude")
    parser.add_argument("--init-only", action="store_true", help="generate-cucumber-client : lot d'initialisation des données, en patch séparé, selon l'état détecté de l'init : absente, copie des Init_<MODULE> du Cucumber Standard pour ses modules ; incomplète, compléments seuls dans une nouvelle feature (jamais de réécriture ni de fichier protégé modifié) ; complète, rien. Sans appel à Claude, sauf steps du projet à écrire")
    parser.add_argument("--profile-only", action="store_true", help="generate-cucumber-client : détecte le profil client (racine et tag des features, init, clear, fichiers du module spécifique, runner, codes de référence, version de la bibliothèque) dans le dépôt <client>-cucumber, sans appel à Claude")
    parser.add_argument("--detect-only", action="store_true", help="generate-cucumber-client : détection automatique du projet (inventaire, profil client, faits de l'initialisation des données), mise en cache par empreinte des commits des dépôts et sautée si elle n'a pas changé ; lecture seule, sans appel à Claude")
    parser.add_argument("--align-library", action="store_true", help="generate-cucumber-client : bibliothèque Cucumber du pom.xml alignée sur la version du Standard du client (Jenkins, à défaut l'application) : seule la version change dans le pom, compilation corrigée dans les steps du projet, dry-run, puis (livraison autonome) runs de l'init et complet, promotion sans régression")
    parser.add_argument("--simple-depot", default="", choices=["", "verifier", "creer", "branche", "aligner"], help="generate-cucumber-client, onglet « Cucumber » simplifié, étape 2 : dépôt <client>-cucumber — constat, création (droits contrôlés), branche de la version, alignement validé de la bibliothèque et du runner, compilation")
    parser.add_argument("--anticipation", action="store_true", help="generate-cucumber-client : analyse statique d'anticipation (Studio à la version testée : variables d'entrée non déclarées, composants absents, XML), 0 token, sans run ; résultat dans anticipation.json")
    parser.add_argument("--simple-init", default="", choices=["", "auto", "verifier", "appliquer", "completer", "lancer", "corriger"], help="generate-cucumber-client, onglet « Cucumber » simplifié, étape 4 : init du client sur la branche de la version — clear déduit des routes de l'application à sa version, init du Standard adaptée ou init existante comparée, run Jenkins de l'init, correction connue proposée sur échec ; « auto » enchaîne tout (fichiers générés et poussés, init lancée sur Jenkins) jusqu'à une init verte")
    parser.add_argument("--java21", action="store_true", help="generate-cucumber-client : migration Java 21 du projet de tests (pom.xml seul, branche dédiée jamais fusionnée) et contrôle de non-régression : compilation et dry-run en Java 21, runs AVANT (application Java 11) et APRÈS (application Java 21), relance des écarts, rapport, PR proposée sans régression")
    parser.add_argument("--java21-avant", default="", help="--java21 : environnement AVANT confirmé, « composant_version=<v>;kis_version=<v> »")
    parser.add_argument("--java21-apres", default="", help="--java21 : environnement APRÈS confirmé, « composant_version=<v>;kis_version=<v> »")
    parser.add_argument("--module-tag", default="", help="generate-cucumber-client : module exclu du périmètre testé (ex. TMS) : son tag client est remplacé par @MODULE_EXCLU dans ses features (runs Jenkins lancés à la main), commit dédié et patch <client>-cucumber-module-<mod>-exclu.patch, sans appel à Claude")
    parser.add_argument("--module-tag-restore", action="store_true", help="avec --module-tag : opération inverse à la réactivation du module (@MODULE_EXCLU -> tag client), patch <client>-cucumber-module-<mod>-reactive.patch")
    parser.add_argument("--fix-client-tags", action="store_true", help="generate-cucumber-client : règle rétroactive « tag client » : ajoute le tag client au-dessus des scénarios actifs qui ne l'ont pas (scénarios désactivés exclus), commite dans le worktree, contrôle de nouveau et régénère le patch, sans appel à Claude")
    parser.add_argument("--detect-force", action="store_true", help="generate-cucumber-client --detect-only : détection refaite même si l'empreinte des commits n'a pas changé (profil corrigé)")
    parser.add_argument("--health-only", action="store_true", help="generate-cucumber-client : bilan de santé du dépôt <client>-cucumber avant génération (emplacement et tags des features, fichiers de données manquants, compilation et dry-run de toutes ses features), sans appel à Claude")
    parser.add_argument("--import-run", nargs="*", default=None, help="generate-cucumber-client : import d'un rapport d'exécution (console Jenkins, rapport Cucumber JSON, exports de logs serveur CSV), secrets masqués avant stockage, analysé sans appel à Claude")
    parser.add_argument("--import-url", default="", help="generate-cucumber-client : import d'un build Jenkins (console et rapports Cucumber JSON lus en mémoire), analysé sans appel à Claude")
    parser.add_argument("--patch-only", action="store_true", help="generate-cucumber-client : régénère seulement le patch <client>-cucumber.patch depuis l'espace de travail de l'agent (un commit, écart net avec la branche d'intégration actuelle), sans appel à Claude")
    parser.add_argument("--fix-init-from-import", type=int, default=0, help="generate-cucumber-client : l'init du run importé n° N échoue dans les compléments écrits par l'agent : il corrige ces fichiers seuls (contrôlés), puis rejoue l'init si CUCUMBER_AGENT_JENKINS=1, sinon livre <client>-cucumber-init-correction.patch")
    parser.add_argument("--modules-exclus", default=None, help="generate-cucumber-client : modules exclus du périmètre testé (codes séparés par des virgules, ex. TMS ; vide : aucun), transmis par l'IHM depuis le profil client ; enregistrés dans le profil s'ils en diffèrent, affichés en tête du journal")
    parser.add_argument("--from-import", type=int, default=0, help="generate-cucumber-client --complete : lots « correction » des échecs « test » des scénarios de l'agent de l'import n° N")
    parser.add_argument("--run-jenkins", action="store_true", help="generate-cucumber-client : pousse le worktree sur la branche lue par le job (profil), contrôle le commit, lance le job Jenkins du client, suit le build puis importe sa console et ses rapports (bilan)")
    parser.add_argument("--run-dry", action="store_true", help="generate-cucumber-client --run-jenkins : aperçu sans push ni lancement (0 token) — job, branche, rôles des paramètres, paramètres envoyés (secrets masqués) et nombre de tests attendus pour --run-tags")
    parser.add_argument("--run-no-push", action="store_true", help="generate-cucumber-client --run-jenkins : rien n'est commité ni poussé, le job exécute la branche telle qu'elle est sur le dépôt client (--run-branch, à défaut celle du profil) ; lecture seule")
    parser.add_argument("--run-branch", default="", help="generate-cucumber-client --run-jenkins --run-no-push : branche du dépôt client à exécuter (défaut : branche de travail du profil)")
    parser.add_argument("--run-tags", default="", help="generate-cucumber-client --run-jenkins : expression de tags ajoutée aux arguments du job (-Dcucumber.filter.tags, vide : tous les scénarios actifs)")
    parser.add_argument("--server-log", default="", help="generate-cucumber-client : export de logs serveur à contrôler (avec --log-scenario) : couvre-t-il l'heure du step en échec ? (l'outil d'export peut garder les seules lignes les plus récentes), sans appel à Claude")
    parser.add_argument("--log-scenario", default="", help="generate-cucumber-client --server-log : identifiant ou tag @agent_… du scénario en échec (dernière génération)")
    parser.add_argument("--init-modules", default="", help="generate-cucumber-client --init-only : modules retenus parmi WMS, TMS, OMS, séparés par des virgules (défaut : détectés dans les dépôts et l'inventaire du client)")
    parser.add_argument("--client-name", default="", help="generate-cucumber-client : nom du client, qui donne le dépôt cible <client>-cucumber (défaut : déduit de --client-repo)")
    parser.add_argument("--cucumber-std-repo", default="", help="Dépôt du Cucumber Standard : URL Bitbucket (navigation ou clonable) ou chemin d'un clone local (défaut : BITBUCKET_CUCUMBER_STD_REPO)")
    parser.add_argument("--feedback", default="", help="Remarques de l'utilisateur pour la révision")
    # Les tests Cucumber sont désormais traités après merge, par le ticket SPEC [CUCUMBER] (generate-cucumber-postmerge)
    parser.add_argument("--generate-cucumber", action="store_true", default=False, help="Réactiver ponctuellement la génération des tests Cucumber avec le patch")
    parser.add_argument("--skip-cucumber", dest="generate_cucumber", action="store_false", help="Désactiver la génération des tests Cucumber (comportement par défaut)")
    parser.add_argument("--db-schema", default="", help="Schéma de la base client consultable en lecture seule par l'agent (vide = pas d'accès base)")
    parser.add_argument("--application", default="", help="Étapes besoin et generate-cucumber-client : Projet / Application dont les tickets servent de référence, séparés par des virgules (étape besoin, défaut : valeur du champ JIRA_APPLICATION_FIELD du ticket)")
    parser.add_argument("--epic-task-type", default=os.getenv("JIRA_EPIC_TASK_ISSUE_TYPE", ""), help="Type des tâches créées sous une épopée (défaut : premier type existant du projet parmi Tâche, Task, Development, Story)")

    postmerge = parser.add_argument_group("Post-merge (--step generate-cucumber-postmerge, déclenché par webhook_server.py)")
    postmerge.add_argument("--repo-slug", default="", help="Dépôt Bitbucket fusionné (ex: odatio-client)")
    postmerge.add_argument("--merge-commit", default="", help="Commit de merge (ou toHash du push)")
    postmerge.add_argument("--base-commit", default="", help="Commit de référence du diff (défaut : 1er parent du merge)")
    postmerge.add_argument("--source-branch", default="")
    postmerge.add_argument("--target-branch", default="")
    postmerge.add_argument("--cucumber-repo", default="", help="Dépôt Cucumber client (défaut : POSTMERGE_CUCUMBER_REPOS puis détection automatique)")
    postmerge.add_argument("--cucumber-base-branch", default=os.getenv("POSTMERGE_CUCUMBER_BASE_BRANCH", "integration"))
    postmerge.add_argument("--jira-target-project",
                           default=os.getenv("POSTMERGE_JIRA_PROJECT", "").strip() or os.getenv("JIRA_PROJECT_KEY", "").strip())
    # Composant ou équipe Atlassian des tickets créés (post-merge et tâches d'épopée)
    postmerge.add_argument("--jira-component", default=os.getenv("POSTMERGE_JIRA_COMPONENT", "").strip())
    postmerge.add_argument("--jira-label", default=os.getenv("POSTMERGE_JIRA_LABEL", "").strip())

    args = parser.parse_args()
    is_postmerge = args.step == "generate-cucumber-postmerge"
    # Couverture Cucumber d'un client : pas de ticket Jira, un identifiant de travail CUCUMBER-<CLIENT>
    is_cucumber_client = args.step == "generate-cucumber-client"
    if not (args.issue.strip() and args.project.strip()):
        parser.error("--issue et --project sont obligatoires pour cette étape")

    target_issue = args.issue.strip().upper()
    target_project = args.project.strip().upper()
    flush_knowledge_outbox()

    # Création des tâches d'un découpage déjà rédigé (ex. création Jira échouée) : sans relancer l'agent
    if args.step == "epic-sync":
        print(f"=== Création des tâches de l'épopée {target_issue} à partir du découpage existant ===")
        sys.exit(0 if sync_epic_tasks(target_issue, target_project, load_epic_manifest(target_issue),
                                      component=args.jira_component, issue_type=args.epic_task_type) else 1)

    workspace_dir = DATA_DIR  # fichiers produits et .tmp : AGENT_DATA_DIR
    python_exe = sys.executable

    # Rédaction du besoin et complément du cadrage par les réponses : Jira et fichiers locaux seulement, aucun dépôt
    is_besoin = args.step == "besoin"
    is_cadrage_answers = args.step == "cadrage-reponses"
    jira_only = is_besoin or is_cadrage_answers
    local_std_repo = local_client_repo = local_cucumber_std_repo = ""
    odatio_version, branch_rule, repo_scope = "non requise", "", ""
    remote_code = False
    conventions_path = ""
    std_repo_arg = args.std_repo or os.getenv("BITBUCKET_STD_REPO_PATH", "").strip()
    if not std_repo_arg:
        print("❌ Dépôt du socle Standard non configuré : renseignez BITBUCKET_STD_REPO_PATH (onglet Configuration) "
              "ou passez --std-repo.")
        sys.exit(2)
    conventions_path = (os.getenv("DEV_CONVENTIONS_PATH", "").strip()
                        or os.path.join(workspace_dir, "odatio-specific-code-conventions-dev.md"))

    # 1. Dépôt Socle Standard. Client Cucumber et Standard projet Bitbucket : lu par l'API (tools/bitbucket_std),
    # sans fetch ici ; les clones (génération) ne sont récupérés qu'après, pour les seuls dépôts utiles au client
    std_key = bitbucket_std.project_key(std_repo_arg)
    remote_std = bool(is_cucumber_client and args.client_name and std_key and os.getenv("BITBUCKET_URL", "").strip())
    # Ticket du socle seul (STD_ONLY_PROJECTS, ex. OXY) : code du Standard lu à jour dans Bitbucket par l'agent
    # (tools/bitbucket_code.py), aucune copie locale ; seul le dépôt modifié au patch est copié (checkout)
    std_only_ticket = target_project.upper() in std_only_projects() or args.client_repo == "STD_ONLY"
    remote_code = bool(std_only_ticket and not is_cucumber_client and std_key
                       and os.getenv("BITBUCKET_URL", "").strip())
    # Cadrage d'un ticket client : Standard et dépôt client lus dans Bitbucket (API REST), aucun clone ni fetch ;
    # Bitbucket non configuré ou illisible : copies locales comme avant, mode annoncé dans le journal
    client_repo_arg = args.client_repo or os.getenv("BITBUCKET_CLIENT_REPO_PATH", "")
    cadrage_remote = {}
    if args.step == "cadrage" and not is_cucumber_client and not std_only_ticket:
        cadrage_remote = cadrage_remote_plan(std_key, client_repo_arg)
        if not cadrage_remote["remote"]:
            print(f"[CODE CADRAGE] Mode local : {cadrage_remote['motif']} ; Standard et client lus dans des "
                  f"copies locales (ensure_local_repo).")
    if remote_code:
        STD_READER[0] = bitbucket_std.RemoteStandard(std_key)
        local_std_repo = os.path.join(workspace_dir, ".tmp", f"std_{std_key}_travail")
        os.makedirs(local_std_repo, exist_ok=True)  # dossier d'écriture de l'agent (add-dir) avant tout checkout
        print(f"[STANDARD] Code du Standard lu à jour dans Bitbucket (projet {std_key}, API REST) : aucune copie "
              f"locale ; seul le dépôt modifié au patch est copié dans {local_std_repo}.")
    elif cadrage_remote.get("remote"):
        STD_READER[0] = cadrage_remote["std"]
        local_std_repo = ""
    else:
        local_std_repo = ensure_local_repo(std_repo_arg, "std")
        STD_READER[0] = local_std_repo
    STD_LOCAL_ROOT[0] = local_std_repo

    # 2. Détection de la version Odatio (repli global ; un client Cucumber a la sienne, voir plus bas). Version du
    # client choisie dans « Données du projet » : tout en est déduit, ODATIO_VERSION du .env n'est pas lu
    local_cucumber_std_repo = ""
    chosen_version = ({})
    odatio_version = detect_odatio_version(local_std_repo)

    # 3. Dépôt Client & Règles de branches
    if target_project.upper() in std_only_projects() or args.client_repo == "STD_ONLY":
        print(f"ℹ️ Projet {target_project} (STD_ONLY_PROJECTS) : socle Standard uniquement (branche dev).")
        local_client_repo = ""
        branch_rule = f"Projet {target_project} : utilise la branche de base 'dev' sur le socle Standard. Branche de travail : '{target_issue}_dev_...'."
        repo_scope = f"Explore le code source UNIQUEMENT dans le socle standard '{local_std_repo}'."
        if remote_code:
            code_cli = f'"{python_exe}" "{os.path.join(SOCLE_DIR, "bitbucket_code.py")}"'
            branch_rule = (f"Projet {target_project} : branche de base du Standard « dev » (à défaut « development », "
                           f"puis la branche par défaut du dépôt, résolue par l'outil ci-dessous). Branche de "
                           f"travail : '{target_issue}_dev_...'.")
            repo_scope = render_prompt("std_bitbucket_code", cli=code_cli, work_root=local_std_repo)
    else:
        branch_rule = f"Projet '{target_project}' : la branche de base sur le dépôt client est 'integration'. Branche de travail : '{target_issue}_dev_...'."
        if cadrage_remote.get("remote"):
            local_client_repo = ""
            repo_scope = cadrage_remote_scope(cadrage_remote, odatio_version, python_exe, workspace_dir)
        else:
            local_client_repo = ensure_local_repo(client_repo_arg, "client")
            repo_scope = f"Explore le code source dans le socle '{local_std_repo}' et dans le dépôt client '{local_client_repo}'."
            if cadrage_remote:
                print(f"[CODE CADRAGE] Standard : local, {local_std_repo}, branche de chaque copie locale.")
                print(f"[CODE CADRAGE] Client : local, {local_client_repo}, branche de chaque copie locale "
                      f"(base attendue : integration).")

    if args.generate_cucumber:
        cucumber_std_url = args.cucumber_std_repo.strip() or os.getenv("BITBUCKET_CUCUMBER_STD_REPO", "")
        local_cucumber_std_repo, _ = ensure_cucumber_std_repo(cucumber_std_url, odatio_version)
    else:
        print("\n[CUCUMBER STD] Génération des tests Cucumber désactivée par l'utilisateur.")

    cucumber_instruction = ""
    if args.generate_cucumber:
        if local_cucumber_std_repo:
            cucumber_instruction = "\n" + render_prompt("cucumber_enabled", odatio_version=odatio_version,
                                                        local_cucumber_std_repo=local_cucumber_std_repo,
                                                        local_client_repo=local_client_repo)
    else:
        cucumber_instruction = "\n" + render_prompt("cucumber_disabled")
    token_saving_rules = "\n" + render_prompt("token_saving_rules")

    # Besoin initial Jira : sert à retrouver les tickets similaires et à capitaliser
    jira_context = get_full_jira_issue_context(
        target_issue, exclude_sections=())
    if jira_context.get("error"):
        print(f"[MÉMOIRE] Contexte Jira indisponible : {jira_context['error']}")
        jira_context = {}

    # Récupération des retours d'expérience et leçons mémorisées (taille plafonnée : MEMORY_MAX_CHARS, 0 = désactivé)
    memory_max_chars = _env_int("MEMORY_MAX_CHARS", 6000)
    try:
        capitalized_exp = get_capitalized_experience(
            target_project,
            component_name=jira_context.get("components", []),
            query_text=f"{jira_context.get('summary', '')}\n{jira_context.get('description', '')[:3000]}",
            exclude_issue_key=target_issue,
            max_chars=memory_max_chars,
        ) if memory_max_chars and not (jira_only or is_cucumber_client) else ""
    except Exception as e:
        print(f"[MÉMOIRE] ⚠️ Lecture de la mémoire impossible ({e})")
        capitalized_exp = ""
    capitalization_block = f"\n{capitalized_exp}\n" if capitalized_exp else ""
    if capitalized_exp:
        print(f"[MÉMOIRE] Retours d'expérience injectés dans le prompt ({len(capitalized_exp)} caractères).")
    else:
        print("[MÉMOIRE] Aucun retour d'expérience pertinent trouvé.")

    # Connaissances partagées : analyses antérieures du projet et du sujet, injectées comme données à vérifier
    # (la génération Cucumber d'un client consulte les siennes dans run_cucumber_client)
    shared = consult_knowledge(
        target_project, client=ticket_client(args, target_project), tickets=[target_issue],
        composants=jira_context.get("components", []),
        query_text=f"{jira_context.get('summary', '')}\n{jira_context.get('description', '')[:3000]}")
    knowledge_block = f"\n{shared['bloc']}\n" if shared["bloc"] else ""

    cadrage_file = f"{target_issue}-cadrage.md"
    release_note_file = f"{target_issue}-release-note.md"
    # Patch et révision : toujours l'analyse COMPLÈTE — fichier local validé, sinon reconstituée depuis Jira
    # (commentaires « Analyse de l'agent » réassemblés, hors de JIRA_CONTEXT_MAX_CHARS) ; partie manquante : arrêt
    if args.step in ("patch", "refine") and not is_cucumber_client:
        try:
            restored = analysis_publication.ensure_local(target_issue)
        except analysis_publication.IncompleteAnalysis as e:
            print(f"[ANALYSE] ⛔ {e}")
            sys.exit(2)
        except RuntimeError as e:
            print(f"[ANALYSE] ⛔ Aucune analyse locale ({cadrage_file}) et analyse illisible dans Jira : {e}")
            sys.exit(2)
        if restored["source"] in ("commentaires", "description"):
            print(f"[ANALYSE] {cadrage_file} absent : {restored['message']} ({len(restored['texte'])} caractères, "
                  f"aucune troncature), fichier reconstitué.")
        elif not restored["source"]:
            print(f"[ANALYSE] ⛔ Aucune analyse pour {target_issue} (ni {cadrage_file}, ni dans Jira).")
            sys.exit(2)
    cadrage_before_run = _read_text(cadrage_file)
    epic_previous_manifest = load_epic_manifest(target_issue) if args.step == "epic-cadrage" else {}
    run_started_at = time.time()

    # Accès base en lecture seule (cadrage, refine, patch) ; le mot de passe reste dans le .env
    db_block = build_db_block(args.db_schema.strip(), python_exe, workspace_dir,
                                                                    step=args.step)

    # Contexte Jira injecté dans le cadrage : l'agent ne réinterroge Jira que si une information manque
    jira_context_block = format_jira_context(jira_context, _env_int("JIRA_CONTEXT_MAX_CHARS", 12000))
    if jira_context_block:
        print(f"[JIRA] Contexte du ticket injecté dans le prompt ({len(jira_context_block)} caractères).")
        jira_fetch_step = (
            f"Le contexte Jira du ticket {target_issue} (description, Épopée, COST) est fourni ci-dessous : n'interroge Jira "
            f"QUE si une information manque (Épopée ou COST absents alors qu'attendus, texte signalé TRONQUÉ). Dans ce cas, "
            f"récupère uniquement l'élément manquant, par exemple :\n"
            f"   \"{python_exe}\" -c \"from agent_socle.jira_tools import get_jira_issue_details; print(get_jira_issue_details('<CLÉ>'))\"\n"
            f"{jira_context_block}"
        )
    else:
        jira_fetch_step = f"Récupère les détails du ticket Jira cible {target_issue}."

    release_note_step = render_prompt("release_note", release_note_file=release_note_file).strip()
    # Documentation développeur ODATiO : analyse, cadrage, chiffrage et patch (hors génération Cucumber d'un client)
    docs_block = "" if is_cucumber_client or args.step not in ODATIO_DOCS_STEPS else odatio_docs_block(
        python_exe, ODATIO_DOCS_STEPS[args.step])

    # Variables communes des modèles de prompts (prompts/*.md)
    prompt_values = {
        "token_saving_rules": token_saving_rules, "capitalization_block": capitalization_block, "db_block": db_block,
        "knowledge_block": knowledge_block, "odatio_docs_block": docs_block,
        "target_issue": target_issue, "target_project": target_project, "conventions_path": conventions_path,
        "branch_rule": branch_rule, "repo_scope": repo_scope, "jira_fetch_step": jira_fetch_step,
        "cucumber_instruction": cucumber_instruction, "release_note_step": release_note_step,
        "local_client_repo": local_client_repo, "local_cucumber_std_repo": local_cucumber_std_repo,
        "odatio_version": odatio_version,
    }

    # Construction du prompt selon l'étape demandée
    if args.step == "cadrage":
        prompt = render_prompt("cadrage", **prompt_values)
    elif args.step == "epic-cadrage":
        manifest_file = epic_manifest_path(target_issue)
        known_keys = {t.get("key") for t in epic_previous_manifest.get("tasks", [])}
        other_children = [c for c in get_epic_children(target_issue) if c["key"] not in known_keys]
        children_block = ""
        if other_children:
            children_block = (
                "\nTickets déjà rattachés à l'épopée (ne propose pas de tâche pour un besoin qu'ils couvrent déjà) :\n"
                + "\n".join(f"- {c['key']} [{c['status']}] {c['summary']}" for c in other_children) + "\n"
            )
        revision_block = ""
        if epic_previous_manifest.get("tasks"):
            revision_block = "\n" + render_prompt(
                "epic_revision", manifest_file=manifest_file,
                previous_manifest_json=json.dumps(epic_previous_manifest, ensure_ascii=False, indent=2),
                revision_instructions=args.feedback or "aucune : vérifie et complète le découpage existant",
            )
        prompt = render_prompt("epic_cadrage", **prompt_values, manifest_file=manifest_file,
                               children_block=children_block, revision_block=revision_block)
    elif args.step == "refine":
        prompt = render_prompt("refine", **prompt_values, feedback=args.feedback)
    if args.step in ("cadrage", "refine", "epic-cadrage") and not is_cucumber_client:
        prompt += estimation_prompt(target_project, target_issue, jira_context, epic=args.step == "epic-cadrage")
    if args.step == "patch":
        prompt = render_prompt("patch_cucumber" if args.generate_cucumber else "patch", **prompt_values)

    # Modèle de prompt sans ${knowledge_block} (prompt personnalisé, besoin...) : le bloc est ajouté en fin de prompt
    if shared["bloc"] and shared["bloc"] not in prompt:
        prompt += knowledge_block
    # Même repli pour la consigne de la doc ODATiO (cadrage_reponses.md, prompts personnalisés)
    if docs_block and docs_block.strip() not in prompt:
        prompt += docs_block
    # Socle seul lu dans Bitbucket : la consigne d'accès au code vaut pour toutes les étapes (patch compris)
    if not jira_only and remote_code and repo_scope not in prompt:
        prompt += "\n\n=== CODE DU STANDARD ===\n" + repo_scope
    print(f"=== Exécution Étape: {args.step.upper()} ===")
    print(f"Projet Jira          : {target_project}")
    print(f"Ticket Cible         : {target_issue}")
    print(f"Version Odatio       : {odatio_version}")
    print(f"Génération Cucumber  : {'Oui' if args.generate_cucumber else 'Non'}")
    print(f"Dépôt Cucumber Std   : {local_cucumber_std_repo if local_cucumber_std_repo else ('Désactivé' if not args.generate_cucumber else 'Non configuré')}")

    claude_model = os.getenv(CLAUDE_MODEL_ENV[args.step], "").strip()
    claude_effort_level = claude_effort(CLAUDE_EFFORT_ENV[args.step])
    print(f"Modèle Claude        : {claude_model or 'défaut de Claude Code'} | "
          f"Effort : {claude_effort_level or 'défaut de Claude Code'}")
    print(f"Taille du prompt     : {len(prompt)} caractères")

    add_dirs = [workspace_dir] + [d for d in (local_std_repo, local_client_repo, local_cucumber_std_repo)
                                  if d and os.path.exists(d)]
    claude_extra = knowledge_tools.read_only_args() if shared["documents"] else ()
    claude_environment = None
    jira_before = {}
    jira_keys = []
    # Analyse non validée : aucune écriture Jira possible pour l'agent, descriptions relevées avant et après
    if jira_guard.applies(args.step):
        claude_extra = jira_guard.claude_args(workspace_dir, claude_extra)
        claude_environment = jira_guard.sandbox_env(claude_env())
        jira_keys = jira_guard.watched_keys(target_issue, args.step, epic_previous_manifest)
        jira_before = jira_guard.snapshot(jira_keys, args.step)
        print(f"[JIRA] Lecture seule pour l'agent pendant l'étape {args.step} (aucun outil ni jeton d'écriture) ; "
              f"descriptions relevées : {', '.join(jira_keys)}.")
        if jira_guard.unreadable(jira_before):
            print(f"[JIRA] ⚠️ État Jira illisible avant l'étape ({', '.join(jira_guard.unreadable(jira_before))}) : "
                  f"contrôle impossible pour ces éléments.")
    returncode, stdout_output, token_usage, session_limit_hit = run_claude(
        prompt, claude_model, add_dirs,
        ["Read", "Write", "Edit", "Grep", "Glob", "Bash", f"Bash({python_exe}:*)", "Bash(git:*)"],
        extra_args=claude_extra, effort=claude_effort_level, env=claude_environment)
    if jira_before and not check_jira_untouched(args.step, jira_before, jira_keys):
        sys.exit(1)
    report_token_usage(target_issue, args.step, claude_model, token_usage, len(prompt), returncode,
                       claude_effort_level)

    if returncode == 0 and not session_limit_hit:
        print(f"\n=== Étape {args.step.upper()} terminée avec succès pour {target_issue} ===")
        
        # Chiffrage : total recalculé sans IA (somme des activités) avant mémoire et relecture
        if args.step in ("cadrage", "refine") and produced(cadrage_file, run_started_at):
            finalize_estimation(cadrage_file, target_project)
        elif args.step == "epic-cadrage":
            for task in load_epic_manifest(target_issue).get("tasks", []):
                if task.get("file") and produced(task["file"], run_started_at):
                    finalize_estimation(task["file"], target_project)
        # Enregistrement en mémoire / capitalisation d'expérience
        cadrage_content = _read_text(cadrage_file)
        # Connaissances partagées : champs communs des analyses du ticket
        knowledge = {"client": ticket_client(args, target_project), "tickets": [target_issue],
                     "composants": jira_context.get("components", []),
                     "depots": knowledge_tools.repo_commits(local_std_repo, local_client_repo),
                     "sources": shared["documents"]}
        knowledge_subject = jira_context.get("summary") or target_issue
        if args.step in ("cadrage", "refine", "cadrage-reponses") and produced(cadrage_file, run_started_at):
            share_knowledge(cadrage_content, target_project, "cadrage", knowledge_subject, **knowledge)
        if args.step == "cadrage":
            if _safe_memory(save_cadrage_memory, target_issue, target_project,
                            cadrage_text=cadrage_content, jira_context=jira_context):
                print(f"[MÉMOIRE] Besoin initial et cadrage enregistrés pour {target_issue}.")
            print_draft_status(target_issue)
        elif args.step == "epic-cadrage":
            manifest_file = epic_manifest_path(target_issue)
            # En révision, l'agent peut ne retoucher que les analyses : le manifeste existant reste valable
            is_revision = bool(epic_previous_manifest.get("tasks"))
            if not os.path.exists(manifest_file) or (not is_revision and os.path.getmtime(manifest_file) < run_started_at):
                print(f"[ÉPOPÉE] ❌ L'agent n'a pas écrit le manifeste '{manifest_file}' pendant cette exécution.")
                sys.exit(1)
            if args.feedback.strip():
                _safe_memory(save_cadrage_memory, target_issue, target_project, jira_context=jira_context)
                _safe_memory(save_refine_feedback, target_issue, args.feedback)
            if not finalize_epic_draft(target_issue, epic_previous_manifest):
                sys.exit(1)
            task_keys = [t["key"] for t in load_epic_manifest(target_issue).get("tasks", []) if t.get("key")]
            share_knowledge(epic_markdown(target_issue), target_project, "decoupage-epopee", knowledge_subject,
                            **{**knowledge, "tickets": [target_issue] + task_keys})
        elif args.step == "refine":
            if _safe_memory(save_refine_feedback, target_issue, args.feedback,
                            previous_cadrage=cadrage_before_run, updated_cadrage=cadrage_content):
                print(f"[MÉMOIRE] Consigne de révision et écarts enregistrés pour {target_issue}.")
            print_draft_status(target_issue)
        elif args.step == "patch":
            # Seuls les patchs produits pendant CETTE exécution
            patch_paths = [f for f in os.listdir(".")
                           if f.startswith(f"{target_issue}-") and f.endswith(".patch")
                           and os.path.getmtime(f) >= run_started_at]
            if not patch_paths:
                print("[MÉMOIRE] ⚠️ Aucun patch généré pendant cette exécution : mémoire non mise à jour.")
            else:
                if _safe_memory(save_patch_memory, target_issue, patch_paths, agent_output=stdout_output,
                                final_cadrage=cadrage_content, jira_context=jira_context):
                    print(f"[MÉMOIRE] Livrable validé indexé : {', '.join(patch_paths)}.")
                publish_release_note(target_issue, release_note_file, run_started_at)
                attach_patches(target_issue, patch_paths)
                # Registre local des livraisons (lu par agent-manager-savoye) : pièces jointes vérifiées en lecture
                if livraisons.noter_patch(target_issue, patch_paths):
                    print(f"[LIVRAISON] Patch(s) de {target_issue} notés dans le registre des livraisons.")
                share_knowledge(patch_recap(target_issue, patch_paths, stdout_output), target_project, "recap-patch",
                                knowledge_subject, **knowledge)
                if produced(release_note_file, run_started_at):
                    share_file(release_note_file, target_project, "release-note", knowledge_subject, **knowledge)
    else:
        print(f"\n=== Étape {args.step.upper()} interrompue pour {target_issue} (Code: {returncode}) ===")
        # Quota atteint : Claude peut sortir en code 0, on signale quand même l'échec
        sys.exit(returncode or 1)


def clean_release_note(text: str) -> str:
    """Ramène le fichier rédigé par l'agent à un seul paragraphe (sans titre ni puce)."""
    lines = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s == "---" or re.fullmatch(r"\**release\s*notes?\s*:?\**", s, re.IGNORECASE):
            continue
        lines.append(re.sub(r"^[-*]\s+", "", s))
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def publish_release_note(issue_key: str, note_path: str, run_started_at: float) -> bool:
    """Ajoute la release note en fin de description Jira. Un échec n'interrompt jamais l'étape patch."""
    if not os.path.exists(note_path) or os.path.getmtime(note_path) < run_started_at:
        print(f"[RELEASE NOTE] ⚠️ '{note_path}' n'a pas été produit pendant cette exécution : description Jira inchangée.")
        return False
    paragraph = clean_release_note(_read_text(note_path))
    if not paragraph:
        print(f"[RELEASE NOTE] ⚠️ '{note_path}' est vide : description Jira inchangée.")
        return False
    try:
        update_jira_release_note(issue_key, paragraph)
        print(f"[RELEASE NOTE] ✅ Paragraphe « Release note » ajouté à la fin de la description de {issue_key}.")
        return True
    except Exception as e:
        print(f"[RELEASE NOTE] ⚠️ Mise à jour de la description Jira impossible ({e}). Fichier local conservé : {note_path}")
        return False


def attach_patches(issue_key: str, patch_paths: list) -> bool:
    """Joint les patchs au ticket Jira (sans doublon). Un échec n'interrompt jamais l'étape patch."""
    try:
        messages = attach_files(issue_key, patch_paths)
    except Exception as e:
        print(f"[JIRA] ⚠️ Pièce(s) jointe(s) impossible(s) sur {issue_key} ({e}). Patch(s) locaux conservés.")
        return False
    failed = [m for m in messages if m.startswith("Erreur")]
    for message in messages:
        print(f"[JIRA] {'⚠️' if message in failed else '✅'} {message}")
    return not failed


def epic_markdown(epic_key: str) -> str:
    """Découpage d'une épopée en un document : une section par tâche (clé, titre) suivie de son analyse."""
    tasks = load_epic_manifest(epic_key).get("tasks", [])
    parts = [f"# Découpage de l'épopée {epic_key}", ""]
    for number, task in enumerate(tasks, 1):
        parts += [f"## {number}. {task.get('key') or 'tâche non créée'} — {task.get('summary', '')}", "",
                  _read_text(task.get("file") or "").strip(), ""]
    if tasks and os.getenv("ESTIMATION_ENABLED", "1").strip() != "0":
        # Chiffrage par tâche enfant et total de l'épopée
        parts += [estimation.epic_summary([(f"{t.get('key') or f'Tâche {n}'} — {t.get('summary', '')}",
                                            _read_text(t.get("file") or "")) for n, t in enumerate(tasks, 1)],
                                          epic_key.split("-")[0]), ""]
    return "\n".join(parts) if tasks else ""


def patch_recap(issue_key: str, patch_paths: list, agent_output: str) -> str:
    """Récapitulatif d'un patch : fichiers produits et fin de la sortie de l'agent (son récapitulatif)."""
    names = "\n".join(f"- {os.path.basename(p)}" for p in patch_paths)
    return f"# Récapitulatif du patch {issue_key}\n\nPatchs produits :\n{names}\n\n{_extract_recap(agent_output, 6000)}\n"


def cadrage_remote_plan(std_key: str, client_repo_arg: str, http=None) -> dict:
    """
    Cadrage d'un ticket client (ni socle seul, ni Cucumber) : Standard et dépôt client lus dans Bitbucket par l'API
    REST (tools/bitbucket_code.py), sans clone ni fetch. {"remote": bool, "motif": raison du mode local,
    "std": RemoteStandard, "client_project", "client_repos": [dépôts] ou None (projet entier)}. Configuration
    incomplète, dépôt client sans adresse Bitbucket ou API en erreur : mode local (copies d'ensure_local_repo).
    """
    from agent_socle import bitbucket_code
    base = os.getenv("BITBUCKET_URL", "").strip().rstrip("/")
    local = lambda motif: {"remote": False, "motif": motif}  # noqa: E731
    if not base:
        return local("BITBUCKET_URL non renseignée")
    if not os.getenv("BITBUCKET_HTTP_TOKEN", "").strip():
        return local("BITBUCKET_HTTP_TOKEN non renseigné")
    if not std_key:
        return local("BITBUCKET_STD_REPO_PATH n'est pas l'adresse d'un projet Bitbucket (…/projects/CLE)")
    client_project, client_repos = bitbucket_code.client_source(client_repo_arg, base)
    if not client_project:
        return local(f"dépôt client « {client_repo_arg or 'non renseigné'} » sans adresse Bitbucket de {base} "
                     f"(URL de projet ou de dépôt, ou remote origin d'un seul projet)")
    std = bitbucket_std.RemoteStandard(std_key, http=http)
    client = bitbucket_std.RemoteStandard(client_project, http=http)
    try:
        if not std.repos():
            return local(f"projet du Standard {std_key} vide ou absent dans Bitbucket")
        found = client.repos()
    except bitbucket_std.BitbucketUnavailable as exc:
        return local(f"API Bitbucket indisponible ({exc})")
    missing = [r for r in (client_repos or []) if r not in found]
    if not found or missing:
        return local(f"dépôt(s) client absent(s) du projet {client_project} dans Bitbucket "
                     f"({', '.join(missing) or 'aucun dépôt'})")
    return {"remote": True, "motif": "", "std": std, "client_project": client_project, "client_repos": client_repos}


def cadrage_remote_scope(plan: dict, odatio_version: str, python_exe: str, workspace_dir: str) -> str:
    """Consigne d'accès au code du cadrage lu dans Bitbucket (bloc bitbucket_code_cadrage) et lignes de journal."""
    from agent_socle.versions import normalize, std_refs
    cli = f'"{python_exe}" "{os.path.join(SOCLE_DIR, "bitbucket_code.py")}"'
    std_project = plan["std"].project
    # Version du Standard : ODATIO_VERSION (références de version) sinon branche par défaut, comme la copie locale
    std_candidates = list(std_refs(odatio_version, ("HEAD",)))
    version = normalize(odatio_version)[0]
    std_ref_label = (f"version {version} : {', '.join(std_candidates[:-1])}, à défaut branche par défaut"
                     if version else "branche par défaut de chaque dépôt")
    repos = plan["client_repos"]
    client_repos = f"dépôt(s) {', '.join(repos)}" if repos else "tous les dépôts du projet"
    client_cli = f"{cli} --project {plan['client_project']} --branches origin/integration"
    print(f"[CODE CADRAGE] Standard : Bitbucket REST, projet {std_project}, {std_ref_label}.")
    print(f"[CODE CADRAGE] Client : Bitbucket REST, projet {plan['client_project']}, {client_repos}, branche "
          f"integration.")
    print("[CODE CADRAGE] Aucune copie locale (ni clone, ni fetch) pour le cadrage ; le patch copiera le dépôt à "
          "modifier, comme avant.")
    return render_prompt("bitbucket_code_cadrage", std_project=std_project, std_ref_label=std_ref_label,
                         std_cli=f"{cli} --project {std_project} --branches {','.join(std_candidates)}",
                         client_project=plan["client_project"], client_repos=client_repos,
                         client_cli=client_cli + (f" --repos {','.join(repos)}" if repos else ""))


def estimation_prompt(project: str, issue_key: str, jira_context: dict, epic: bool = False) -> str:
    """
    Consignes de la section « Chiffrage » avec l'historique de calibrage : temps réels des tickets livrés d'abord
    enregistrés dans la mémoire, puis tickets similaires de la mémoire et de Jira. ESTIMATION_ENABLED=0 : ''.
    """
    if os.getenv("ESTIMATION_ENABLED", "1").strip() == "0":
        return ""
    query_text = f"{(jira_context or {}).get('summary', '')}\n{(jira_context or {}).get('description', '')[:3000]}"
    try:
        for item in estimation.record_actuals(project):
            print(f"[CHIFFRAGE] {item['cle']} livré : {item['valeur']} {item['unite']} réellement passés pour "
                  f"{item['chiffrage']} chiffrés (écart {item['ecart']}).")
    except Exception as e:  # noqa: BLE001 - mémoire ou Jira illisible : calibrage sans retour d'expérience
        print(f"[CHIFFRAGE] ⚠️ Temps réels non relus ({e}).")
    config = estimation.settings(project)
    history = (estimation.memory_history(project, query_text, issue_key, (jira_context or {}).get("components"))
               + estimation.jira_history(project, query_text, issue_key, config))
    print(f"[CHIFFRAGE] Unité {config['unite']}, {len(history)} ticket(s) similaire(s) pour le calibrage"
          + ("" if history else " : aucun historique exploitable, fourchette élargie demandée") + ".")
    return estimation.prompt_block(project, history, epic)


def finalize_estimation(path: str, project: str) -> None:
    """Section « Chiffrage » d'une analyse écrite : total recalculé (somme des activités) et contrôle signalé."""
    text = _read_text(path)
    if not text or os.getenv("ESTIMATION_ENABLED", "1").strip() == "0":
        return
    fixed, changed = estimation.normalize(text, estimation.settings(project)["unite"])
    if changed:
        with open(path, "w", encoding="utf-8") as f:
            f.write(fixed)
        print(f"[CHIFFRAGE] {path} : total recalculé (somme des activités) et unité du projet.")
    problems = estimation.check(fixed, estimation.settings(project))
    if problems:
        print(f"[CHIFFRAGE] ⚠️ {path} : " + " ; ".join(problems))


def finalize_epic_draft(epic_key: str, previous_manifest: dict) -> bool:
    """
    Découpage rédigé ou révisé par l'agent : il reste un brouillon local. Aucune tâche n'est créée ni mise à jour
    dans Jira avant la validation du découpage par l'utilisateur (étape epic-sync).
    """
    manifest = load_epic_manifest(epic_key)
    if not manifest.get("tasks"):
        print(f"[ÉPOPÉE] ❌ Manifeste '{epic_manifest_path(epic_key)}' absent, illisible ou sans tâche.")
        return False
    # Clés des tâches déjà créées, si l'agent les a perdues en réécrivant le manifeste
    restore_epic_keys(manifest, previous_manifest or {})
    attached = attach_existing_analyses(manifest)
    save_epic_manifest(epic_key, manifest)
    if attached:
        print(f"[ÉPOPÉE] Analyse rattachée à une tâche existante, écrite dans son fichier local : "
              f"{', '.join(f'{k}-cadrage.md' for k in attached)} (brouillon, publiée à la validation).")
    for task in missing_analyses(manifest):
        print(f"[ÉPOPÉE] ❌ {task.get('key') or task.get('summary')} : analyse absente (fichier "
              f"'{task.get('file')}' vide ou manquant) — générer ou réviser le découpage.")
    drafts = draft_tasks(manifest)
    created = [t for t in manifest["tasks"] if t.get("key")]
    print(f"[ÉPOPÉE] Découpage de {epic_key} en brouillon : {len(manifest['tasks'])} tâche(s)"
          + (f", dont {len(drafts)} à créer dans Jira" if drafts else "")
          + (f" ; analyses à republier après validation : {', '.join(t['key'] for t in created)}" if created else "")
          + ". Rien n'est créé ni publié dans Jira avant la validation du découpage.")
    return True


if __name__ == "__main__":
    # Fichiers produits ('<TICKET>-cadrage.md', patchs…) relatifs au dossier courant : AGENT_DATA_DIR
    os.chdir(DATA_DIR)
    main()
