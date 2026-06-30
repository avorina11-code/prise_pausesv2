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
from datetime import datetime
import database as db


# --------------------------------------------------------------------------
# Etape 1 : parsing robuste de la matrice Vocalcom
# --------------------------------------------------------------------------
def parse_vocalcom_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """
    Transforme un tableau croisé dynamique (agents en lignes, colonnes = tranches
    horaires / statuts) en un format long et normalisé :
        matricule | heure_constatee (datetime) | statut_constate

    Hypothèses :
    - La première colonne contient le matricule / nom de l'agent.
    - Les en-têtes de colonnes restantes sont des horodatages (formats variés).
    - Les cellules contiennent le statut de l'agent à cet instant T
      (ex: "Pause", "Production", "Pause Dejeuner", vide, etc.)
    """
    if df.empty:
        raise ValueError("Le fichier importé est vide.")

    agent_col = df.columns[0]
    time_cols = df.columns[1:]

    records = []
    for _, row in df.iterrows():
        matricule = str(row[agent_col]).strip()
        if not matricule or matricule.lower() == "nan":
            continue
        for col in time_cols:
            statut = row[col]
            if pd.isna(statut) or str(statut).strip() == "":
                continue
            heure_parsee = _safe_parse_datetime(col)
            if heure_parsee is None:
                # En-tête de colonne non interprétable comme horodatage : on l'ignore
                continue
            records.append({
                "matricule": matricule,
                "heure_constatee": heure_parsee,
                "statut_constate": str(statut).strip(),
            })

    return pd.DataFrame(records)


def _safe_parse_datetime(value):
    """
    Parsing robuste d'un horodatage hétérogène (12h/24h, ISO, AM/PM, '/' ou '-').
    Retourne un objet datetime ou None si impossible à interpréter.
    """
    try:
        # dayfirst=False car les exports anglo-saxons type Vocalcom utilisent souvent MM/DD,
        # mais pandas reste tolérant ; en cas d'échec on retente avec dayfirst=True.
        parsed = pd.to_datetime(value, errors="raise", dayfirst=False)
        return parsed.to_pydatetime() if hasattr(parsed, "to_pydatetime") else parsed
    except Exception:
        try:
            parsed = pd.to_datetime(value, errors="raise", dayfirst=True)
            return parsed.to_pydatetime() if hasattr(parsed, "to_pydatetime") else parsed
        except Exception:
            return None


# --------------------------------------------------------------------------
# Etape 2 : rapprochement avec les logs internes (clics site)
# --------------------------------------------------------------------------
def reconcile(vocalcom_df: pd.DataFrame, jour_iso: str) -> pd.DataFrame:
    """
    Compare, pour chaque agent et chaque pause déclarée sur le site (pause_logs),
    le premier évènement Vocalcom de type "pause" survenu à proximité de l'heure
    de clic, afin de calculer :
        - l'écart de déclenchement (minutes) : heure Vocalcom - heure clic site
        - l'écart de durée : durée réelle (Vocalcom) - durée déclarée (site)
        - un score / indicateur de conformité

    Retourne un DataFrame avec une ligne par pause déclarée sur le site.
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

    rows = []
    for log in site_logs:
        matricule = log["matricule"]
        try:
            # Le clic site est enregistré en heure Madagascar (timezone-aware) ; on le ramène
            # en naïf pour le comparer aux horodatages Vocalcom (qui n'ont pas de fuseau,
            # mais représentent déjà l'heure locale Madagascar telle qu'exportée).
            heure_clic = db.parse_iso(log["heure_debut"]).replace(tzinfo=None)
        except Exception:
            continue

        agent_events = vocalcom_df[vocalcom_df["matricule"] == matricule].copy()
        # On ne garde que les statuts ressemblant à une "pause"
        agent_events = agent_events[
            agent_events["statut_constate"].str.contains("pause", case=False, na=False)
        ]

        if agent_events.empty:
            rows.append(_build_row(log, None, None))
            continue

        # On choisit l'évènement Vocalcom le plus proche temporellement de l'heure de clic
        agent_events["delta_abs"] = (agent_events["heure_constatee"] - heure_clic).abs()
        closest = agent_events.sort_values("delta_abs").iloc[0]

        rows.append(_build_row(log, closest["heure_constatee"], closest["statut_constate"]))

    return pd.DataFrame(rows)


def _build_row(log, heure_vocalcom, statut_vocalcom):
    heure_clic = db.parse_iso(log["heure_debut"]).replace(tzinfo=None)
    duree_declaree = log["duree_minutes"]

    if heure_vocalcom is not None:
        ecart_declenchement_min = round((heure_vocalcom - heure_clic).total_seconds() / 60.0, 1)
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
    """
    Classe la déviation comportementale en fonction de l'écart de déclenchement.
    - Aucune correspondance Vocalcom trouvée -> "Non vérifiable"
    - Écart <= 1 min -> "Conforme"
    - 1 < écart <= 3 min -> "A surveiller"
    - écart > 3 min -> "Fraude au statut / Non-adhérence"
    Un écart négatif (clic après le constat Vocalcom) est aussi suspect.
    """
    if ecart_min is None:
        return "Non vérifiable (aucun évènement Vocalcom correspondant)"
    abs_ecart = abs(ecart_min)
    if abs_ecart <= 1:
        return "Conforme"
    if abs_ecart <= 3:
        return "A surveiller"
    return "Fraude au statut / Non-adhérence"
