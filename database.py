"""
database.py
-----------
Couche d'accès aux données (SQLite) pour l'application Mvola AE - Pause Pilot.

Responsabilités :
- Initialisation automatique du schéma (tables users, pause_logs, config, vocalcom_import)
- Fonctions CRUD réutilisables pour les utilisateurs et les logs de pause
- Connexion SQLite thread-safe pour Streamlit (check_same_thread=False)

Toutes les fonctions sont volontairement simples (pas d'ORM) afin de rester
lisibles et faciles à auditer pour une équipe WFM non-développeuse.
"""

import sqlite3
import hashlib
from datetime import datetime, date, timezone, timedelta
from contextlib import contextmanager

DB_PATH = "mvola_ae.db"

# --------------------------------------------------------------------------
# Fuseau horaire Madagascar (UTC+3, pas de changement d'heure saisonnier)
# --------------------------------------------------------------------------
MADAGASCAR_TZ = timezone(timedelta(hours=3))


def now_mg() -> datetime:
    """Heure actuelle, fuseau Madagascar (Antananarivo, UTC+3)."""
    return datetime.now(MADAGASCAR_TZ)


def today_mg() -> date:
    """Date du jour, fuseau Madagascar."""
    return now_mg().date()


def parse_iso(value: str) -> datetime:
    """
    Parse un horodatage ISO stocké en base. Si la valeur est naïve (anciens
    enregistrements créés avant la bascule au fuseau Madagascar), on la
    considère comme étant déjà en heure Madagascar plutôt que UTC.
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MADAGASCAR_TZ)
    return dt.astimezone(MADAGASCAR_TZ)


# --------------------------------------------------------------------------
# Connexion
# --------------------------------------------------------------------------
def get_connection():
    """Retourne une connexion SQLite réutilisable dans le contexte Streamlit."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def get_cursor():
    """Context manager pratique : ouvre une connexion, commit/rollback, ferme proprement."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Initialisation du schéma
# --------------------------------------------------------------------------
def init_db():
    """
    Crée les tables nécessaires si elles n'existent pas encore.
    Appelée systématiquement au démarrage de app.py.
    """
    with get_cursor() as cur:
        # Table des utilisateurs (Agents + Admins)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                matricule TEXT UNIQUE NOT NULL,
                nom_complet TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('Admin', 'Agent')),
                actif INTEGER NOT NULL DEFAULT 1,
                date_creation TEXT NOT NULL
            )
        """)

        # Table des logs de pause (déclarations faites via le site)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pause_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                matricule TEXT NOT NULL,
                type_pause TEXT NOT NULL CHECK(
                    type_pause IN ('Dejeuner', 'Besoin', 'Personnelle', 'Autre')
                ),
                heure_debut TEXT NOT NULL,
                heure_fin TEXT,
                duree_minutes REAL,
                date_jour TEXT NOT NULL,
                statut TEXT NOT NULL DEFAULT 'En cours' CHECK(
                    statut IN ('En cours', 'Terminee')
                ),
                FOREIGN KEY (matricule) REFERENCES users(matricule)
            )
        """)

        # Table de configuration globale (effectif total, seuils, durées planifiées)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS config (
                cle TEXT PRIMARY KEY,
                valeur TEXT NOT NULL
            )
        """)

        # Table de stockage des imports Vocalcom (extraction à froid)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS vocalcom_import (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                matricule TEXT NOT NULL,
                statut_constate TEXT,
                heure_constatee TEXT NOT NULL,
                date_import TEXT NOT NULL,
                nom_fichier TEXT
            )
        """)

        # Valeurs de configuration par défaut (insérées une seule fois)
        defaults = {
            "effectif_total": "100",
            "seuil_simultaneite_pct": "15",
            "duree_dejeuner_min": "45",
            "max_occurrences_besoin": "3",
            "max_duree_cumulee_besoin": "15",
        }
        for cle, valeur in defaults.items():
            cur.execute(
                "INSERT OR IGNORE INTO config (cle, valeur) VALUES (?, ?)",
                (cle, valeur),
            )

        # Compte Admin par défaut si la table users est vide
        cur.execute("SELECT COUNT(*) AS n FROM users")
        if cur.fetchone()["n"] == 0:
            cur.execute(
                """INSERT INTO users (matricule, nom_complet, password_hash, role, actif, date_creation)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    "admin",
                    "Administrateur Vigie",
                    hash_password("admin123"),
                    "Admin",
                    1,
                    now_mg().isoformat(),
                ),
            )


# --------------------------------------------------------------------------
# Sécurité - mots de passe
# --------------------------------------------------------------------------
def hash_password(plain_password: str) -> str:
    """Hash simple SHA-256 + salage fixe (suffisant pour un outil interne WFM)."""
    salt = "mvola_ae_pause_pilot_salt"
    return hashlib.sha256((salt + plain_password).encode("utf-8")).hexdigest()


def verify_password(plain_password: str, password_hash: str) -> bool:
    return hash_password(plain_password) == password_hash


# --------------------------------------------------------------------------
# CRUD Utilisateurs
# --------------------------------------------------------------------------
def get_user(matricule: str):
    with get_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE matricule = ?", (matricule,))
        row = cur.fetchone()
        return dict(row) if row else None


def list_users():
    with get_cursor() as cur:
        cur.execute("SELECT * FROM users ORDER BY role, nom_complet")
        return [dict(r) for r in cur.fetchall()]


def add_user(matricule, nom_complet, plain_password, role):
    with get_cursor() as cur:
        cur.execute(
            """INSERT INTO users (matricule, nom_complet, password_hash, role, actif, date_creation)
               VALUES (?, ?, ?, ?, 1, ?)""",
            (matricule, nom_complet, hash_password(plain_password), role, now_mg().isoformat()),
        )


def update_user(matricule, nom_complet=None, role=None, actif=None, new_password=None):
    fields, values = [], []
    if nom_complet is not None:
        fields.append("nom_complet = ?")
        values.append(nom_complet)
    if role is not None:
        fields.append("role = ?")
        values.append(role)
    if actif is not None:
        fields.append("actif = ?")
        values.append(int(actif))
    if new_password:
        fields.append("password_hash = ?")
        values.append(hash_password(new_password))
    if not fields:
        return
    values.append(matricule)
    with get_cursor() as cur:
        cur.execute(f"UPDATE users SET {', '.join(fields)} WHERE matricule = ?", values)


def delete_user(matricule):
    with get_cursor() as cur:
        cur.execute("DELETE FROM users WHERE matricule = ?", (matricule,))


# --------------------------------------------------------------------------
# CRUD Config
# --------------------------------------------------------------------------
def get_config(cle: str, default=None):
    with get_cursor() as cur:
        cur.execute("SELECT valeur FROM config WHERE cle = ?", (cle,))
        row = cur.fetchone()
        return row["valeur"] if row else default


def set_config(cle: str, valeur: str):
    with get_cursor() as cur:
        cur.execute(
            "INSERT INTO config (cle, valeur) VALUES (?, ?) "
            "ON CONFLICT(cle) DO UPDATE SET valeur = excluded.valeur",
            (cle, str(valeur)),
        )


def get_all_config():
    with get_cursor() as cur:
        cur.execute("SELECT cle, valeur FROM config")
        return {r["cle"]: r["valeur"] for r in cur.fetchall()}


# --------------------------------------------------------------------------
# CRUD Pause Logs
# --------------------------------------------------------------------------
def start_pause(matricule, type_pause):
    now = now_mg()
    with get_cursor() as cur:
        cur.execute(
            """INSERT INTO pause_logs (matricule, type_pause, heure_debut, date_jour, statut)
               VALUES (?, ?, ?, ?, 'En cours')""",
            (matricule, type_pause, now.isoformat(), today_mg().isoformat()),
        )


def end_pause(matricule):
    """Clôture la pause en cours de l'agent et calcule la durée."""
    with get_cursor() as cur:
        cur.execute(
            """SELECT * FROM pause_logs
               WHERE matricule = ? AND statut = 'En cours'
               ORDER BY id DESC LIMIT 1""",
            (matricule,),
        )
        row = cur.fetchone()
        if not row:
            return None
        debut = parse_iso(row["heure_debut"])
        fin = now_mg()
        duree = (fin - debut).total_seconds() / 60.0
        cur.execute(
            """UPDATE pause_logs SET heure_fin = ?, duree_minutes = ?, statut = 'Terminee'
               WHERE id = ?""",
            (fin.isoformat(), round(duree, 2), row["id"]),
        )
        return round(duree, 2)


def get_active_pause(matricule):
    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM pause_logs WHERE matricule = ? AND statut = 'En cours' ORDER BY id DESC LIMIT 1",
            (matricule,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_today_logs(matricule):
    today = today_mg().isoformat()
    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM pause_logs WHERE matricule = ? AND date_jour = ? ORDER BY id",
            (matricule, today),
        )
        return [dict(r) for r in cur.fetchall()]


def get_all_active_pauses():
    """Liste de toutes les pauses en cours, tous agents confondus (pour la simultanéité)."""
    with get_cursor() as cur:
        cur.execute("SELECT * FROM pause_logs WHERE statut = 'En cours'")
        return [dict(r) for r in cur.fetchall()]


def get_logs_for_date(jour_iso: str):
    with get_cursor() as cur:
        cur.execute("SELECT * FROM pause_logs WHERE date_jour = ? ORDER BY matricule, id", (jour_iso,))
        return [dict(r) for r in cur.fetchall()]


def get_all_logs():
    with get_cursor() as cur:
        cur.execute("SELECT * FROM pause_logs ORDER BY date_jour DESC, id DESC")
        return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------
# Stockage import Vocalcom
# --------------------------------------------------------------------------
def save_vocalcom_rows(rows: list, nom_fichier: str):
    """rows: liste de dicts {matricule, statut_constate, heure_constatee}"""
    today = now_mg().isoformat()
    with get_cursor() as cur:
        for r in rows:
            cur.execute(
                """INSERT INTO vocalcom_import (matricule, statut_constate, heure_constatee, date_import, nom_fichier)
                   VALUES (?, ?, ?, ?, ?)""",
                (r["matricule"], r.get("statut_constate"), r["heure_constatee"], today, nom_fichier),
            )


def get_vocalcom_rows(nom_fichier=None):
    with get_cursor() as cur:
        if nom_fichier:
            cur.execute("SELECT * FROM vocalcom_import WHERE nom_fichier = ?", (nom_fichier,))
        else:
            cur.execute("SELECT * FROM vocalcom_import")
        return [dict(r) for r in cur.fetchall()]
