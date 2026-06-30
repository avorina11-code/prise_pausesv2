"""
business_rules.py
------------------
Moteur de règles métier WFM pour Mvola AE - Pause Pilot.

Deux familles de règles :
1. Règles d'occurrence / durée par type de pause (réinitialisées chaque jour)
2. Règle de simultanéité globale (max 15% de l'effectif en pause au même instant)

Ce module ne touche jamais directement la base : il reçoit des données déjà
chargées (via database.py) et retourne des verdicts exploitables par l'UI.
"""

from datetime import date
import database as db


# --------------------------------------------------------------------------
# Règles par type de pause
# --------------------------------------------------------------------------
def evaluate_pause_quota(matricule: str, type_pause: str) -> dict:
    """
    Calcule, pour un agent et un type de pause donnés, l'état de consommation
    du quota journalier (occurrences + durée cumulée).

    Retourne un dict :
        {
            "autorise": bool,
            "message": str,
            "niveau": "ok" | "warning" | "danger",
            "occurrences": int,
            "max_occurrences": int | None,
            "duree_cumulee": float,
            "max_duree": float | None,
        }
    """
    logs_du_jour = [l for l in db.get_today_logs(matricule) if l["type_pause"] == type_pause]
    occurrences = len(logs_du_jour)
    duree_cumulee = sum(l["duree_minutes"] or 0 for l in logs_du_jour)

    if type_pause == "Dejeuner":
        max_occurrences = 1
        max_duree = float(db.get_config("duree_dejeuner_min", 45))
        if occurrences >= max_occurrences:
            return _verdict(False, "danger", occurrences, max_occurrences, duree_cumulee, max_duree,
                             "Pause déjeuner déjà prise aujourd'hui.")
        return _verdict(True, "ok", occurrences, max_occurrences, duree_cumulee, max_duree,
                         "Pause déjeuner disponible.")

    if type_pause == "Besoin":
        max_occurrences = int(db.get_config("max_occurrences_besoin", 3))
        max_duree = float(db.get_config("max_duree_cumulee_besoin", 15))

        # Règle d'or légale : on ne bloque JAMAIS le départ pour une pause Besoin/Urgente,
        # même au-delà des seuils. On se contente d'alerter visuellement.
        if occurrences >= max_occurrences or duree_cumulee >= max_duree:
            return _verdict(True, "danger", occurrences, max_occurrences, duree_cumulee, max_duree,
                             "⚠️ Seuil dépassé (occurrences ou durée) — départ autorisé, "
                             "alerte envoyée à la Vigie.")
        if occurrences == max_occurrences - 1 or duree_cumulee >= max_duree * 0.7:
            return _verdict(True, "warning", occurrences, max_occurrences, duree_cumulee, max_duree,
                             "Vous approchez du quota de pause Besoin.")
        return _verdict(True, "ok", occurrences, max_occurrences, duree_cumulee, max_duree,
                         "Pause Besoin disponible.")

    # Personnelle / Autre : soumis à validation, pas de quota strict codé en dur ici,
    # mais on remonte l'historique pour information à l'agent et à la Vigie.
    return _verdict(True, "ok", occurrences, None, duree_cumulee, None,
                     "Pause soumise à validation / quota du jour.")


def _verdict(autorise, niveau, occurrences, max_occurrences, duree_cumulee, max_duree, message):
    return {
        "autorise": autorise,
        "message": message,
        "niveau": niveau,
        "occurrences": occurrences,
        "max_occurrences": max_occurrences,
        "duree_cumulee": round(duree_cumulee, 2),
        "max_duree": max_duree,
    }


# --------------------------------------------------------------------------
# Règle de simultanéité
# --------------------------------------------------------------------------
def evaluate_simultaneity(type_pause: str) -> dict:
    """
    Vérifie si déclencher une nouvelle pause dépasserait le seuil de 15%
    de l'effectif total en pause simultanée.

    Pour 'Besoin' : on ne bloque jamais (exception légale), on alerte seulement.
    Pour les autres types : le bouton doit être désactivé si le seuil est déjà atteint.
    """
    effectif_total = int(db.get_config("effectif_total", 100))
    seuil_pct = float(db.get_config("seuil_simultaneite_pct", 15))
    seuil_absolu = max(1, int(effectif_total * seuil_pct / 100))

    actives = db.get_all_active_pauses()
    nb_en_pause = len(actives)
    taux_actuel_pct = round((nb_en_pause / effectif_total) * 100, 1) if effectif_total else 0

    seuil_franchi = nb_en_pause >= seuil_absolu

    if type_pause == "Besoin":
        bloque = False  # Jamais bloqué pour une pause Besoin/Urgente
    else:
        bloque = seuil_franchi

    return {
        "bloque": bloque,
        "alerte_vigie": seuil_franchi,
        "nb_en_pause": nb_en_pause,
        "seuil_absolu": seuil_absolu,
        "effectif_total": effectif_total,
        "taux_actuel_pct": taux_actuel_pct,
        "seuil_pct": seuil_pct,
        "message": (
            f"Seuil de simultanéité franchi : {nb_en_pause}/{effectif_total} agents en pause "
            f"({taux_actuel_pct}% > {seuil_pct}%)."
            if seuil_franchi else
            f"Simultanéité OK : {nb_en_pause}/{effectif_total} agents en pause ({taux_actuel_pct}%)."
        ),
    }


# --------------------------------------------------------------------------
# Détection de fractionnement abusif (micro-pauses répétées)
# --------------------------------------------------------------------------
def detect_fractionnement(matricule: str, seuil_minutes: float = 3.0) -> list:
    """
    Retourne la liste des pauses du jour considérées comme des micro-pauses
    (durée < seuil_minutes) pour un agent donné — indicateur de fractionnement abusif.
    """
    logs = db.get_today_logs(matricule)
    return [l for l in logs if l["statut"] == "Terminee" and (l["duree_minutes"] or 0) < seuil_minutes]
