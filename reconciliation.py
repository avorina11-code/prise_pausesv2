"""
reconciliation.py
------------------
Module d'analyse à froid & synchronicité (Axe 2 - 20% du projet).

Permet :
- de parser une matrice Vocalcom (agents en lignes, tranches horaires/statuts en colonnes)
- de la transformer en évènements normalisés (matricule, heure, statut)
- de la rapprocher des logs internes SQLite (pause_logs) générés par les clics agents
- de calculer un score de conformité par agent (décalage de déclenchement + écart de durée)
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
                
            # Extraction robuste de l'heure de la colonne
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
    Parsing robuste adapté aux en-têtes de type 'HH:MM:SS' de Vocalcom.
    Associe l'heure de la tranche à la date du jour pour l'analyse à froid.
    """
    val_str = str(value).strip()
    
    # Si c'est déjà un objet de type heure ou datetime venant d'un Excel
    if isinstance(value, (datetime, time)):
        if isinstance(value, time):
            dt = datetime.combine(datetime.today(), value)
        else:
            dt = value
        if dt.minute in (0, 30):
            return dt
        return None

    # Test du format texte HH:MM:SS ou HH:MM (ex: "07:30:00")
    try:
        # On extrait juste les heures et minutes du texte
        parts = val_str.split(':')
        if len(parts) >= 2:
            h = int(parts[0])
            m = int(parts[1])
            if m in (0, 30):
                # On combine avec la date du jour actuel pour la réconciliation
                return datetime.combine(datetime.today(), time(h, m))
    except Exception:
        pass

    # Fallback standard si le format inclut une date complète
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
    le premier évènement Vocalcom de type "pause" survenu à proximité de l'heure
    de clic.
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

    # Ajustement crucial : s'assurer que les dates des deux côtés coïncident pour la comparaison
    try:
        cible_date = datetime.strptime(jour_iso, "%Y-%m-%d").date()
    except Exception:
        cible_date = datetime.today().date()

    rows = []
    for log in site_logs:
        matricule = log["matricule"]
        try:
            heure_clic = db.parse_iso(log["heure_debut"]).replace(tzinfo=None)
            # On force la date de l'analyse à froid pour s'aligner aux tranches horaires parées
            heure_clic = datetime.combine(cible_date, heure_clic.time())
        except Exception:
            continue

        agent_events = vocalcom_df[vocalcom_df["matricule"] == matricule].copy()
        
        # 🔥 CORRECTION : Recherche de "pause" tolérante aux formats comme "Pause_Besoin" ou "pause dejeuner"
        agent_events = agent_events[
            agent_events["statut_constate"].str.contains("pause", case=False, na=False)
        ]

        if agent_events.empty:
            rows.append(_build_row(log, None, None))
            continue

        # Aligner également la date des événements pour éviter des deltas énormes de 24h
        agent_events["heure_constatee"] = agent_events["heure_constatee"].apply(
            lambda d: datetime.combine(cible_date, d.time())
        )

        # Calcul du delta temporel le plus proche
        agent_events["delta_abs"] = (agent_events["heure_constatee"] - heure_clic).abs()
        closest = agent_events.sort_values("delta_abs").iloc[0]

        rows.append(_build_row(log, closest["heure_constatee"], closest["statut_constate"]))

    return pd.DataFrame(rows)


def _build_row(log, heure_vocalcom, statut_vocalcom):
    heure_clic = db.parse_iso(log["heure_debut"]).replace(tzinfo=None)
    duree_declaree = log["duree_minutes"]

    if heure_vocalcom is not None:
        # Comparaison basée uniquement sur l'heure de la journée pour éviter les écarts de date
        time_vocalcom = datetime.combine(datetime.today(), heure_vocalcom.time())
        time_clic = datetime.combine(datetime.today(), heure_clic.time())
        ecart_declenchement_min = round((time_vocalcom - time_clic).total_seconds() / 60.0, 1)
    else:
        ecart_declenchement_min = None

    deviation = _score_conformite(ecart_declenchement_min)

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


def _score_conformite(ecart_min):
    if ecart_min is None:
        return "Non vérifiable (aucun évènement Vocalcom correspondant)"
    abs_ecart = abs(ecart_min)
    if abs_ecart <= 15:  # Tolérance élargie à 15 min car Vocalcom capture par fenêtres de 30 min
        return "Conforme"
    if abs_ecart <= 30:
        return "A surveiller"
    return "Fraude au statut / Non-adhérence"