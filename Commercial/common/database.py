# === Fichier: common/database.py ===
"""
Ce module contient la logique de connexion SQLite
et d'initialisation de l'ensemble des tables de l'application.
"""
import sqlite3


def creer_connexion(nom_fichier: str = "centre_local.db") -> sqlite3.Connection:
    """Crée et retourne une connexion à la base de données SQLite."""
    return sqlite3.connect(nom_fichier)


def creer_tables(conn: sqlite3.Connection) -> None:
    """Crée les tables de gestion du stock et des mouvements de stock."""
    with conn:
        # Table principale du stock disponible
        conn.execute('''
            CREATE TABLE IF NOT EXISTS stock (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                centre TEXT,
                produit_id TEXT,
                produit_nom TEXT,
                quantite_disponible INTEGER,
                prix_unitaire REAL,
                date_maj TEXT
            )
        ''')

        # Table d'historique des mouvements (entrées/sorties)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS mouvements_stock (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                centre TEXT,
                produit_id TEXT,
                produit_nom TEXT,
                type_mouvement TEXT,
                quantite INTEGER,
                prix_unitaire REAL,
                date_mouvement TEXT
            )
        ''')


def initialiser_ventes(conn: sqlite3.Connection) -> None:
    """Crée la table des ventes enregistrées par les centres."""
    with conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS ventes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                centre TEXT,
                produit_id TEXT,
                produit_nom TEXT,
                quantite INTEGER,
                prix REAL,
                date TEXT
            )
        ''')


def initialiser_toutes_les_tables(conn: sqlite3.Connection) -> None:
    """Initialise l'ensemble des tables de l'application en une seule opération."""
    creer_tables(conn)
    initialiser_ventes(conn)