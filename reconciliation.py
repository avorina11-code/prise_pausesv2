"""
reconciliation.py
------------------
Module d'analyse à froid & synchronicité (Axe 2 - 20% du projet).

Permet :
- de parser une matrice Vocalcom (agents en lignes, tranches horaires/statuts en colonnes)
- de la transformer en évènements normalisés (matricule, heure, statut)
- de la rapprocher des logs internes SQLite (pause_logs) générés par les clics agents
- de calculer un score de conformité par agent basé sur la tranche de 30 minutes.
"""

import pandas as pd
from datetime import datetime, time
import database as db


# --------------------------------------------------------------------------
# Etape 1 : parsing robuste de la matrice Vocalcom
# --------------------------------------------------------------------------
def parse_vocalcom_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """
    Transforme un tableau croisé dynamique (agents en lignes, colonnes = tranches
    horaires / statuts) en un format long et normalisé :
        matricule | heure_constatee (datetime) | statut_constate
    """
    if df.empty:
        raise ValueError("Le fichier importé est vide.")

    agent_col = df.columns[0]
    time_cols = df.columns[1:]

    records = []
    for _, row in df.iterrows():
        matricule = str(row[agent_col]).strip()
        if not matricule or matricule.lower() == "nan" or matricule.lower() == "agent":
            continue
            
        for col in time_cols:
            statut = row[col]
            if pd.isna(statut) or str(statut).strip() == "":
                continue
                
            # Extraction de l'heure de la colonne (ex: "07:30:00")
            heure_parsee = _safe_parse_datetime(col)
            if heure_parsee is None:
                continue
                
            records.append({
                "matricule": matricule,
                "heure_constatee": heure_parsee,
                "statut_constate": str(statut).strip(),
            })

    return pd.DataFrame(records)


def _safe_parse_datetime(value):
    """
    Parsing adapté aux en-têtes de type 'HH:MM:SS' ou 'HH:MM' de Vocalcom.
    Associe l'heure de la tranche à la date du jour pour l'analyse.
    """
    val_str = str(value).strip()
    
    # Si c'est déjà un objet de type heure ou datetime
    if isinstance(value, (datetime, time)):
        if isinstance(value, time):
            dt = datetime.combine(datetime.today(), value)
        else:
            dt = value
        if dt.minute in (0, 30):
            return dt
        return None

    # Découpage du texte au format HH:MM:SS
    try:
        parts = val_str.split(':')
        if len(parts) >= 2:
            h = int(parts[0])
            m = int(parts[1])
            if m in (0, 30):
                return datetime.combine(datetime.today(), time(h, m))
    except Exception:
        pass

    # Analyse standard si une date complète est présente
    try:
        parsed = pd.to_datetime(value, errors="raise")
        dt = parsed.to_pydatetime() if hasattr(parsed, "to_pydatetime") else parsed
        if dt.minute in (0, 30):
            return dt
    except Exception:
        return None

    return None


# --------------------------------------------------------------------------
# Etape 2 : rapprochement avec les logs internes (clics site)
# --------------------------------------------------------------------------
def reconcile(vocalcom_df: pd.DataFrame, jour_iso: str) -> pd.DataFrame:
    """
    Compare, pour chaque agent et chaque pause déclarée sur le site (pause_logs),
    la présence d'un évènement Vocalcom sur la même tranche horaire (fenêtre de 30 min).
    """
    site_logs = db.get_logs_for_date(jour_iso)
    if not site_logs:
        return pd.DataFrame()

    if vocalcom_df.empty:
        rows = []
        for log in site_logs:
            rows.append(_build_row(log, None, None))
        return pd.DataFrame(rows)

    vocalcom_df = vocalcom_df.copy()
    vocalcom_df["heure_constatee"] = pd.to_datetime(vocalcom_df["heure_constatee"])

    # Aligner la date pour la comparaison des objets datetime
    try:
        cible_date = datetime.strptime(jour_iso, "%Y-%m-%d").date()
    except Exception:
        cible_date = datetime.today().date()

    rows = []
    for log in site_logs:
        matricule = log["matricule"]
        try:
            heure_clic = db.parse_iso(log["heure_debut"]).replace(tzinfo=None)
            heure_clic = datetime.combine(cible_date, heure_clic.time())
        except Exception:
            continue

        agent_events = vocalcom_df[vocalcom_df["matricule"] == matricule].copy()
        
        # Filtre souple pour détecter le mot "pause" (ex: Pause_Besoin, Pause Dejeuner)
        agent_events = agent_events[
            agent_events["statut_constate"].str.contains("pause", case=False, na=False)
        ]

        if agent_events.empty:
            rows.append(_build_row(log, None, None))
            continue

        # Forcer la même date sur les événements Vocalcom pour le calcul du delta
        agent_events["heure_constatee"] = agent_events["heure_constatee"].apply(
            lambda d: datetime.combine(cible_date, d.time())
        )

        # Trouver la tranche horaire la plus proche du clic
        agent_events["delta_abs"] = (agent_events["heure_constatee"] - heure_clic).abs()
        closest = agent_events.sort_values("delta_abs").iloc[0]

        rows.append(_build_row(log, closest["heure_constatee"], closest["statut_constate"]))

    return pd.DataFrame(rows)


def _build_row(log, heure_vocalcom, statut_vocalcom):
    """
    Construit la ligne d'analyse en appliquant la tolérance de 30 minutes de Vocalcom.
    """
    heure_clic = db.parse_iso(log["heure_debut"]).replace(tzinfo=None)
    duree_declaree = log["duree_minutes"]

    if heure_vocalcom is not None:
        # Différence absolue en minutes entre l'heure exacte du clic et la tranche Vocalcom
        ecart_secondes = (heure_vocalcom - heure_clic).total_seconds()
        abs_ecart_min = abs(ecart_secondes) / 60.0
        
        # LOGIQUE REVISEE : Si l'écart est inférieur ou égal à 30 minutes, 
        # le clic de l'agent est considéré comme validé dans la tranche horaire.
        if abs_ecart_min <= 30.0:
            ecart_declenchement_min = 0.0
            deviation = "Conforme (Dans la tranche)"
        else:
            ecart_declenchement_min = round(ecart_secondes / 60.0, 1)
            deviation = "Hors Tranche / Non-adhérence"
    else:
        ecart_declenchement_min = None
        deviation = "Non vérifiable (aucun évènement Vocalcom correspondant)"

    return {
        "matricule": log["matricule"],
        "type_pause": log["type_pause"],
        "heure_clic_site": heure_clic,
        "duree_declaree_min": duree_declaree,
        "heure_constatee_vocalcom": heure_vocalcom,
        "statut_constate_vocalcom": statut_vocalcom,
        "ecart_declenchement_min": ecart_declenchement_min,
        "indicateur_conformite": deviation,
    }