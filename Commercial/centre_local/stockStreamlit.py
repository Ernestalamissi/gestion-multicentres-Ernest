"""Gestion du stock pour l'application Commercial avec synchronisation locale et centrale fiable.

À placer dans : Commercial/centre_local/stockStreamlit.py
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
import math
import requests
import pandas as pd
import streamlit as st

PROJECT_DIR = Path(__file__).resolve().parent.parent
DB_LOCAL = Path(os.environ.get("STOCK_DATABASE", PROJECT_DIR / "centre_local.db"))
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


def contexte_acces() -> tuple[str, str, bool]:
    """Retourne le rôle, le centre affecté et le statut administrateur."""
    role = str(st.session_state.get("role", "")).strip().lower()
    centre = str(st.session_state.get("centre_affecte", "")).strip().upper()
    return role, centre, role == "admin"


def verifier_perimetre_centre(centre: str) -> str:
    """Empêche un utilisateur non administrateur d'agir sur un autre centre."""
    _, centre_affecte, is_admin = contexte_acces()
    centre_demande = str(centre or "").strip().upper()
    if not centre_demande:
        raise PermissionError("Aucun centre valide n'a été fourni.")
    if is_admin:
        return centre_demande
    if not centre_affecte:
        raise PermissionError("Aucun centre n'est affecté à cet utilisateur.")
    if centre_demande != centre_affecte:
        raise PermissionError(
            f"Accès refusé : votre compte est limité au centre {centre_affecte}."
        )
    return centre_affecte


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

    # 2. Initialisation de la base centrale
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
    url_api = "http://127.0.0.1:8000/api/mouvements/batch"

    try:
        with connexion_locale() as db_l:
            non_synchro_mouvements = db_l.execute(
                "SELECT * FROM mouvements_stock WHERE synchronise = 0 ORDER BY id ASC"
            ).fetchall()

            if not non_synchro_mouvements:
                return

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

            response = requests.post(url_api, json=lot_payload, timeout=10.0)

            if response.status_code == 200:
                placeholders = ",".join("?" * len(ids_a_marquer))
                db_l.execute(
                    f"UPDATE mouvements_stock SET synchronise = 1 WHERE id IN ({placeholders})",
                    ids_a_marquer
                )
                db_l.commit()
    except (requests.exceptions.ConnectionError, Exception):
        pass


def approvisionner(centre: str, produit_id: str, produit_nom: str, quantite: int, prix_unitaire: float) -> None:
    centre = verifier_perimetre_centre(centre)
    produit_id = str(produit_id).strip().upper()
    produit_nom = str(produit_nom).strip()
    if not centre or not produit_id or not produit_nom:
        raise ValueError("Le centre, le code et le nom du produit sont obligatoires.")
    if quantite <= 0 or prix_unitaire < 0:
        raise ValueError("La quantité doit être positive et le prix ne peut pas être négatif.")

    date_op = maintenant()
    with connexion_locale() as db_l:
        art = produit(db_l, centre, produit_id)
        if art:
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

        art_maj = produit(db_l, centre, produit_id)
        if art_maj:
            ajouter_mouvement(db_l, art_maj, "entree", quantite, date_op, synchro_statut=0)
        db_l.commit()

    synchroniser_vers_central()


def sortir_produits_du_magasin(centre: str, produit_id: str, quantite: int) -> None:
    centre = verifier_perimetre_centre(centre)
    produit_id = str(produit_id).strip().upper()
    if quantite <= 0:
        raise ValueError("La quantité doit être supérieure à zéro.")

    date_op = maintenant()
    with connexion_locale() as db_l:
        art = produit(db_l, centre, produit_id)
        if not art:
            raise ValueError("Produit introuvable dans ce centre.")
        if quantite > art["quantite_disponible"]:
            raise ValueError(f"Stock insuffisant. Disponible : {art['quantite_disponible']}")

        restant = max(0, art["quantite_disponible"] - quantite)
        db_l.execute("""
            UPDATE stock SET quantite_disponible = ?, date_maj = ?, synchronise = 0
            WHERE centre = ? AND produit_id = ?
        """, (restant, date_op, centre, produit_id))

        ajouter_mouvement(db_l, art, "sortie", quantite, date_op, synchro_statut=0)
        db_l.commit()

    synchroniser_vers_central()


# --- FONCTIONS DE GESTION POUR STREAMLIT ---

def modifier_produit_stock(centre: str, produit_id: str, nouveau_nom: str, nouveau_prix: float) -> None:
    """Modifie un produit de manière sécurisée (compatible Streamlit)."""
    centre = verifier_perimetre_centre(centre)
    produit_id = str(produit_id).strip().upper()
    nouveau_nom = str(nouveau_nom).strip()
    if not centre or not produit_id or not nouveau_nom:
        raise ValueError("Le centre, l'ID et le nom du produit sont obligatoires.")
    if not math.isfinite(nouveau_prix) or nouveau_prix < 0:
        raise ValueError("Prix invalide.")

    date_op = maintenant()
    with connexion_locale() as db_l:
        art = produit(db_l, centre, produit_id)
        if not art:
            raise ValueError("Produit introuvable dans ce centre.")

        db_l.execute("""
            UPDATE stock SET produit_nom = ?, prix_unitaire = ?, date_maj = ?, synchronise = 0
            WHERE centre = ? AND produit_id = ?
        """, (nouveau_nom, nouveau_prix, date_op, centre, produit_id))

        db_l.execute("""
            INSERT INTO mouvements_stock
            (centre, produit_id, produit_nom, type_mouvement, quantite, prix_unitaire, date_mouvement, synchronise)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        """, (centre, produit_id, nouveau_nom, "modification_produit", 0, nouveau_prix, date_op))
        db_l.commit()

    synchroniser_vers_central()


def supprimer_produit_stock(centre: str, produit_id: str, quantite: int) -> None:
    """Retire une quantité du stock / correction (compatible Streamlit)."""
    centre = verifier_perimetre_centre(centre)
    produit_id = str(produit_id).strip().upper()
    if quantite <= 0:
        raise ValueError("La quantité à retirer doit être supérieure à zéro.")

    date_op = maintenant()
    with connexion_locale() as db_l:
        art = produit(db_l, centre, produit_id)
        if not art:
            raise ValueError("Produit introuvable dans ce centre.")
        if quantite > art["quantite_disponible"]:
            raise ValueError(f"Stock insuffisant. Disponible : {art['quantite_disponible']}")

        restant = max(0, art["quantite_disponible"] - quantite)
        db_l.execute("""
            UPDATE stock SET quantite_disponible = ?, date_maj = ?, synchronise = 0
            WHERE centre = ? AND produit_id = ?
        """, (restant, date_op, centre, produit_id))

        ajouter_mouvement(db_l, art, "correction_sortie", quantite, date_op, synchro_statut=0)
        db_l.commit()

    synchroniser_vers_central()


def afficher_historique_mouvements(centre_filtre: str | None = None, produit_filtre: str | None = None) -> pd.DataFrame:
    """Retourne uniquement l'historique autorisé au rôle connecté."""
    _, centre_affecte, is_admin = contexte_acces()
    if not is_admin:
        if not centre_affecte:
            return pd.DataFrame()
        centre_filtre = centre_affecte
    elif centre_filtre:
        centre_filtre = str(centre_filtre).strip().upper()
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
    return df


# --- INTERFACES GRAPHIQUES STREAMLIT (Formulaires Web) ---
def menu_console_stock():
    """Formulaire d'approvisionnement sécurisé selon le rôle avec débogage."""
    st.subheader("📦 Approvisionnement du Stock")

    # Récupération stricte du périmètre de l'utilisateur connecté
    centre_restreint = st.session_state.get("centre_affecte", "")
    is_admin = not centre_restreint or centre_restreint.lower() in ["tous", "admin", "global"]

    # --- DÉBOGAGE VISUEL TEMPORAIRE ---
    with st.form("form_appro_web"):
        col1, col2 = st.columns(2)
        with col1:
            if is_admin:
                centre_saisi = st.text_input("Nom du centre").strip().title()
            else:
                # On utilise un selectbox ou un affichage texte strict pour forcer l'affichage
                st.text_input("Nom du centre (Verrouillé)", value=centre_restreint, disabled=True)
                centre_saisi = centre_restreint

            code = st.text_input("Produit ID").strip().upper()
            nom_produit = st.text_input("Nom complet du produit").strip().title()

        with col2:
            quantite = st.number_input("Quantité à ajouter", min_value=1, step=1, value=1)
            prix = st.number_input("Prix unitaire", min_value=0.0, step=100.0, value=0.0)

        submitted = st.form_submit_button("Valider l'approvisionnement", use_container_width=True)

        if submitted:
            # BLINDAGE ABSOLU INCONDITIONNEL
            if not is_admin:
                centre_final = centre_restreint
            else:
                centre_final = centre_saisi

            if not centre_final or not code or not nom_produit:
                st.error("❌ Veuillez remplir tous les champs obligatoires (Centre, ID, Nom).")
            else:
                try:
                    approvisionner(centre_final, code, nom_produit, int(quantite), float(prix))
                    st.success(
                        f"✅ {quantite} unités de « {nom_produit} » ajoutées avec succès pour le centre **{centre_final}** !"
                    )
                    st.rerun()
                except Exception as error:
                    st.error(f"❌ Erreur lors de l'approvisionnement : {error}")

def menu_console_ventes():
    """Formulaire Streamlit optimisé et sécurisé pour les sorties de stock (ventes)."""
    st.subheader("🛒 Sortie de stock pour Vente")

    # Récupération du périmètre de l'utilisateur connecté
    role, centre_restreint, is_admin = contexte_acces()
    if not role:
        st.error("❌ Utilisateur non authentifié.")
        return

    # Récupération dynamique des centres et produits existants en base
    try:
        with connexion_locale() as db:
            db.row_factory = sqlite3.Row if 'sqlite3' in globals() else lambda cursor, row: {col[0]: row[idx] for
                                                                                             idx, col in enumerate(
                    cursor.description)}

            if is_admin:
                centres_dispo = [row["centre"] for row in db.execute("SELECT DISTINCT centre FROM stock").fetchall() if
                                 row["centre"]]
            else:
                centres_dispo = [centre_restreint] if centre_restreint else []

            if is_admin:
                produits_bruts = db.execute(
                    "SELECT produit_id, produit_nom, quantite_disponible, centre FROM stock"
                ).fetchall()
            else:
                produits_bruts = db.execute(
                    """SELECT produit_id, produit_nom, quantite_disponible, centre
                       FROM stock WHERE UPPER(TRIM(centre)) = ?""",
                    (centre_restreint,),
                ).fetchall()
            produits_dispo = [dict(p) for p in produits_bruts]
    except Exception as e:
        centres_dispo = [centre_restreint] if centre_restreint else []
        produits_dispo = []

    if not produits_dispo:
        st.warning(
            "⚠️ Aucun produit n'est actuellement enregistré en stock. Veuillez d'abord faire un approvisionnement.")
        return

    with st.form("form_ventes_web"):
        col1, col2 = st.columns(2)
        with col1:
            if is_admin and len(centres_dispo) > 1:
                centre = st.selectbox("Sélectionner le centre", options=sorted(centres_dispo))
            else:
                centre = st.selectbox("Centre (Verrouillé)", options=centres_dispo, disabled=True)
                centre = centre_restreint if not is_admin else (centres_dispo[0] if centres_dispo else "")

            # Filtrer les produits du centre sélectionné en toute sécurité
            prods_centre = [p for p in produits_dispo if p.get("centre") == centre]

            dict_prods = {
                f"{p.get('produit_id')} - {p.get('produit_nom')} (Dispo: {p.get('quantite_disponible', 0)})": p.get(
                    'produit_id')
                for p in prods_centre
            }

            produit_label = st.selectbox(
                "Sélectionner le produit",
                options=list(dict_prods.keys()) if dict_prods else ["Aucun"]
            )
            code = dict_prods.get(produit_label, "") if produit_label != "Aucun" else ""

        with col2:
            quantite = st.number_input("Quantité à sortir", min_value=1, step=1, value=1)

        submitted = st.form_submit_button("Enregistrer la sortie / Vente", use_container_width=True)

        if submitted:
            centre_final = centre_restreint if not is_admin else centre
            if not centre_final or not code:
                st.error("❌ Veuillez sélectionner un centre et un produit valide.")
            else:
                try:
                    with connexion_locale() as db:
                        article = produit(db, centre_final, code)

                    if not article:
                        st.error("❌ Produit introuvable pour ce centre.")
                    elif quantite > article["quantite_disponible"]:
                        st.error(f"❌ Stock insuffisant ! Quantité disponible : {article.get('quantite_disponible', 0)}")
                    else:
                        sortir_produits_du_magasin(centre_final, code, int(quantite))
                        st.success(
                            f"✅ Sortie de {quantite} × « {article.get('produit_nom')} » validée avec succès ! Stock restant : {article.get('quantite_disponible', 0) - quantite}.")
                        st.rerun()  # Rafraîchit l'affichage pour actualiser les stocks instantanément
                except Exception as error:
                    st.error(f"❌ Erreur lors de la vente : {error}")


def verifier_integrite_stock() -> list[str]:
    """Vérifie l'intégrité et retourne les alertes sous forme de liste (compatible Streamlit)."""
    alertes = []
    for db_conn, nom in [(connexion_locale(), "Local"), (connexion_centrale(), "Central")]:
        if db_conn is None:
            continue
        try:
            doublons = db_conn.execute("""
                SELECT centre, produit_id, COUNT(*) AS nombre
                FROM stock GROUP BY centre, produit_id HAVING COUNT(*) > 1
            """).fetchall()
            if doublons:
                for ligne in doublons:
                    alertes.append(f"⚠️ Doublons sur {nom} - Centre {ligne['centre']} / produit {ligne['produit_id']} : {ligne['nombre']} lignes")
            else:
                alertes.append(f"✅ Aucun doublon sur {nom}.")
        finally:
            db_conn.close()
    return alertes


# Initialisation automatique au chargement du module
initialiser_base()
