"""
reconciliation.py
------------------
Module d'analyse à froid & synchronicité — format Vocalcom Mvola AE.

Format d'entrée réel :
    Agent       07:00:00   07:30:00   08:00:00 … 15:30:00
    CN00751     Dispo      Dispo      Pause_Besoin …
    CN00996     Pause_Besoin  Dispo   Dispo …

Règles :
- Colonnes horaires en HH:MM:SS, toutes les 30 minutes (minutes = 0 ou 30 uniquement)
- Statuts reconnus comme "en pause" : tout statut contenant "Pause" (ex: Pause_Besoin)
- "Dispo" = agent disponible → ignoré dans le résultat long
- Tolérance de synchronisation : ±30 minutes (une tranche Vocalcom)
"""

import pandas as pd
from datetime import datetime, timedelta
import database as db

# Tolérance max pour rapprocher un clic site et un statut Vocalcom (en minutes)
TOLERANCE_SYNC_MIN = 30


# --------------------------------------------------------------------------
# Etape 1 : parsing de la matrice Vocalcom
# --------------------------------------------------------------------------
def parse_vocalcom_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """
    Transforme la matrice Vocalcom en format long normalisé.
    Retourne un DataFrame : matricule | heure_constatee (datetime) | statut_constate
    Seules les lignes en pause (statut != Dispo) sont conservées.
    """
    if df.empty:
        raise ValueError("Le fichier importé est vide.")

    agent_col = df.columns[0]   # colonne "Agent"
    time_cols = df.columns[1:]  # colonnes "07:00:00", "07:30:00" …

    records = []
    for col in time_cols:
        heure = _parse_hhmm(str(col))
        if heure is None:
            continue  # colonne non-horaire ou hors grille 30 min → ignorée

        for _, row in df.iterrows():
            matricule = str(row[agent_col]).strip()
            if not matricule or matricule.lower() in ("nan", "agent", ""):
                continue

            raw = row[col]
            if pd.isna(raw):
                continue
            statut = str(raw).strip()
            if not statut or statut.lower() in ("nan", "dispo", ""):
                continue  # on ne conserve que les statuts de pause

            records.append({
                "matricule": matricule,
                "heure_constatee": heure,
                "statut_constate": statut,          # ex: "Pause_Besoin"
                "statut_affiche": statut.replace("_", " "),  # ex: "Pause Besoin"
            })

    result = pd.DataFrame(records)
    if not result.empty:
        result = result.sort_values(["matricule", "heure_constatee"]).reset_index(drop=True)
    return result


def _parse_hhmm(value: str):
    """
    Parse un en-tête de colonne Vocalcom.

    Formats acceptés : "07:30:00" (HH:MM:SS), "07:30" (HH:MM), "07:30:00 AM/PM".
    Règle : minutes DOIVENT être 0 ou 30 (grille 30 min). Sinon → None (ignoré).
    Retourne un datetime naïf avec la date du jour (pour les comparaisons delta).
    """
    from datetime import date as dt_date
    s = str(value).strip()
    today = dt_date.today()

    for fmt in ("%H:%M:%S", "%H:%M", "%I:%M:%S %p", "%I:%M %p"):
        try:
            t = datetime.strptime(s, fmt)
            if t.minute not in (0, 30):
                return None   # hors grille 30 min
            return datetime.combine(today, t.time())
        except ValueError:
            continue

    # Fallback pandas (gère les formats régionaux)
    try:
        parsed = pd.to_datetime(s, dayfirst=False)
        if parsed.minute not in (0, 30):
            return None
        return datetime.combine(today, parsed.time())
    except Exception:
        return None


# --------------------------------------------------------------------------
# Etape 2 : rapprochement (reconciliation) avec les logs internes
# --------------------------------------------------------------------------
def reconcile(vocalcom_df: pd.DataFrame, jour_iso: str) -> pd.DataFrame:
    """
    Pour chaque pause déclarée sur le site (pause_logs du jour),
    cherche dans la matrice Vocalcom le statut "Pause_*" le plus proche
    temporellement, dans une fenêtre de ±30 minutes.

    Calcule :
      - écart de déclenchement : heure_tranche_Vocalcom − heure_clic_site
      - indicateur de conformité
    """
    site_logs = db.get_logs_for_date(jour_iso)
    if not site_logs:
        return pd.DataFrame()

    if vocalcom_df.empty:
        return pd.DataFrame([_build_row(log, None, None) for log in site_logs])

    vocalcom_df = vocalcom_df.copy()
    vocalcom_df["heure_constatee"] = pd.to_datetime(vocalcom_df["heure_constatee"])

    # Filtre : on ne garde que les statuts "Pause*" (insensible à la casse, tolère underscore)
    mask_pause = vocalcom_df["statut_constate"].str.upper().str.contains("PAUSE", na=False)
    vocalcom_pauses = vocalcom_df[mask_pause].copy()

    rows = []
    for log in site_logs:
        matricule = log["matricule"]
        try:
            heure_clic = db.parse_iso(log["heure_debut"]).replace(tzinfo=None)
        except Exception:
            continue

        agent_v = vocalcom_pauses[vocalcom_pauses["matricule"] == matricule].copy()
        if agent_v.empty:
            rows.append(_build_row(log, None, None))
            continue

        # Calcul du delta et application de la tolérance ±30 min (une tranche Vocalcom)
        agent_v = agent_v.copy()
        agent_v["delta_min"] = (
            (agent_v["heure_constatee"] - heure_clic)
            .dt.total_seconds() / 60.0
        )
        agent_v["delta_abs"] = agent_v["delta_min"].abs()

        # On ne retient que les évènements dans la fenêtre de tolérance
        dans_tolerance = agent_v[agent_v["delta_abs"] <= TOLERANCE_SYNC_MIN]

        if dans_tolerance.empty:
            # Hors tolérance : on signale quand même le plus proche
            closest = agent_v.sort_values("delta_abs").iloc[0]
            rows.append(_build_row(log, closest["heure_constatee"],
                                   closest["statut_affiche"], hors_tolerance=True))
        else:
            closest = dans_tolerance.sort_values("delta_abs").iloc[0]
            rows.append(_build_row(log, closest["heure_constatee"],
                                   closest["statut_affiche"], hors_tolerance=False))

    return pd.DataFrame(rows)


def _build_row(log, heure_vocalcom, statut_vocalcom, hors_tolerance=False):
    heure_clic = db.parse_iso(log["heure_debut"]).replace(tzinfo=None)

    if heure_vocalcom is not None:
        ecart_min = round(
            (heure_vocalcom - heure_clic).total_seconds() / 60.0, 1
        )
    else:
        ecart_min = None

    indicateur = _score_conformite(ecart_min, hors_tolerance)

    return {
        "matricule":                 log["matricule"],
        "type_pause":                log["type_pause"],
        "heure_clic_site":           heure_clic,
        "duree_declaree_min":        log["duree_minutes"],
        "heure_tranche_vocalcom":    heure_vocalcom,
        "statut_vocalcom":           statut_vocalcom,
        "ecart_declenchement_min":   ecart_min,
        "indicateur_conformite":     indicateur,
    }


def _score_conformite(ecart_min, hors_tolerance=False):
    """
    Scoring de conformité adapté à la granularité 30 min de Vocalcom.
      - Aucun match Vocalcom          → "Non vérifiable"
      - Hors tolérance (> 30 min)     → "Fraude au statut / Non-adhérence"
      - |écart| <= 15 min (≤ ½ tranche) → "Conforme"
      - 15 < |écart| <= 30 min         → "A surveiller"
    """
    if ecart_min is None:
        return "Non vérifiable (aucun statut Pause Vocalcom trouvé)"
    if hors_tolerance:
        return "Fraude au statut / Non-adhérence (hors tolérance 30 min)"
    abs_ecart = abs(ecart_min)
    if abs_ecart <= 15:
        return "Conforme"
    return "A surveiller"
