"""Gestion du stock pour l'application Commercial avec synchronisation locale et centrale fiable.

À placer dans : Commercial/centre_local/stock.py
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
import math
import requests
import pandas as pd
PROJECT_DIR = Path(__file__).resolve().parent.parent
DB_LOCAL = Path(os.environ.get("STOCK_DATABASE", PROJECT_DIR / "centre_local.db"))
#DB_CENTRAL = PROJECT_DIR / "centre_central.db"
DB_CENTRAL = PROJECT_DIR / "centre_central" / "centre_central.db"

def connexion_locale() -> sqlite3.Connection:
    """Retourne une connexion vers la base de données locale."""
    db = sqlite3.connect(DB_LOCAL, timeout=10.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL;")
    return db


def connexion_centrale() -> sqlite3.Connection | None:
    """Retourne une connexion vers la base centrale si elle existe et est accessible."""
    if not DB_CENTRAL.exists():
        return None
    try:
        db = sqlite3.connect(DB_CENTRAL, timeout=10.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL;")
        return db
    except sqlite3.Error:
        return None


def maintenant() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def initialiser_base() -> None:
    """Initialise le schéma sur la base locale et s'assure que la centrale est initialisée."""
    # 1. Initialisation de la base locale
    db_l = connexion_locale()
    try:
        with db_l:
            db_l.execute("""
                CREATE TABLE IF NOT EXISTS stock (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    centre TEXT NOT NULL,
                    produit_id TEXT NOT NULL,
                    produit_nom TEXT NOT NULL,
                    quantite_disponible INTEGER NOT NULL DEFAULT 0,
                    prix_unitaire REAL NOT NULL DEFAULT 0,
                    date_maj TEXT,
                    synchronise INTEGER NOT NULL DEFAULT 0
                )
            """)
            db_l.execute("""
                CREATE TABLE IF NOT EXISTS mouvements_stock (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    centre TEXT NOT NULL,
                    produit_id TEXT NOT NULL,
                    produit_nom TEXT NOT NULL,
                    type_mouvement TEXT NOT NULL,
                    quantite INTEGER NOT NULL,
                    prix_unitaire REAL NOT NULL,
                    date_mouvement TEXT NOT NULL,
                    synchronise INTEGER NOT NULL DEFAULT 0
                )
            """)
            for q in [
                "ALTER TABLE stock ADD COLUMN synchronise INTEGER DEFAULT 0",
                "ALTER TABLE mouvements_stock ADD COLUMN synchronise INTEGER DEFAULT 0",
                "ALTER TABLE stock ADD COLUMN date_maj TEXT"
            ]:
                try:
                    db_l.execute(q)
                except sqlite3.OperationalError:
                    pass
            db_l.execute("CREATE INDEX IF NOT EXISTS idx_stock_centre_produit ON stock(centre, produit_id)")
            db_l.execute("CREATE INDEX IF NOT EXISTS idx_mouvements_date ON mouvements_stock(date_mouvement)")
            db_l.execute("CREATE INDEX IF NOT EXISTS idx_mouvements_synchro ON mouvements_stock(synchronise)")
    finally:
        db_l.close()

    # 2. Initialisation de la base centrale (la crée si elle n'existe pas pour forcer le bon schéma)
    DB_CENTRAL.parent.mkdir(parents=True, exist_ok=True)
    db_c = sqlite3.connect(DB_CENTRAL, timeout=10.0)
    try:
        with db_c:
            db_c.execute("""
                CREATE TABLE IF NOT EXISTS stock (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    centre TEXT NOT NULL,
                    produit_id TEXT NOT NULL,
                    produit_nom TEXT NOT NULL,
                    quantite_disponible INTEGER NOT NULL DEFAULT 0,
                    prix_unitaire REAL NOT NULL DEFAULT 0,
                    date_maj TEXT,
                    synchronise INTEGER NOT NULL DEFAULT 0
                )
            """)
            db_c.execute("""
                CREATE TABLE IF NOT EXISTS mouvements_stock (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    centre TEXT NOT NULL,
                    produit_id TEXT NOT NULL,
                    produit_nom TEXT NOT NULL,
                    type_mouvement TEXT NOT NULL,
                    quantite INTEGER NOT NULL,
                    prix_unitaire REAL NOT NULL,
                    date_mouvement TEXT NOT NULL,
                    synchronise INTEGER NOT NULL DEFAULT 0
                )
            """)
            for q in [
                "ALTER TABLE stock ADD COLUMN synchronise INTEGER DEFAULT 0",
                "ALTER TABLE mouvements_stock ADD COLUMN synchronise INTEGER DEFAULT 0",
                "ALTER TABLE stock ADD COLUMN date_maj TEXT"
            ]:
                try:
                    db_c.execute(q)
                except sqlite3.OperationalError:
                    pass
            db_c.execute("CREATE INDEX IF NOT EXISTS idx_stock_centre_produit ON stock(centre, produit_id)")
            db_c.execute("CREATE INDEX IF NOT EXISTS idx_mouvements_date ON mouvements_stock(date_mouvement)")
            db_c.execute("CREATE INDEX IF NOT EXISTS idx_mouvements_synchro ON mouvements_stock(synchronise)")
    finally:
        db_c.close()

def produit(db: sqlite3.Connection, centre: str, produit_id: str) -> sqlite3.Row | None:
    rows = db.execute(
        "SELECT * FROM stock WHERE centre = ? AND produit_id = ?",
        (centre, produit_id),
    ).fetchall()
    if len(rows) > 1:
        raise ValueError(f"Doublon détecté pour le produit {produit_id} dans le centre {centre}.")
    return rows[0] if rows else None


def ajouter_mouvement(
    db: sqlite3.Connection,
    article: sqlite3.Row | dict,
    type_mouvement: str,
    quantite: int,
    date_mouv: str | None = None,
    synchro_statut: int = 0
) -> None:
    db.execute("""
        INSERT INTO mouvements_stock
        (centre, produit_id, produit_nom, type_mouvement, quantite, prix_unitaire, date_mouvement, synchronise)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        article["centre"], article["produit_id"], article["produit_nom"],
        type_mouvement, quantite, article["prix_unitaire"], date_mouv or maintenant(), synchro_statut
    ))


def synchroniser_vers_central() -> None:
    """Pousse toutes les opérations en attente (synchro=0) vers l'API du serveur central."""
    # URL de votre serveur FastAPI central (ajustez le port si nécessaire, ex: 8000)
    url_api = "http://127.0.0.1:8000/api/mouvements/batch"

    try:
        with connexion_locale() as db_l:
            non_synchro_mouvements = db_l.execute(
                "SELECT * FROM mouvements_stock WHERE synchronise = 0 ORDER BY id ASC"
            ).fetchall()

            if not non_synchro_mouvements:
                return

            # Préparation du lot (batch) au format attendu par le serveur FastAPI
            lot_payload = []
            ids_a_marquer = []

            for mouv in non_synchro_mouvements:
                lot_payload.append({
                    "centre": mouv["centre"],
                    "produit_id": mouv["produit_id"],
                    "produit_nom": mouv["produit_nom"],
                    "quantite": mouv["quantite"],
                    "prix_unitaire": mouv["prix_unitaire"],
                    "date": mouv["date_mouvement"],
                    "type_mouvement": mouv["type_mouvement"]
                })
                ids_a_marquer.append(mouv["id"])

            # Envoi de la requête HTTP POST vers le serveur central
            response = requests.post(url_api, json=lot_payload, timeout=10.0)

            if response.status_code == 200:
                resultat = response.json()
                # Si le serveur a bien reçu et traité les mouvements, on les marque synchronisés en local
                placeholders = ",".join("?" * len(ids_a_marquer))
                db_l.execute(
                    f"UPDATE mouvements_stock SET synchronise = 1 WHERE id IN ({placeholders})",
                    ids_a_marquer
                )
                db_l.commit()
                print(f"🔄 Synchronisation réussie : {len(ids_a_marquer)} mouvement(s) transmis au central.")
            else:
                print(f"⚠️ Erreur du serveur central (Code {response.status_code}) : {response.text}")

    except requests.exceptions.ConnectionError:
        print("⚠️ Serveur central injoignable (assurez-vous qu'il est démarré). Synchronisation reportée.")
    except Exception as e:
        print(f"⚠️ Erreur lors de la synchronisation vers le central : {e}")


def approvisionner(centre: str, produit_id: str, produit_nom: str, quantite: int, prix_unitaire: float) -> None:
    if not centre or not produit_id or not produit_nom:
        raise ValueError("Le centre, le code et le nom du produit sont obligatoires.")
    if quantite <= 0 or prix_unitaire < 0:
        raise ValueError("La quantité doit être positive et le prix ne peut pas être négatif.")

    date_op = maintenant()
    with connexion_locale() as db_l:
        article = produit(db_l, centre, produit_id)
        if article:
            db_l.execute("""
                UPDATE stock
                SET produit_nom = ?, quantite_disponible = quantite_disponible + ?,
                    prix_unitaire = ?, date_maj = ?, synchronise = 0
                WHERE centre = ? AND produit_id = ?
            """, (produit_nom, quantite, prix_unitaire, date_op, centre, produit_id))
        else:
            db_l.execute("""
                INSERT INTO stock (centre, produit_id, produit_nom, quantite_disponible, prix_unitaire, date_maj, synchronise)
                VALUES (?, ?, ?, ?, ?, ?, 0)
            """, (centre, produit_id, produit_nom, quantite, prix_unitaire, date_op))

        article = produit(db_l, centre, produit_id)
        if article:
            ajouter_mouvement(db_l, article,"entree", quantite, date_op, synchro_statut=0)

    print(f"✅ {quantite} unités de « {produit_nom} » ajoutées en local.")
    synchroniser_vers_central()

def sortir_produits_du_magasin(centre: str, produit_id: str, quantite: int) -> None:
    if quantite <= 0:
        raise ValueError("La quantité doit être supérieure à zéro.")

    date_op = maintenant()
    try:
        with connexion_locale() as db_l:
            article = produit(db_l, centre, produit_id)
            if not article:
                raise ValueError("Produit introuvable dans ce centre.")
            if quantite > article["quantite_disponible"]:
                raise ValueError(f"Stock insuffisant. Disponible : {article['quantite_disponible']}")

            restant = max(0, article["quantite_disponible"] - quantite)
            db_l.execute("""
                UPDATE stock SET quantite_disponible = ?, date_maj = ?, synchronise = 0
                WHERE centre = ? AND produit_id = ?
            """, (restant, date_op, centre, produit_id))

            ajouter_mouvement(db_l, article, "sortie", quantite, date_op, synchro_statut=0)
            db_l.commit()

        print(f"🛒 {quantite} × « {article['produit_nom']} » sorties. Stock restant : {restant}.")

        # Appel de la bonne fonction de synchronisation
        synchroniser_vers_central()

    except sqlite3.Error as error:
        print(f"❌ Erreur de base de données lors de la sortie : {error}")
    except ValueError as error:
        print(f"❌ {error}")
def sortir_produits_du_magasins(centre: str, produit_id: str, quantite: int) -> None:
    if quantite <= 0:
        raise ValueError("La quantité doit être supérieure à zéro.")

    date_op = maintenant()
    with connexion_locale() as db_l:
        article = produit(db_l, centre, produit_id)
        if not article:
            raise ValueError("Produit introuvable dans ce centre.")
        if quantite > article["quantite_disponible"]:
            raise ValueError(f"Stock insuffisant. Disponible : {article['quantite_disponible']}")

        restant = max(0, article["quantite_disponible"] - quantite)
        db_l.execute("""
            UPDATE stock SET quantite_disponible = ?, date_maj = ?, synchronise = 0
            WHERE centre = ? AND produit_id = ?
        """, (restant, date_op, centre, produit_id))
        ajouter_mouvement(db_l, article, "sortie", quantite, date_op, synchro_statut=0)

    print(f"🛒 {quantite} × « {article['produit_nom']} » sorties. Stock restant : {restant}.")
    synchroniser_vers_central()


def modifier_produit_stock() -> None:
    centre = input("Centre : ").strip().title()
    produit_id = input("Produit ID à modifier : ").strip().upper()

    try:
        with connexion_locale() as db_l:
            article = produit(db_l, centre, produit_id)
            if not article:
                print("❌ Produit introuvable.")
                return

            ancien_nom, ancien_prix = article["produit_nom"], article["prix_unitaire"]
            print(
                f"\n=== Produit trouvé : {ancien_nom} | Stock : {article['quantite_disponible']} | Prix : {ancien_prix} ===")

            nouveau_nom = input("Nouveau nom (Entrée pour conserver) : ").strip() or ancien_nom
            prix_saisi = input("Nouveau prix (Entrée pour conserver) : ").strip()
            nouveau_prix = float(prix_saisi) if prix_saisi else ancien_prix

            if not math.isfinite(nouveau_prix) or nouveau_prix < 0:
                raise ValueError("Prix invalide.")

            if nouveau_nom == ancien_nom and nouveau_prix == ancien_prix:
                print("ℹ️ Aucune modification effectuée.")
                return

            if input("Confirmer (o/n) : ").strip().lower() != "o":
                print("Opération annulée.")
                return

            date_op = maintenant()
            db_l.execute("""
                UPDATE stock SET produit_nom = ?, prix_unitaire = ?, date_maj = ?, synchronise = 0
                WHERE centre = ? AND produit_id = ?
            """, (nouveau_nom, nouveau_prix, date_op, centre, produit_id))

            db_l.execute("""
                INSERT INTO mouvements_stock
                (centre, produit_id, produit_nom, type_mouvement, quantite, prix_unitaire, date_mouvement, synchronise )
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """, (centre, produit_id, nouveau_nom, "modification_produit", 0, nouveau_prix, date_op))

        print("✅ Produit modifié en local.")
        synchroniser_vers_central()

    except (ValueError, sqlite3.Error) as error:
        print(f"❌ Erreur : {error}")


def supprimer_produit_stock() -> None:
    centre = input("Centre : ").strip().title()
    produit_id = input("Produit ID : ").strip().upper()
    try:
        with connexion_locale() as db_l:
            article = produit(db_l, centre, produit_id)
            if not article:
                print("❌ Produit introuvable.")
                return

            quantite = int(input(f"Quantité à retirer (Max {article['quantite_disponible']}) : "))
            if quantite <= 0 or quantite > article["quantite_disponible"]:
                raise ValueError("Quantité invalide.")

            if input("Confirmer le retrait ? (o/n) : ").strip().lower() != "o":
                print("Opération annulée.")
                return

            date_op = maintenant()
            restant = max(0, article["quantite_disponible"] - quantite)
            db_l.execute("""
                UPDATE stock SET quantite_disponible = ?, date_maj = ?, synchronise = 0
                WHERE centre = ? AND produit_id = ?
            """, (restant, date_op, centre, produit_id))

            ajouter_mouvement(db_l, article, "correction_sortie", quantite, date_op, synchro_statut=0)

        print("✅ Retrait effectué en local.")
        synchroniser_vers_central()
    except (ValueError, sqlite3.Error) as error:
        print(f"❌ Erreur : {error}")


def menu_console_stock() -> None:
    print("=== Approvisionnement du stock ===")
    while True:
        try:
            # 1. Saisie du centre
            while True:
                centre = input("\nNom du centre : ").strip().title()
                if centre:
                    break
                print("❌ Le nom du centre ne peut pas être vide.")

            # 2. Saisie du code produit
            while True:
                code = input("Produit ID : ").strip().upper()
                if code:
                    break
                print("❌ L'ID du produit ne peut pas être vide.")

            # 3. Recherche BDD
            with connexion_locale() as db:
                article = produit(db, centre, code)

            if article:
                nom = article["produit_nom"]
                prix_actuel = float(article["prix_unitaire"])
                print(
                    f"📌 Produit existant : {nom} | Stock actuel : {article['quantite_disponible']} | Prix actuel : {prix_actuel}")

                # Saisie optionnelle du prix
                while True:
                    raw_prix = input(f"Nouveau prix unitaire (Entrée pour conserver {prix_actuel}) : ").strip().replace(
                        ",", ".")
                    if not raw_prix:
                        prix = prix_actuel
                        break
                    try:
                        prix = float(raw_prix)
                        if prix >= 0:
                            break
                        print("❌ Le prix ne peut pas être négatif.")
                    except ValueError:
                        print("❌ Prix unitaire invalide.")
            else:
                # Saisie nouveau produit
                while True:
                    nom = input("Nouveau produit - Nom complet : ").strip().title()
                    if nom:
                        break
                    print("❌ Le nom du produit ne peut pas être vide.")

                while True:
                    raw_prix = input("Prix unitaire : ").strip().replace(",", ".")
                    try:
                        prix = float(raw_prix)
                        if prix >= 0:
                            break
                        print("❌ Le prix ne peut pas être négatif.")
                    except ValueError:
                        print("❌ Prix unitaire invalide (entrez un nombre).")

            # 4. Saisie de la quantité
            while True:
                raw_qte = input("Quantité à ajouter : ").strip()
                try:
                    quantite = int(raw_qte)
                    if quantite > 0:
                        break
                    print("❌ La quantité doit être supérieure à 0.")
                except ValueError:
                    print("❌ Quantité invalide (entrez un nombre entier).")

            # 5. Exécution & Synchronisation
            approvisionner(centre, code, nom, quantite, prix)

        except KeyboardInterrupt:
            print("\n🛑 Opération interrompue par l'utilisateur.")
            break
        except Exception as error:
            print(f"❌ Erreur lors de l'approvisionnement : {error}")

        if input("\nAjouter un autre produit ? (o/n) : ").strip().lower() != "o":
            break

def menu_console_ventes() -> None:
    print("=== Sortie des produits du stock pour la vente ===")
    while True:
        centre = input("Nom du centre : ").strip().title()
        try:
            code = input("Produit ID : ").strip().upper()
            with connexion_locale() as db:
                article = produit(db, centre, code)
            if not article:
                print("❌ Produit introuvable pour ce centre.")
                continue
            print(
                f"Produit : {article['produit_nom']} | Stock : {article['quantite_disponible']} | Prix : {article['prix_unitaire']}")
            quantite = int(input("Quantité à sortir : "))
            if input(f"Confirmer la sortie de {quantite} × {article['produit_nom']} ? (o/n) : ").strip().lower() == "o":
                sortir_produits_du_magasin(centre, code, quantite)
            else:
                print("Opération annulée.")
        except ValueError as error:
            print(f"❌ {error}")

        if input("Ajouter un autre produit ? (o/n) : ").strip().lower() != "o":
            break


def afficher_historique_mouvements(centre_filtre: str | None = None, produit_filtre: str | None = None) -> None:
    requete = "SELECT * FROM mouvements_stock WHERE 1=1"
    params: list[str] = []
    if centre_filtre:
        requete += " AND centre = ?"
        params.append(centre_filtre)
    if produit_filtre:
        requete += " AND produit_id = ?"
        params.append(produit_filtre)
    requete += " ORDER BY date_mouvement DESC, id DESC"

    with connexion_locale() as db:
        df = pd.read_sql_query(requete, db, params=params)

    if df.empty:
        print("ℹ️ Aucun mouvement trouvé en local.")
        return

    print("\n=== Historique des mouvements (Local) ===")
    print(df.to_string(index=False))


def verifier_integrite_stock() -> None:
    for db_conn, nom in [(connexion_locale(), "Local"), (connexion_centrale(), "Central")]:
        if db_conn is None:
            continue
        try:
            doublons = db_conn.execute("""
                SELECT centre, produit_id, COUNT(*) AS nombre
                FROM stock GROUP BY centre, produit_id HAVING COUNT(*) > 1
            """).fetchall()
            if doublons:
                print(f"⚠️ Doublons sur {nom} :")
                for ligne in doublons:
                    print(f"- Centre {ligne['centre']} / produit {ligne['produit_id']} : {ligne['nombre']} lignes")
            else:
                print(f"✅ Aucun doublon sur {nom}.")
        finally:
            db_conn.close()

initialiser_base()
