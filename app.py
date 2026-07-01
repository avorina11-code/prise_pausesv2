"""
app.py
------
Mvola AE — Pause Pilot
Application Streamlit de régulation, traçabilité et audit des pauses agents.

Lancement :
    streamlit run app.py

Architecture :
    database.py        -> couche SQLite (schéma, CRUD)
    business_rules.py  -> moteur de règles (quotas, simultanéité, fractionnement)
    reconciliation.py  -> module d'analyse à froid Vocalcom (Axe 2)
    app.py (ce fichier) -> UI Streamlit + RBAC + orchestration
"""

import streamlit as st
import pandas as pd
from datetime import date, datetime

import database as db
import business_rules as rules
import reconciliation as reco

# --------------------------------------------------------------------------
# Configuration générale de la page
# --------------------------------------------------------------------------
st.set_page_config(
    page_title="Mvola AE — Pause Pilot",
    page_icon="⏸️",
    layout="wide",
)

# Initialisation automatique des tables SQLite au premier lancement
db.init_db()

PAUSE_TYPES = ["Dejeuner", "Besoin", "Personnelle", "Autre"]
PAUSE_LABELS = {
    "Dejeuner": "🍽️ Pause Déjeuner",
    "Besoin": "🚻 Pause Besoin / Urgente",
    "Personnelle": "🙋 Pause Personnelle",
    "Autre": "📋 Autre",
}


# --------------------------------------------------------------------------
# Authentification
# --------------------------------------------------------------------------
def login_screen():
    st.title("⏸️ Mvola AE — Pause Pilot")
    st.caption("Outil WFM de régulation, traçabilité et audit des pauses agents.")

    with st.form("login_form"):
        matricule = st.text_input("Matricule")
        password = st.text_input("Mot de passe", type="password")
        submitted = st.form_submit_button("Se connecter", use_container_width=True)

    if submitted:
        user = db.get_user(matricule.strip())
        if user and user["actif"] and db.verify_password(password, user["password_hash"]):
            st.session_state["user"] = user
            st.rerun()
        else:
            st.error("Matricule ou mot de passe incorrect, ou compte désactivé.")

    st.info("Compte Admin par défaut au premier lancement : **admin / admin123** "
            "(à changer immédiatement dans Gestion des Accès).")


def logout_button():
    if st.sidebar.button("🚪 Déconnexion", use_container_width=True):
        del st.session_state["user"]
        st.rerun()


# --------------------------------------------------------------------------
# Interface AGENT
# --------------------------------------------------------------------------
def agent_interface(user):
    st.title(f"👋 Bonjour {user['nom_complet']}")
    st.caption(f"Matricule : {user['matricule']} — {db.today_mg().strftime('%d/%m/%Y')}")
    _agent_live_panel(user)


@st.fragment(run_every=5)
def _agent_live_panel(user):
    """
    Panneau vivant de l'espace Agent : statut de pause, quotas et historique.
    Ce fragment se réactualise tout seul toutes les 5 secondes (compteur de
    temps de pause, quotas, seuil de simultanéité) SANS recharger toute la
    page et SANS jamais redemander le matricule / mot de passe : la session
    de connexion (st.session_state) n'est pas touchée par ce rafraîchissement.
    """
    st.caption(f"🕒 Heure Madagascar : {db.now_mg().strftime('%H:%M:%S')} — "
               "synchronisation automatique toutes les 5 secondes")

    active_pause = db.get_active_pause(user["matricule"])

    st.divider()

    if active_pause:
        debut = db.parse_iso(active_pause["heure_debut"])
        ecoule = round((db.now_mg() - debut).total_seconds() / 60.0, 1)
        st.warning(f"Vous êtes actuellement en **{PAUSE_LABELS[active_pause['type_pause']]}** "
                   f"depuis {ecoule} minute(s).")
        if st.button("⏹️ Déclarer mon Retour de Pause", type="primary", use_container_width=True):
            duree = db.end_pause(user["matricule"])
            st.success(f"Retour de pause enregistré. Durée : {duree} min.")
            st.rerun(scope="fragment")
    else:
        type_pause = st.selectbox(
            "Type de pause",
            PAUSE_TYPES,
            format_func=lambda t: PAUSE_LABELS[t],
        )

        quota = rules.evaluate_pause_quota(user["matricule"], type_pause)
        simult = rules.evaluate_simultaneity(type_pause)

        _render_quota_banner(quota)
        if simult["alerte_vigie"]:
            st.warning("⚠️ " + simult["message"])

        bouton_desactive = (not quota["autorise"]) or simult["bloque"]

        if st.button("▶️ Déclencher ma Pause", type="primary",
                      use_container_width=True, disabled=bouton_desactive):
            db.start_pause(user["matricule"], type_pause)
            st.success(f"{PAUSE_LABELS[type_pause]} démarrée.")
            st.rerun(scope="fragment")

        if bouton_desactive and not quota["autorise"]:
            st.error("Bouton indisponible : quota journalier atteint pour ce type de pause.")
        elif bouton_desactive and simult["bloque"]:
            st.error("Bouton temporairement indisponible : seuil de simultanéité des pauses atteint "
                      "(15% de l'effectif). Réessayez dans quelques minutes.")

    st.divider()
    st.subheader("📊 Mes compteurs du jour")
    cols = st.columns(len(PAUSE_TYPES))
    for i, t in enumerate(PAUSE_TYPES):
        q = rules.evaluate_pause_quota(user["matricule"], t)
        with cols[i]:
            label = PAUSE_LABELS[t]
            max_occ = q["max_occurrences"] if q["max_occurrences"] is not None else "∞"
            st.metric(label, f"{q['occurrences']}/{max_occ}")
            if q["max_duree"] is not None:
                st.caption(f"Durée cumulée : {q['duree_cumulee']} / {q['max_duree']} min")

    st.divider()
    st.subheader("🕓 Historique du jour")
    logs = db.get_today_logs(user["matricule"])
    if logs:
        dfl = pd.DataFrame(logs)[["type_pause", "heure_debut", "heure_fin", "duree_minutes", "statut"]]
        dfl["type_pause"] = dfl["type_pause"].map(PAUSE_LABELS)
        st.dataframe(dfl, use_container_width=True, hide_index=True)
    else:
        st.caption("Aucune pause enregistrée aujourd'hui.")


def _render_quota_banner(quota):
    msg = f"Occurrences : {quota['occurrences']}/{quota['max_occurrences'] or '∞'} — " \
          f"Durée cumulée : {quota['duree_cumulee']} / {quota['max_duree'] or '∞'} min"
    if quota["niveau"] == "ok":
        st.success(msg + "  \n" + quota["message"])
    elif quota["niveau"] == "warning":
        st.warning(msg + "  \n" + quota["message"])
    else:
        st.error(msg + "  \n" + quota["message"])


# --------------------------------------------------------------------------
# Interface ADMIN
# --------------------------------------------------------------------------
def admin_interface(user):
    st.title("🛡️ Espace Administrateur — Vigie")
    st.caption(f"Connecté en tant que {user['nom_complet']} ({user['matricule']})")

    tab_dash, tab_users, tab_cold, tab_config = st.tabs(
        ["📡 Dashboard Temps Réel", "👥 Gestion des Accès", "🧊 Analyse à Froid", "⚙️ Configuration"]
    )

    with tab_dash:
        render_dashboard()
    with tab_users:
        render_user_management()
    with tab_cold:
        render_cold_analysis()
    with tab_config:
        render_config()


@st.fragment(run_every=10)
def render_dashboard():
    st.subheader("📡 Supervision temps réel des flux de pause")
    st.caption(f"🕒 Dernière synchronisation : {db.now_mg().strftime('%H:%M:%S')} "
               "(heure Madagascar) — actualisation automatique toutes les 10 secondes")

    effectif_total = int(db.get_config("effectif_total", 100))
    seuil_dej = float(db.get_config("seuil_simultaneite_dejeuner_pct", 15))
    seuil_bes = float(db.get_config("seuil_simultaneite_besoin_pct", 10))
    actives = db.get_all_active_pauses()
    nb_actives = len(actives)
    nb_dejeuner = len([a for a in actives if a["type_pause"] == "Dejeuner"])
    nb_besoin = len([a for a in actives if a["type_pause"] == "Besoin"])
    taux = round((nb_actives / effectif_total) * 100, 1) if effectif_total else 0
    taux_dej = round((nb_dejeuner / effectif_total) * 100, 1) if effectif_total else 0
    taux_bes = round((nb_besoin / effectif_total) * 100, 1) if effectif_total else 0

    alerte_dej = taux_dej > seuil_dej
    alerte_bes = taux_bes > seuil_bes

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Effectif total configuré", effectif_total)
    c2.metric("Agents en pause (total)", nb_actives)
    c3.metric("🍽️ Déjeuner simultané", f"{taux_dej}%",
              delta=f"Seuil {seuil_dej}%", delta_color="inverse")
    c4.metric("🚻 Besoin simultané", f"{taux_bes}%",
              delta=f"Seuil {seuil_bes}%", delta_color="inverse")

    c5, c6 = st.columns(2)
    c5.metric("Statut Déjeuner", "🔴 Dépassé" if alerte_dej else "🟢 OK")
    c6.metric("Statut Besoin",   "🔴 Dépassé" if alerte_bes else "🟢 OK")

    if alerte_dej:
        st.error(f"⚠️ ALERTE VIGIE — Pause Déjeuner : {nb_dejeuner}/{effectif_total} agents "
                  f"({taux_dej}% > seuil {seuil_dej}%).")
    if alerte_bes:
        st.warning(f"⚠️ ALERTE VIGIE — Pause Besoin : {nb_besoin}/{effectif_total} agents "
                    f"({taux_bes}% > seuil {seuil_bes}%) — départ jamais bloqué (règle légale).")

    st.markdown("#### Agents actuellement en pause")
    if actives:
        df_act = pd.DataFrame(actives)
        df_act["type_pause"] = df_act["type_pause"].map(PAUSE_LABELS)
        df_act["heure_debut"] = pd.to_datetime(df_act["heure_debut"]).dt.strftime("%H:%M:%S")
        st.dataframe(
            df_act[["matricule", "type_pause", "heure_debut"]],
            use_container_width=True, hide_index=True,
        )
    else:
        st.caption("Aucun agent en pause actuellement.")

    st.markdown("#### Historique complet")
    all_logs = db.get_all_logs()
    if all_logs:
        dfl = pd.DataFrame(all_logs)
        dfl["type_pause"] = dfl["type_pause"].map(PAUSE_LABELS)
        st.dataframe(
            dfl[["matricule", "type_pause", "date_jour", "heure_debut", "heure_fin",
                 "duree_minutes", "statut"]],
            use_container_width=True, hide_index=True,
        )
        st.download_button(
            "📥 Exporter l'historique (CSV)",
            dfl.to_csv(index=False).encode("utf-8"),
            file_name=f"pause_logs_export_{db.today_mg().isoformat()}.csv",
            mime="text/csv",
        )
    else:
        st.caption("Aucun log enregistré pour le moment.")

    st.markdown("#### 🔍 Détection de fractionnement abusif (micro-pauses < 3 min)")
    agents = [u for u in db.list_users() if u["role"] == "Agent"]
    frac_rows = []
    for a in agents:
        micro_pauses = rules.detect_fractionnement(a["matricule"])
        if micro_pauses:
            frac_rows.append({
                "matricule": a["matricule"],
                "nom": a["nom_complet"],
                "nb_micro_pauses": len(micro_pauses),
            })
    if frac_rows:
        st.dataframe(pd.DataFrame(frac_rows), use_container_width=True, hide_index=True)
    else:
        st.caption("Aucun fractionnement abusif détecté aujourd'hui.")


def render_user_management():
    st.subheader("👥 Gestion des Accès (Admin uniquement)")

    with st.expander("➕ Ajouter un utilisateur", expanded=False):
        with st.form("add_user_form", clear_on_submit=True):
            c1, c2 = st.columns(2)
            matricule = c1.text_input("Matricule")
            nom = c2.text_input("Nom complet")
            c3, c4 = st.columns(2)
            password = c3.text_input("Mot de passe", type="password")
            role = c4.selectbox("Rôle", ["Agent", "Admin"])
            ok = st.form_submit_button("Créer l'utilisateur")
            if ok:
                if not matricule or not nom or not password:
                    st.error("Tous les champs sont obligatoires.")
                elif db.get_user(matricule):
                    st.error("Ce matricule existe déjà.")
                else:
                    db.add_user(matricule, nom, password, role)
                    st.success(f"Utilisateur {matricule} créé.")
                    st.rerun()

    st.markdown("#### Dictionnaire de données complet des utilisateurs")
    users = db.list_users()
    if not users:
        st.caption("Aucun utilisateur enregistré.")
        return

    df_users = pd.DataFrame(users)[["matricule", "nom_complet", "role", "actif", "date_creation"]]
    st.dataframe(df_users, use_container_width=True, hide_index=True)

    st.markdown("#### Modifier / Supprimer un utilisateur")
    matricules = [u["matricule"] for u in users]
    selected = st.selectbox("Sélectionner un matricule", matricules)
    target = db.get_user(selected)

    if target:
        with st.form("edit_user_form"):
            c1, c2 = st.columns(2)
            nom = c1.text_input("Nom complet", value=target["nom_complet"])
            role = c2.selectbox("Rôle", ["Agent", "Admin"],
                                 index=["Agent", "Admin"].index(target["role"]))
            c3, c4 = st.columns(2)
            actif = c3.checkbox("Compte actif", value=bool(target["actif"]))
            new_password = c4.text_input("Nouveau mot de passe (laisser vide pour ne pas changer)",
                                          type="password")
            c5, c6 = st.columns(2)
            update_ok = c5.form_submit_button("💾 Enregistrer les modifications")
            delete_ok = c6.form_submit_button("🗑️ Supprimer cet utilisateur")

            if update_ok:
                db.update_user(selected, nom_complet=nom, role=role, actif=actif,
                                new_password=new_password or None)
                st.success("Utilisateur mis à jour.")
                st.rerun()
            if delete_ok:
                if selected == st.session_state["user"]["matricule"]:
                    st.error("Vous ne pouvez pas supprimer votre propre compte connecté.")
                else:
                    db.delete_user(selected)
                    st.success("Utilisateur supprimé.")
                    st.rerun()


def render_cold_analysis():
    st.subheader("🧊 Module d'Analyse à Froid & Synchronicité")
    st.caption(
        "Importez l'extraction Vocalcom de la veille (agents en lignes, tranches horaires/statuts "
        "en colonnes) pour rapprocher les heures déclarées sur le site et les heures réellement "
        "constatées sur le bandeau Vocalcom."
    )

    jour_analyse = st.date_input("Date à analyser (logs internes)", value=db.today_mg())
    uploaded = st.file_uploader("Fichier d'extraction Vocalcom (xlsx ou csv)", type=["xlsx", "csv"])

    if uploaded is not None:
        try:
            if uploaded.name.endswith(".csv"):
                raw_df = pd.read_csv(uploaded)
            else:
                raw_df = pd.read_excel(uploaded)
        except Exception as e:
            st.error(f"Impossible de lire le fichier importé : {e}")
            return

        try:
            parsed_df = reco.parse_vocalcom_matrix(raw_df)
        except Exception as e:
            st.error(f"Erreur lors du parsing de la matrice Vocalcom (formats de date/colonnes) : {e}")
            return

        if parsed_df.empty:
            st.warning("Aucun évènement exploitable trouvé dans le fichier importé. "
                       "Vérifiez le format des en-têtes de colonnes (horodatages) et des cellules.")
            return

        st.success(f"{len(parsed_df)} évènements Vocalcom interprétés avec succès.")
        with st.expander("Aperçu des évènements normalisés"):
            st.dataframe(parsed_df.head(50), use_container_width=True, hide_index=True)

        try:
            archivable = parsed_df.assign(heure_constatee=parsed_df["heure_constatee"].astype(str))
            db.save_vocalcom_rows(archivable.to_dict("records"), uploaded.name)
        except Exception as e:
            st.warning(f"Les évènements n'ont pas pu être archivés en base : {e}")

        st.markdown("#### 🔁 Rapprochement (Reconciliation)")
        try:
            recon_df = reco.reconcile(parsed_df, jour_analyse.isoformat())
        except Exception as e:
            st.error(f"Erreur lors du rapprochement : {e}")
            return

        if recon_df.empty:
            st.info("Aucun log interne (pause_logs) trouvé pour la date sélectionnée, "
                    "impossible de rapprocher avec l'extraction Vocalcom.")
            return

        recon_df["type_pause"] = recon_df["type_pause"].map(PAUSE_LABELS)

        def _style_row(row):
            if "Fraude" in row["indicateur_conformite"]:
                return ["background-color: #ffcccc"] * len(row)
            if row["indicateur_conformite"] == "A surveiller":
                return ["background-color: #fff3cd"] * len(row)
            return [""] * len(row)

        st.dataframe(
            recon_df.style.apply(_style_row, axis=1),
            use_container_width=True, hide_index=True,
        )

        st.download_button(
            "📥 Exporter le rapport de conformité (CSV)",
            recon_df.to_csv(index=False).encode("utf-8"),
            file_name=f"rapport_conformite_{jour_analyse.isoformat()}.csv",
            mime="text/csv",
        )

        st.markdown("#### Synthèse par agent")
        synth = recon_df.groupby("matricule")["indicateur_conformite"].value_counts().unstack(fill_value=0)
        st.dataframe(synth, use_container_width=True)


def render_config():
    st.subheader("⚙️ Configuration des règles métier")
    cfg = db.get_all_config()

    with st.form("config_form"):
        effectif_total = st.number_input("Effectif total configuré", min_value=1,
                                          value=int(cfg.get("effectif_total", 100)))

        st.markdown("**Seuils de simultanéité par type de pause**")
        c1, c2 = st.columns(2)
        seuil_dejeuner = c1.number_input(
            "🍽️ Pause Déjeuner — seuil max (%)", min_value=1, max_value=100,
            value=int(float(cfg.get("seuil_simultaneite_dejeuner_pct", 15))),
            help="% maximal d'agents pouvant être simultanément en pause Déjeuner. "
                 "Au-delà, le bouton est désactivé pour les autres agents."
        )
        seuil_besoin = c2.number_input(
            "🚻 Pause Besoin — seuil max (%)", min_value=1, max_value=100,
            value=int(float(cfg.get("seuil_simultaneite_besoin_pct", 10))),
            help="% maximal pour la Pause Besoin. Le départ n'est jamais bloqué "
                 "(raison légale), mais une alerte Vigie est levée au-dessus de ce seuil."
        )
        duree_dejeuner = st.number_input("Durée pause déjeuner (min)", min_value=1,
                                          value=int(float(cfg.get("duree_dejeuner_min", 45))))
        max_occ_besoin = st.number_input("Max occurrences pause Besoin / jour", min_value=1,
                                          value=int(float(cfg.get("max_occurrences_besoin", 3))))
        max_duree_besoin = st.number_input("Durée cumulée max pause Besoin (min/jour)", min_value=1,
                                            value=int(float(cfg.get("max_duree_cumulee_besoin", 15))))
        ok = st.form_submit_button("💾 Enregistrer la configuration")

        if ok:
            db.set_config("effectif_total", effectif_total)
            db.set_config("seuil_simultaneite_dejeuner_pct", seuil_dejeuner)
            db.set_config("seuil_simultaneite_besoin_pct", seuil_besoin)
            db.set_config("duree_dejeuner_min", duree_dejeuner)
            db.set_config("max_occurrences_besoin", max_occ_besoin)
            db.set_config("max_duree_cumulee_besoin", max_duree_besoin)
            st.success("Configuration mise à jour.")
            st.rerun()


# --------------------------------------------------------------------------
# Point d'entrée
# --------------------------------------------------------------------------
def main():
    if "user" not in st.session_state:
        login_screen()
        return

    user = st.session_state["user"]
    st.sidebar.title("⏸️ Pause Pilot")
    st.sidebar.caption(f"{user['nom_complet']}\n\nRôle : **{user['role']}**")
    st.sidebar.caption(f"🕒 {db.now_mg().strftime('%d/%m/%Y %H:%M:%S')} (heure Madagascar)")
    logout_button()

    if user["role"] == "Admin":
        admin_interface(user)
    else:
        agent_interface(user)


if __name__ == "__main__":
    main()
