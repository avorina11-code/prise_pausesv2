# Mvola AE — Pause Pilot

Application Streamlit de régulation, traçabilité et audit des pauses agents
pour la campagne **Mvola AE**, conforme aux normes WFM / Vigie.

## 🚀 Installation

```bash
python -m venv venv
source venv/bin/activate        # Windows : venv\Scripts\activate
pip install -r requirements.txt
```

## ▶️ Lancement

```bash
streamlit run app.py
```

La base SQLite (`mvola_ae.db`) et toutes les tables sont créées automatiquement
au premier lancement. Un compte administrateur par défaut est généré :

- **Matricule** : `admin`
- **Mot de passe** : `admin123`

⚠️ Changez ce mot de passe immédiatement depuis l'onglet *Gestion des Accès*.

## 🗂️ Architecture du projet

```
mvola_ae/
├── app.py                # UI Streamlit, routage RBAC, orchestration
├── database.py           # Couche SQLite (schéma, CRUD users/pause_logs/config)
├── business_rules.py     # Moteur de règles (quotas, simultanéité, fractionnement)
├── reconciliation.py     # Module d'analyse à froid Vocalcom (parsing + rapprochement)
├── requirements.txt
└── README.md
```

## 📋 Fonctionnalités

### Axe 1 — Régulation temps réel
- Déclenchement / retour de pause en un clic (Déjeuner, Besoin, Personnelle, Autre)
- Quotas stricts pause Besoin : 3 occurrences max, 15 min cumulées max / jour
- Alerte visuelle (orange/rouge) en cas de dépassement
- Règle de simultanéité : blocage du bouton si >15% de l'effectif est en pause
  (exception légale pour la pause Besoin/Urgente : jamais bloquée, alerte Vigie à la place)
- Dashboard de supervision temps réel pour les admins
- Détection automatique du fractionnement abusif (micro-pauses < 3 min)

### Axe 2 — Analyse à froid & synchronicité
- Import d'une extraction Vocalcom (matrice agents x tranches horaires)
- Parsing robuste des horodatages (formats régionaux, ISO, AM/PM, slashs/tirets)
- Rapprochement avec les logs internes (heure de clic site vs heure constatée Vocalcom)
- Score / indicateur de conformité par agent (Conforme / A surveiller / Fraude au statut)
- Export CSV des rapports

## 🔐 Gestion des accès (RBAC)

- **Agent** : interface épurée, déclenchement de pause, compteurs du jour
- **Admin** : seul rôle pouvant créer/modifier/supprimer des utilisateurs,
  configurer les seuils, consulter le dashboard temps réel et le module d'analyse à froid

## 🔧 Configuration

Les seuils (effectif total, % de simultanéité, durée déjeuner, quotas pause Besoin)
sont modifiables depuis l'onglet *Configuration* de l'espace Admin, sans toucher au code.

## 🗃️ Versioning GitHub

```bash
git init
git add .
git commit -m "Initial commit — Mvola AE Pause Pilot"
git branch -M main
git remote add origin <URL_DU_DEPOT>
git push -u origin main
```

Pensez à ajouter un `.gitignore` excluant `mvola_ae.db` et `venv/` si vous ne
souhaitez pas versionner les données de production.
