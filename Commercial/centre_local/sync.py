# === Fichier: centre_local/sync.py ===
import os
import io
import sys
import time
import sqlite3
import subprocess
import requests
import pandas as pd
import streamlit as st
from pathlib import Path
import plotly.express as px
from datetime import datetime
from typing import Tuple, List, Dict, Any

PROJECT_DIR = Path(__file__).resolve().parent.parent
DB_LOCAL = Path(os.environ.get("STOCK_DATABASE", PROJECT_DIR / "centre_local.db"))
DB_CENTRAL = PROJECT_DIR / "centre_central" / "centre_central.db"

def assurer_serveur_central_actif():
    """Démarre et s'assure que le serveur central FastAPI est en ligne avant de continuer."""
    url_docs = "http://localhost:8000/docs"

    # 1. On teste si le serveur répond déjà
    try:
        requests.get(url_docs, timeout=1)
        return  # Il est déjà actif, on quitte la fonction
    except requests.exceptions.RequestException:
        pass  # Il est hors ligne, on passe au démarrage

    # 2. S'il est hors ligne, on le lance
    try:
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "centre_central.serveur_central:app", "--port", "8000", "--log-level",
             "warning"],
            cwd=str(PROJECT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
    except Exception as e:
        print(f"❌ Erreur lors du lancement du processus Uvicorn : {e}")
        return

    # 3. On attend activement qu'il soit prêt (jusqu'à 5 secondes max)
    for _ in range(10):
        try:
            response = requests.get(url_docs, timeout=1)
            if response.status_code == 200:
                time.sleep(0.5)  # Petite pause de sécurité pour stabiliser
                return
        except requests.exceptions.RequestException:
            time.sleep(0.5)

    print("⚠️ Attention : Le serveur central met trop de temps à démarrer.")


# --- Connexion SQLite sécurisée multi-threads ---
def obtenir_connexion(db_path):
    conn = sqlite3.connect(db_path, timeout=15, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except sqlite3.OperationalError:
        pass
    return conn


def Obtenir_donnees_directes():
    """Lit directement la base de données sans utiliser le cache Streamlit."""
    db_cible = DB_CENTRAL if DB_CENTRAL.exists() else DB_LOCAL
    if not db_cible.exists():
        return pd.DataFrame()

    try:
        with obtenir_connexion(db_cible) as conn:
            query = """
                SELECT 
                    centre, 
                    produit_id, 
                    produit_nom, 
                    quantite, 
                    prix_unitaire AS prix, 
                    date_mouvement AS date
                FROM mouvements_stock
                WHERE LOWER(TRIM(type_mouvement)) IN ('sortie', 'vente', 'sorties', 'ventes')
                ORDER BY date_mouvement DESC
            """
            return pd.read_sql_query(query, conn)
    except Exception as e:
        st.error(f"Erreur de lecture DB centrale : {e}")
        return pd.DataFrame()


def obtenir_stock_par_centre(source="local") -> pd.DataFrame:
    """Récupère l'état du stock et prépare les colonnes pour l'affichage Streamlit."""
    db_cible = DB_CENTRAL if source == "central" else DB_LOCAL

    if not db_cible.exists():
        db_cible = DB_LOCAL

    if not db_cible.exists():
        return pd.DataFrame()

    try:
        with sqlite3.connect(db_cible) as conn:
            conn.row_factory = sqlite3.Row
            # 1. Tentative sur la table stock
            try:
                query_stock = """
                    SELECT 
                        centre,
                        produit_id,
                        produit_nom,
                        prix_unitaire,
                        quantite_disponible AS quantite
                    FROM stock 
                    ORDER BY centre, produit_nom
                """
                df = pd.read_sql_query(query_stock, conn)
                if not df.empty:
                    return df
            except Exception:
                pass

            # 2. Secours sur mouvements_stock si la table stock était vide ou absente
            try:
                query_mouvements = """
                    SELECT 
                        centre, 
                        produit_id, 
                        produit_nom, 
                        MAX(prix_unitaire) AS prix_unitaire,
                        MAX(0,SUM(
                            CASE 
                                WHEN LOWER(TRIM(type_mouvement)) IN ('entree', 'entrée', 'approvisionnement', 'entree_stock','correction_entree', 'retour_client', 'transfert_entree', 'ajustement_positif') THEN quantite 
                                WHEN LOWER(TRIM(type_mouvement)) IN ('sortie', 'vente', 'correction_sortie', 'suppression', 'retour_fournisseur', 'transfert_sortie', 'perte', 'casse', 'ajustement_negatif') THEN -quantite 
                                ELSE 0  
                            END
                        )) AS quantite
                    FROM mouvements_stock
                    GROUP BY centre, produit_id, produit_nom
                    HAVING quantite IS NOT NULL
                    ORDER BY centre, produit_nom
                    """
                df = pd.read_sql_query(query_mouvements, conn)
                if not df.empty:
                    return df
            except Exception:
                return pd.DataFrame()

    except Exception as e:
        print(f"⚠️ Erreur de connexion : {e}")
    return pd.DataFrame()


@st.cache_data(ttl=15)
def charger_donnees_ventes(_refresh_key=0):
    """Charge toutes les ventes consolidées depuis la base centrale."""
    return Obtenir_donnees_directes()


def synchroniser_tous_mouvements(api_url: str = 'http://localhost:8000/api/mouvements/batch') -> Tuple[bool, str]:
    """
    Extrait TOUS les mouvements non synchronisés et les envoie en lot au serveur central.
    """
    if not DB_LOCAL.exists():
        return False, "Base locale introuvable."

    assurer_serveur_central_actif()

    # --- Step 0 : Migration/Vérification du schéma ---
    try:
        with obtenir_connexion(DB_LOCAL) as conn:
            colonnes = [col[1] for col in conn.execute("PRAGMA table_info(mouvements_stock)").fetchall()]
            if 'synchronise' not in colonnes:
                conn.execute("ALTER TABLE mouvements_stock ADD COLUMN synchronise INTEGER DEFAULT 0")
                conn.commit()
    except Exception as e:
        return False, f"Erreur lors de la vérification du schéma BDD : {e}"

    payload: List[Dict[str, Any]] = []
    ids_mouvements: List[int] = []

    # --- Step 1 : Lecture sécurisée des enregistrements ---
    try:
        with obtenir_connexion(DB_LOCAL) as conn:
            conn.row_factory = sqlite3.Row

            mouvements = conn.execute("""
                SELECT 
                    id, centre, produit_id, produit_nom, 
                    quantite, prix_unitaire AS prix, date_mouvement AS date, type_mouvement 
                FROM mouvements_stock 
                WHERE synchronise = 0 OR synchronise IS NULL
                ORDER BY id ASC
            """).fetchall()

            if not mouvements:
                return True, "Toutes les opérations sont déjà synchronisées."

            for m in mouvements:
                if m["id"] is None:
                    continue

                try:
                    qte = int(m["quantite"])
                    prix = float(m["prix"])
                except (ValueError, TypeError):
                    continue

                payload.append({
                    "centre": str(m["centre"] or "").strip(),
                    "produit_id": str(m["produit_id"] or "").strip().upper(),
                    "produit_nom": str(m["produit_nom"] or "").strip(),
                    "quantite": qte,
                    "prix": prix,
                    "date": str(m["date"] or "").strip(),
                    "type_mouvement": str(m["type_mouvement"] or "").strip()
                })
                ids_mouvements.append(int(m["id"]))

    except Exception as e:
        return False, f"Erreur lors de la lecture des données locales : {e}"

    if not payload:
        return True, "Aucune donnée valide à synchroniser."

    # --- Step 2 : Transmission HTTP vers l'API Centrale ---
    response = None
    try:
        response = requests.post(api_url, json=payload, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        msg = response.text if response is not None and response.text else str(e)
        return False, f"Échec de la transmission au serveur central : {msg}"

    # --- Step 3 : Marquage local par lots ---
    try:
        with obtenir_connexion(DB_LOCAL) as conn:
            taille_lot = 500
            for i in range(0, len(ids_mouvements), taille_lot):
                lot_ids = ids_mouvements[i:i + taille_lot]
                placeholders = ','.join(['?'] * len(lot_ids))
                conn.execute(
                    f"UPDATE mouvements_stock SET synchronise = 1 WHERE id IN ({placeholders})",
                    lot_ids
                )
            conn.commit()

        return True, f"✅ {len(ids_mouvements)} opération(s) synchronisée(s) avec succès."

    except Exception as e:
        return False, f"⚠️ Données transmises au central, mais erreur de validation locale : {e}"


def archiver_jour():
    """Archive les ventes actuelles dans des fichiers Excel structurés par centre."""
    df = Obtenir_donnees_directes()
    if df.empty:
        return

    df['quantite'] = pd.to_numeric(df['quantite'], errors='coerce').fillna(0)
    df['prix'] = pd.to_numeric(df['prix'], errors='coerce').fillna(0.0)
    df['chiffre_affaire'] = df['quantite'] * df['prix']
    date_str = datetime.now().strftime('%Y-%m-%d')

    for centre in df['centre'].dropna().unique():
        df_centre = df[df['centre'] == centre].copy()
        recap_ventes = (
            df_centre.groupby(['produit_id', 'produit_nom'])['quantite']
            .sum()
            .reset_index()
            .rename(columns={'quantite': 'quantite_totale_vendue'})
        )

        dossier = PROJECT_DIR / "archives" / str(centre).replace(' ', '_')
        os.makedirs(dossier, exist_ok=True)
        fichier = dossier / f"historique_{date_str}.xlsx"

        with pd.ExcelWriter(fichier) as writer:
            df_centre.to_excel(writer, sheet_name='ventes', index=False)
            recap_ventes.to_excel(writer, sheet_name='recap_produits', index=False)

def verifier_admin() -> None:
    role = str(st.session_state.get("role", "")).strip().lower()

    if role != "admin":
        raise PermissionError(
            "Accès refusé : cette opération est réservée à l'administrateur."
        )

def reinitialiser_donnees(confirmer=False):
    """Archive puis vide les bases locales ET centrales."""
    verifier_admin()
    if not confirmer:
        return False

    archiver_jour()

    if DB_LOCAL.exists():
        try:
            with obtenir_connexion(DB_LOCAL) as conn:
                conn.execute("DELETE FROM mouvements_stock")
                conn.execute("DELETE FROM stock")
                conn.commit()
        except Exception:
            pass

    if DB_CENTRAL.exists():
        try:
            with obtenir_connexion(DB_CENTRAL) as conn:
                conn.execute("DELETE FROM mouvements_stock")
                conn.execute("DELETE FROM stock")
                conn.commit()
        except Exception:
            pass

    try:
        st.cache_data.clear()
    except Exception:
        pass
    return True


def afficher_resume_streamlit():
    st.subheader("🧾 Résumé et analyse des ventes (Tous les centres)")
    if "refresh_key" not in st.session_state:
        st.session_state.refresh_key = 0
    if "toast_msg" in st.session_state:
        st.toast(st.session_state.toast_msg, icon="✅")
        del st.session_state.toast_msg
    # --- Barre latérale : Maintenance ---
    st.sidebar.markdown("---")
    with st.sidebar.expander("⚠️ Zone de Danger / Maintenance"):
        st.warning("Archive puis réinitialise les bases locale et centrale.")
        confirmation = st.checkbox("Je confirme la suppression")
        if st.button("🗑️ Réinitialiser les données", disabled=not confirmation):
            if reinitialiser_donnees(confirmer=confirmation):
                st.success("Bases nettoyées !")
                st.rerun()
        if st.button("🧹 Nettoyer et harmoniser les centres (Retours)"):
            try:
                with obtenir_connexion(DB_LOCAL) as conn:
                    cursor = conn.cursor()
                    # Met à jour les anciens 'Centre Local' vers le centre 'A' par défaut
                    cursor.execute("""
                            UPDATE retours_clients 
                            SET centre = 'A' 
                            WHERE centre = 'Centre Local'
                                OR centre = 'Aucun centre disponible' 
                                OR centre IS NULL 
                                OR centre = ''
                        """)
                    nb_mises_a_jour = cursor.rowcount
                    conn.commit()

                st.success(f"✅ {nb_mises_a_jour} ancien(s) enregistrement(s) harmonisé(s) vers le centre **A** !")
                time.sleep(1)
                st.rerun()
            except Exception as e:
                st.error(f"Erreur lors de la maintenance : {e}")

        if st.button("🗑️ Purger l'historique des retours"):
            try:
                with obtenir_connexion(DB_LOCAL) as conn:
                    conn.execute("DELETE FROM retours_clients")
                    conn.commit()
                st.warning("⚠️ Table des retours entièrement purgée.")
                time.sleep(1)
                st.rerun()
            except Exception as e:
                st.error(f"Erreur : {e}")
    # Navigation par onglets
    tab_ventes, tab_stock, tab_prevision,tab_transfert, tab_po,tab_retours = st.tabs(["🧾 Historique des Ventes", "📦 Stock Restant par Centre","📊 Prévisions & Réappro","🔄 Transferts Inter-Centres","📦 Bons de Commande","🔄 Retours Clients"])

    # === ONGLET 1 : HISTORIQUE DES VENTES ===
    with tab_ventes:
        col_btn, col_txt = st.columns([1, 4])
        with col_btn:
            if st.button("🔄 Rafraîchir les données", use_container_width=True, key="btn_refresh_ventes"):
                succes, message = synchroniser_tous_mouvements()
                if not succes:
                    st.warning(message)
                else:
                    st.session_state.toast_msg = message
                    st.cache_data.clear()
                    st.session_state.refresh_key += 1
                    st.rerun()

        df = charger_donnees_ventes(_refresh_key=st.session_state.refresh_key)

        if df.empty:
            st.info("Aucune donnée de vente disponible dans la base centrale.")
        else:
            df['quantite'] = pd.to_numeric(df['quantite'], errors='coerce').fillna(0).astype(int)
            df['prix'] = pd.to_numeric(df['prix'], errors='coerce').fillna(0.0).astype(float)
            df['date_dt'] = pd.to_datetime(df['date'], format='mixed', errors='coerce').dt.date
            df['chiffre_affaire'] = df['quantite'] * df['prix']

            st.sidebar.header("Filtres Ventes")
            centres = ["Tous"] + sorted(df['centre'].dropna().astype(str).unique().tolist())
            centre_selectionne = st.sidebar.selectbox("Centre", options=centres)
            dates_valides = df['date_dt'].dropna()
            min_date = dates_valides.min() if not dates_valides.empty else datetime.now().date()
            max_date = datetime.now().date()
            plage_dates = st.sidebar.date_input("Plage de dates", value=(min_date, max_date))
            mot_cle = st.sidebar.text_input("Mot-clé (produit ou ID)").strip().lower()

            df_filtre = df.copy()
            if centre_selectionne != "Tous":
                df_filtre = df_filtre[df_filtre['centre'] == centre_selectionne]
            if isinstance(plage_dates, (tuple, list)) and len(plage_dates) == 2:
                start_date, end_date = plage_dates
                df_filtre = df_filtre[(df_filtre['date_dt'] >= start_date) & (df_filtre['date_dt'] <= end_date)]
            if mot_cle:
                df_filtre = df_filtre[
                    df_filtre['produit_nom'].astype(str).str.lower().str.contains(mot_cle) |
                    df_filtre['produit_id'].astype(str).str.lower().str.contains(mot_cle)
                    ]
            df_filtre = df_filtre.drop_duplicates()

            col_kpi1, col_kpi2, col_kpi3 = st.columns(3)
            ca_total = float(df_filtre['chiffre_affaire'].sum()) if not df_filtre.empty else 0.0
            qty_total = int(df_filtre['quantite'].sum()) if not df_filtre.empty else 0
            col_kpi1.metric("Chiffre d'Affaires Total", f"{ca_total:,.2f} FCFA".replace(',', ' '))
            col_kpi2.metric("Quantité Totale Vendue", f"{qty_total:,}".replace(',', ' '))
            col_kpi3.metric("Nombre de Ventes", f"{len(df_filtre):,}".replace(',', ' '))

            st.markdown("---")
            st.dataframe(
                df_filtre[['date', 'centre', 'produit_id', 'produit_nom', 'quantite', 'prix', 'chiffre_affaire']],
                column_config={
                    "date": st.column_config.TextColumn("Date/Heure"),
                    "centre": "Centre",
                    "produit_id": "ID Produit",
                    "produit_nom": "Nom Produit",
                    "quantite": st.column_config.NumberColumn("Quantité", format="%d"),
                    "prix": st.column_config.NumberColumn("Prix unitaire", format="%.2f FCFA"),
                    "chiffre_affaire": st.column_config.NumberColumn("Chiffre d'affaires", format="%.2f FCFA")
                },
                use_container_width=True,
                hide_index=True
            )

            col_dl1, col_dl2 = st.columns(2)
            with col_dl1:
                csv = df_filtre.to_csv(index=False).encode('utf-8')
                st.download_button("📥 Télécharger CSV", data=csv, file_name="ventes_consolidees.csv", mime="text/csv")
            with col_dl2:
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='openpyxl') as writer:
                    df_filtre.to_excel(writer, index=False, sheet_name='Ventes')
                st.download_button(
                    "📊 Télécharger Excel",
                    data=output.getvalue(),
                    file_name="ventes_consolidees.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True
                )

            if not df_filtre.empty:
                st.subheader("📈 Visualisation des Ventes")
                try:
                    df_qty = df_filtre.groupby(['produit_nom', 'centre'], as_index=False)['quantite'].sum()
                    df_ca = df_filtre.groupby(['produit_nom', 'centre'], as_index=False)['chiffre_affaire'].sum()
                    col1, col2 = st.columns(2)
                    with col1:
                        fig_qty = px.bar(
                            df_qty, x="produit_nom", y="quantite", color="centre",
                            title="Quantités vendues par produit",
                            labels={"produit_nom": "Produit", "quantite": "Quantité", "centre": "Centre"}
                        )
                        st.plotly_chart(fig_qty, use_container_width=True)
                    with col2:
                        fig_ca = px.bar(
                            df_ca, x="produit_nom", y="chiffre_affaire", color="centre",
                            title="Chiffre d'affaires par produit",
                            labels={"produit_nom": "Produit", "chiffre_affaire": "FCFA", "centre": "Centre"}
                        )
                        st.plotly_chart(fig_ca, use_container_width=True)
                except Exception as e:
                    st.error(f"Erreur lors du rendu des graphiques : {e}")

    # === ONGLET 2 : STOCK RESTANT & ALERTES ===
    with tab_stock:
        col_st_title, col_st_btn = st.columns([3, 1])
        with col_st_title:
            st.subheader("📦 Niveaux de Stock Disponibles & Alertes")
        with col_st_btn:
            if st.button("🔄 Synchroniser & Rafraîchir", key="btn_sync_stock", use_container_width=True):
                try:
                    succes, message = synchroniser_tous_mouvements()
                    if succes:
                        st.toast(f"✅ {message}")
                    else:
                        st.warning(f"⚠️ {message}")
                except Exception as e:
                    st.error(f"Erreur lors de la synchronisation : {e}")

                st.cache_data.clear()
                st.rerun()

        df_stock = obtenir_stock_par_centre(source="central")

        if df_stock.empty:
            st.info("Aucune donnée de stock disponible.")
        else:
            def classifier_stock(qte):
                if qte <= 0:
                    return "🔴 Rupture"
                elif qte <= 5:
                    return "🟠 Critique"
                elif qte <= 15:
                    return "🟡 Faible"
                else:
                    return "🟢 Confortable"

            df_stock['quantite'] = pd.to_numeric(df_stock['quantite'], errors='coerce').fillna(0).astype(int)
            df_stock['statut'] = df_stock['quantite'].apply(classifier_stock)

            st.sidebar.markdown("---")
            st.sidebar.header("Filtres Stock")
            centres_stock = ["Tous"] + sorted(df_stock['centre'].dropna().astype(str).unique().tolist())
            centre_f = st.sidebar.selectbox("Centre (Stock)", options=centres_stock, key="select_stock_centre")

            filtre_statut = st.sidebar.selectbox(
                "Filtrer par Alerte",
                options=["Tous", "🔴 Rupture", "🟠 Critique", "🟡 Faible", "🟢 Confortable"],
                key="select_alerte"
            )

            df_stock_filtre = df_stock.copy()
            if centre_f != "Tous":
                df_stock_filtre = df_stock_filtre[df_stock_filtre['centre'] == centre_f]
            if filtre_statut != "Tous":
                df_stock_filtre = df_stock_filtre[df_stock_filtre['statut'] == filtre_statut]

            col_s1, col_s2, col_s3 = st.columns(3)
            col_s1.metric("Références Suivies", len(df_stock_filtre))
            col_s2.metric("Quantité Totale", int(df_stock_filtre['quantite'].sum()))

            nb_alertes = len(df_stock[df_stock['quantite'] <= 5])
            col_s3.metric("Produits en Alerte (≤ 5)", nb_alertes, delta=-nb_alertes if nb_alertes > 0 else 0,
                          delta_color="inverse")

            st.dataframe(
                df_stock_filtre[['centre', 'produit_id', 'produit_nom', 'quantite', 'statut']],
                column_config={
                    "centre": "Centre",
                    "produit_id": "ID Produit",
                    "produit_nom": "Nom Produit",
                    "quantite": st.column_config.NumberColumn("Quantité Restante", format="%d"),
                    "statut": "État du Stock"
                },
                use_container_width=True,
                hide_index=True
            )

            fig_stock = px.bar(
                df_stock_filtre,
                x="produit_nom",
                y="quantite",
                color="centre",
                title="Stock restant par produit et par centre",
                labels={"produit_nom": "Produit", "quantite": "Stock restant", "centre": "Centre"}
            )
            st.plotly_chart(fig_stock, use_container_width=True)

    # === ONGLET 3 : PRÉVISIONS & RÉAPPROVISIONNEMENT ===
    with tab_prevision:
        st.subheader("📊 Prévisions de Rupture & Suggestions de Réapprovisionnement")
        st.markdown(
            "Ce module analyse la vitesse de vente récente de chaque produit par centre pour estimer "
            "le nombre de jours d'autonomie restants et recommander les quantités à réapprovisionner."
        )

        df_stock_prev = obtenir_stock_par_centre(source="central")
        df_ventes_prev = charger_donnees_ventes(_refresh_key=st.session_state.refresh_key)

        if df_stock_prev.empty or df_ventes_prev.empty:
            st.info("Données insuffisantes (stock ou historique de ventes requis) pour calculer les prévisions.")
        else:
            # Paramètre modifiable par l'utilisateur dans la sidebar
            st.sidebar.markdown("---")
            st.sidebar.header("Paramètres Réappro")
            objectif_jours = st.sidebar.slider("Couler un stock pour (jours)", min_value=7, max_value=60, value=30,step=5)

            # Nettoyage des ventes
            df_v = df_ventes_prev.copy()
            df_v['quantite'] = pd.to_numeric(df_v['quantite'], errors='coerce').fillna(0)
            df_v['date_dt'] = pd.to_datetime(df_v['date'], format='mixed', errors='coerce')

            # Calcul de la période couverte par l'historique des ventes pour estimer la vitesse journalière
            date_min_vente = df_v['date_dt'].min()
            date_max_vente = df_v['date_dt'].max()
            nb_jours_historique = max(1, (date_max_vente - date_min_vente).days + 1)
            # Si l'historique est très court (< 7 jours), on normalise sur 7 jours pour éviter des vitesses aberrantes
            periode_ref = max(nb_jours_historique, 7)

            # Total vendu par produit et par centre sur la période
            df_v_gb = df_v.groupby(['centre', 'produit_id', 'produit_nom'], as_index=False)['quantite'].sum()
            df_v_gb['vente_journaliere'] = df_v_gb['quantite'] / periode_ref

            # Fusion avec le stock actuel
            df_stock_prev['quantite'] = pd.to_numeric(df_stock_prev['quantite'], errors='coerce').fillna(0)
            df_prev_merge = pd.merge(
                df_stock_prev,
                df_v_gb[['centre', 'produit_id', 'vente_journaliere']],
                on=['centre', 'produit_id'],
                how='left'
            ).fillna({'vente_journaliere': 0.0})

            # Calculs prévisionnels
            # Autonomie en jours = Stock actuel / Vente journalière
            def calculer_autonomie(row):
                v_j = row['vente_journaliere']
                if v_j <= 0:
                    return 999  # Infini / Pas de vente récente
                return round(row['quantite'] / v_j, 1)

            df_prev_merge['jours_autonomie'] = df_prev_merge.apply(calculer_autonomie, axis=1)

            # Quantité recommandée à commander = (Vente journalière * Objectif jours) - Stock actuel
            def calculer_reappro(row):
                besoin_total = row['vente_journaliere'] * objectif_jours
                manque = besoin_total - row['quantite']
                return max(0, round(manque))

            df_prev_merge['reappro_suggere'] = df_prev_merge.apply(calculer_reappro, axis=1)

            # Statut de prévision
            def statut_prevision(jours):
                if jours <= 3:
                    data = "🔴 Rupture imminente (≤ 3j)"
                elif jours <= 10:
                    data = "🟠 Attention (≤ 10j)"
                elif jours <= 30:
                    data = "🟡 Confort moyen"
                else:
                    data = "🟢 Stock sain (> 30j)"
                return data

            df_prev_merge['statut_prev'] = df_prev_merge['jours_autonomie'].apply(statut_prevision)

            # Filtre par centre dans l'onglet
            centres_prev = ["Tous"] + sorted(df_prev_merge['centre'].dropna().astype(str).unique().tolist())
            centre_prev_f = st.selectbox("Filtrer les prévisions par centre", options=centres_prev,key="select_prev_centre")

            df_prev_filtre = df_prev_merge.copy()
            if centre_prev_f != "Tous":
                df_prev_filtre = df_prev_filtre[df_prev_filtre['centre'] == centre_prev_f]

            # Affichage KPIs globaux de prévision
            col_p1, col_p2, col_p3 = st.columns(3)
            nb_urgents = len(df_prev_filtre[df_prev_filtre['jours_autonomie'] <= 3])
            col_p1.metric("Références en Danger (≤ 3 jours)", nb_urgents, delta=-nb_urgents if nb_urgents > 0 else 0,delta_color="inverse")
            col_p2.metric("Période Cible de Couverture", f"{objectif_jours} jours")
            col_p3.metric("Total Unités Suggérées en Réappro", int(df_prev_filtre['reappro_suggere'].sum()))

            # Tableau des recommandations
            st.dataframe(
                df_prev_filtre[
                    ['centre', 'produit_id', 'produit_nom', 'quantite', 'vente_journaliere', 'jours_autonomie',
                     'reappro_suggere', 'statut_prev']],
                column_config={
                    "centre": "Centre",
                    "produit_id": "ID Produit",
                    "produit_nom": "Nom Produit",
                    "quantite": st.column_config.NumberColumn("Stock Actuel", format="%d"),
                    "vente_journaliere": st.column_config.NumberColumn("Vente / jour (moy)", format="%.2f"),
                    "jours_autonomie": st.column_config.NumberColumn("Autonomie (jours)", format="%.1f"),
                    "reappro_suggere": st.column_config.NumberColumn(f"À Commander ({objectif_jours}j)", format="%d"),
                    "statut_prev": "Diagnostic Prévisionnel"
                },
                use_container_width=True,
                hide_index=True
            )

            # Graphique des quantités à commander par produit
            fig_reappro = px.bar(
                df_prev_filtre[df_prev_filtre['reappro_suggere'] > 0],
                x="produit_nom",
                y="reappro_suggere",
                color="centre",
                title=f"Quantités suggérées à réapprovisionner pour couvrir les {objectif_jours} prochains jours",
                labels={"produit_nom": "Produit", "reappro_suggere": "Quantité à commander", "centre": "Centre"}
            )
            st.plotly_chart(fig_reappro, use_container_width=True)

    # === ONGLET 4 : TRANSFERTS INTER-CENTRES ===
    with tab_transfert:
        st.subheader("🔄 Gestion des Transferts Inter-Centres (Logistique Réseau)")
        st.markdown(
            "Répartissez intelligemment vos stocks en déplaçant des marchandises d'un centre excédentaire "
            "vers un centre en tension, sans impacter votre chiffre d'affaires."
        )

        df_stock_trans = obtenir_stock_par_centre(source="central")

        if df_stock_trans.empty:
            st.info("Aucun stock disponible pour effectuer des transferts.")
        else:
            # --- MODULE DE SUGGESTIONS AUTOMATIQUES DE RÉÉQUILIBRAGE ---
            with st.expander("💡 Suggestions automatiques de rééquilibrage (Basé sur vos stocks)", expanded=True):
                suggestions = []
                produits_uniques = df_stock_trans['produit_id'].unique()

                for prod_id in produits_uniques:
                    df_prod = df_stock_trans[df_stock_trans['produit_id'] == prod_id]
                    if len(df_prod) >= 2:
                        prod_name = df_prod['produit_nom'].iloc[0]
                        min_row = df_prod.loc[df_prod['quantite'].idxmin()]
                        max_row = df_prod.loc[df_prod['quantite'].idxmax()]

                        # Seuil assoupli pour détecter facilement un déséquilibre entre deux centres
                        if min_row['quantite'] < max_row['quantite']:
                            qte_suggeree = min(int((max_row['quantite'] - min_row['quantite']) // 2),
                                               int(max_row['quantite']))
                            if qte_suggeree > 0 and max_row['centre'] != min_row['centre']:
                                suggestions.append({
                                    "produit": prod_name,
                                    "prod_id": prod_id,
                                    "source": max_row['centre'],
                                    "dest": min_row['centre'],
                                    "qte": qte_suggeree,
                                    "prix": max_row.get('prix_unitaire', 0.0)
                                })

                if suggestions:
                    st.success(f"🔍 {len(suggestions)} opportunité(s) de rééquilibrage détectée(s) dans le réseau :")
                    for i, sug in enumerate(suggestions):
                        col_s1, col_s2 = st.columns([4, 1])
                        with col_s1:
                            st.markdown(
                                f"- Transférer jusqu'à **{sug['qte']}** unité(s) de *{sug['produit']}* "
                                f"du centre **{sug['source']}** vers **{sug['dest']}**"
                            )
                        with col_s2:
                            if st.button("Appliquer", key=f"sug_trans_{i}"):
                                st.session_state['trans_source'] = sug['source']
                                st.session_state['trans_dest'] = sug['dest']
                                st.rerun()
                else:
                    st.info("✅ Aucun déséquilibre détecté pour le moment. Vos stocks sont bien répartis.")

            st.divider()
            # --- FIN DU MODULE DE SUGGESTIONS ---

            # Liste unique des centres disponibles
            centres_dispos = sorted(df_stock_trans['centre'].dropna().astype(str).unique().tolist())

            if len(centres_dispos) < 2:
                st.warning(
                    "Il faut au moins 2 centres enregistrés dans le système pour réaliser des transferts inter-centres.")
            else:
                col_t1, col_t2 = st.columns(2)

                with col_t1:
                    centre_source = st.selectbox("Centre Source (Départ)", options=centres_dispos, key="trans_source")
                with col_t2:
                    destinations_possibles = [c for c in centres_dispos if c != centre_source]

                    current_dest = st.session_state.get('trans_dest')
                    if current_dest not in destinations_possibles and destinations_possibles:
                        current_dest = destinations_possibles[0]

                    centre_dest = st.selectbox("Centre Destination (Arrivée)", options=destinations_possibles,key="trans_dest")

                # Filtrer les produits disponibles dans le centre source ayant un stock > 0
                df_produits_source = df_stock_trans[
                    (df_stock_trans['centre'] == centre_source) & (df_stock_trans['quantite'] > 0)]

                if df_produits_source.empty:
                    st.error(f"Le centre **{centre_source}** ne possède aucun produit en stock à transférer.")
                else:
                    col_prix_nom = 'prix_unitaire' if 'prix_unitaire' in df_produits_source.columns else (
                        'prix' if 'prix' in df_produits_source.columns else None)

                    dict_produits = {}
                    for _, row in df_produits_source.iterrows():
                        p_val = 0.0
                        if col_prix_nom and pd.notnull(row[col_prix_nom]):
                            try:
                                p_val = float(row[col_prix_nom])
                            except (ValueError, TypeError):
                                p_val = 0.0

                        label = f"{row['produit_nom']} (ID: {row['produit_id']} - Stock dispo: {int(row['quantite'])})"
                        dict_produits[label] = {
                            "id": row['produit_id'],
                            "nom": row['produit_nom'],
                            "stock_max": int(row['quantite']),
                            "prix": p_val
                        }

                    produit_selectionne_label = st.selectbox("Sélectionner le produit à transférer",
                                                             options=list(dict_produits.keys()))
                    infos_prod = dict_produits[produit_selectionne_label]

                    quantite_a_transférer = st.number_input(
                        "Quantité à transférer",
                        min_value=1,
                        max_value=infos_prod["stock_max"],
                        value=min(1, infos_prod["stock_max"]),
                        step=1
                    )

                    st.markdown(
                        f"**Résumé du transfert :** Déplacement de **{quantite_a_transférer} unité(s)** de *{infos_prod['nom']}* de **{centre_source}** vers **{centre_dest}**.")

                    if st.button("🚀 Valider et Exécuter le Transfert", type="primary"):
                        date_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

                        try:
                            with obtenir_connexion(DB_LOCAL) as conn:
                                # Insertion sortie source
                                conn.execute("""
                                    INSERT INTO mouvements_stock (centre, produit_id, produit_nom, quantite, prix_unitaire, date_mouvement, type_mouvement, synchronise)
                                    VALUES (?, ?, ?, ?, ?, ?, 'transfert_sortie', 0)
                                """, (centre_source, infos_prod['id'], infos_prod['nom'], quantite_a_transférer,
                                      infos_prod['prix'], date_str))

                                # Insertion entrée destination
                                conn.execute("""
                                    INSERT INTO mouvements_stock (centre, produit_id, produit_nom, quantite, prix_unitaire, date_mouvement, type_mouvement, synchronise)
                                    VALUES (?, ?, ?, ?, ?, ?, 'transfert_entree', 0)
                                """, (centre_dest, infos_prod['id'], infos_prod['nom'], quantite_a_transférer,
                                      infos_prod['prix'], date_str))

                                conn.commit()

                            st.success(
                                f"✅ Transfert enregistré avec succès ! Lancement de la synchronisation vers le central...")

                            succes_sync, msg_sync = synchroniser_tous_mouvements()
                            if succes_sync:
                                st.toast(msg_sync)
                            else:
                                st.warning(f"Attention à la synchro : {msg_sync}")

                            st.cache_data.clear()
                            time.sleep(1)
                            st.rerun()

                        except Exception as e:
                            st.error(f"Erreur lors de l'enregistrement du transfert : {e}")

            # --- NOUVEAU : HISTORIQUE DES TRANSFERTS RÉALISÉS ---
            st.divider()
            st.subheader("📜 Historique des Transferts Réalisés")

            try:
                with obtenir_connexion(DB_LOCAL) as conn:
                    df_historique = pd.read_sql("""
                        SELECT date_mouvement AS Date, centre AS Centre, produit_id AS [ID Produit], 
                               produit_nom AS [Nom Produit], quantite AS Quantité, type_mouvement AS [Type de Mouvement]
                        FROM mouvements_stock
                        WHERE type_mouvement IN ('transfert_sortie', 'transfert_entree')
                        ORDER BY date_mouvement DESC
                    """, conn)

                if df_historique.empty:
                    st.info("Aucun transfert n'a encore été enregistré dans l'historique.")
                else:
                    st.dataframe(df_historique, use_container_width=True)
            except Exception as e:
                st.error(f"Impossible de charger l'historique des transferts : {e}")

    # === ONGLET 5 : BONS DE COMMANDE FOURNISSEURS ===
    with tab_po:
        st.subheader("📦 Gestion des Bons de Commande Fournisseurs (Procurement)")
        st.markdown(
            "Automatisez vos achats externes dès qu'un produit atteint un niveau critique "
            "nécessitant un réapprovisionnement auprès de vos fournisseurs."
        )

        # 1. S'assurer que la table existe dans la base locale avant toute requête
        try:
            with obtenir_connexion(DB_LOCAL) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS bons_commande (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        fournisseur TEXT,
                        produit_id TEXT,
                        produit_nom TEXT,
                        quantite INTEGER,
                        statut TEXT DEFAULT 'En attente',
                        date_commande TEXT
                    )
                """)
                conn.commit()
        except Exception as e:
            st.error(f"Erreur lors de l'initialisation de la table bons_commande : {e}")

        # Formulaire de création d'un Bon de Commande
        with st.form("form_po_externe"):
            col_po1, col_po2, col_po3 = st.columns(3)

            with col_po1:
                fournisseur = st.text_input("Nom du Fournisseur", value="Global Distribution Inc.")
            with col_po2:
                prod_id_po = st.text_input("ID Produit (ex: P001)", value="P001")
            with col_po3:
                qte_po = st.number_input("Quantité à commander", min_value=1, value=50, step=1)

            submitted_po = st.form_submit_button("📝 Générer le Bon de Commande", type="primary")

            if submitted_po:
                date_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                try:
                    with obtenir_connexion(DB_LOCAL) as conn:
                        conn.execute("""
                            INSERT INTO bons_commande (fournisseur, produit_id, produit_nom, quantite, statut, date_commande)
                            VALUES (?, ?, ?, ?, 'En attente', ?)
                        """, (fournisseur, prod_id_po, "Produit ID " + prod_id_po, qte_po, date_str))
                        conn.commit()

                    st.success(f"✅ Bon de commande de {qte_po} unité(s) créé avec succès pour **{fournisseur}** !")
                    st.cache_data.clear()
                    time.sleep(1)
                    st.rerun()

                except Exception as e:
                    st.error(f"Erreur lors de la création du bon de commande : {e}")

        st.divider()

        # Tableau de suivi des bons de commande actifs
        st.markdown("### 📋 Suivi des Bons de Commande en Cours")
        try:
            with obtenir_connexion(DB_LOCAL) as conn:
                df_po = pd.read_sql("""
                    SELECT id AS [ID PO], date_commande AS [Date], fournisseur AS [Fournisseur], 
                           produit_id AS [ID Produit], produit_nom AS [Nom Produit], 
                           quantite AS [Quantité], statut AS [Statut]
                    FROM bons_commande 
                    ORDER BY date_commande DESC
                """, conn)

            if df_po.empty:
                st.info("Aucun bon de commande en cours pour le moment.")
            else:
                st.dataframe(df_po, use_container_width=True)

                # --- SECTION INTERACTIVE DE MISE À JOUR DU STATUT ---
                st.markdown("### ⚙️ Mettre à jour le statut d'un Bon de Commande")

                # Filtrer uniquement les commandes "En attente" pour les actions
                df_en_attente = df_po[df_po["Statut"] == "En attente"]

                if df_en_attente.empty:
                    st.info("Tous les bons de commande ont déjà été traités (aucun en attente).")
                else:
                    col_action1, col_action2, col_action3 = st.columns([2, 1, 1])

                    with col_action1:
                        po_selectionne = st.selectbox(
                            "Sélectionner un Bon de Commande (par ID)",
                            options=df_en_attente["ID PO"].tolist()
                        )

                    with col_action2:
                        st.text("")  # Espacement visuel
                        st.text("")
                        btn_valider = st.button("✅ Marquer Reçu", type="primary", use_container_width=True)

                    with col_action3:
                        st.text("")
                        st.text("")
                        btn_annuler = st.button("❌ Annuler PO", use_container_width=True)

                    if btn_valider:
                        try:
                            with obtenir_connexion(DB_LOCAL) as conn:
                                # 1. Mettre à jour le statut du bon de commande
                                conn.execute("""
                                    UPDATE bons_commande 
                                    SET statut = 'Reçu' 
                                    WHERE id = ?
                                """, (int(po_selectionne),))

                                # (Optionnel - si vous souhaitez aussi injecter dans mouvements_stock automatiquement)
                                # Récupérer les infos du PO pour l'injecter dans le stock local
                                cursor = conn.cursor()
                                cursor.execute(
                                    "SELECT produit_id, produit_nom, quantite FROM bons_commande WHERE id = ?",
                                    (int(po_selectionne),))
                                row_po = cursor.fetchone()

                                if row_po:
                                    p_id, p_nom, p_qte = row_po
                                    conn.execute("""
                                        INSERT INTO mouvements_stock (centre, produit_id, produit_nom, type_mouvement, quantite, date_mouvement)
                                        VALUES (?, ?, ?, 'entree', ?, ?)
                                    """, ("Centre Local", p_id, p_nom, p_qte,
                                          datetime.now().strftime('%Y-%m-%d %H:%M:%S')))

                                conn.commit()

                            st.success(
                                f"🎉 Bon de commande #{po_selectionne} marqué comme **Reçu** ! Le stock a été mis à jour.")
                            time.sleep(1)
                            st.rerun()
                        except Exception as e:
                            st.error(f"Erreur lors de la mise à jour : {e}")

                    if btn_annuler:
                        try:
                            with obtenir_connexion(DB_LOCAL) as conn:
                                conn.execute("""
                                    UPDATE bons_commande 
                                    SET statut = 'Annulé' 
                                    WHERE id = ?
                                """, (int(po_selectionne),))
                                conn.commit()
                            st.warning(f"Le bon de commande #{po_selectionne} a été **annulé**.")
                            time.sleep(1)
                            st.rerun()
                        except Exception as e:
                            st.error(f"Erreur lors de l'annulation : {e}")

        except Exception as e:
            st.error(f"Impossible de charger les bons de commande : {e}")

    # === ONGLET 6 : GESTION DES RETOURS CLIENTS (VERSION ULTIME 100%) ===

    # 1. S'assurer que la table des retours existe dans la base locale
    try:
        with obtenir_connexion(DB_LOCAL) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS retours_clients (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vente_id TEXT,
                    produit_id TEXT,
                    produit_nom TEXT,
                    quantite INTEGER,
                    centre TEXT,
                    date_vente TEXT,
                    date_retour TEXT,
                    motif TEXT,
                    statut TEXT DEFAULT 'Accepté'
                )
            """)
            conn.commit()
    except Exception as e:
        st.error(f"Erreur lors de l'initialisation de la table retours_clients : {e}")

    # Règle : Délai maximum de retour fixé à 30 jours
    DELAI_MAX_RETOURS_JOURS = 30

    st.subheader("🔄 Gestion des Retours Clients")
    st.markdown(f"Encadrez et automatisez les retours d'articles payés dans une fenêtre stricte de **{DELAI_MAX_RETOURS_JOURS} jours**.")
    # 🌟 1. Le champ produit est mis en dehors du formulaire pour être 100% réactif
    produit_id_retour = st.text_input("ID du Produit concerné (ex: P001)", value="P001")

    # 🔍 Recherche dynamique des centres basée sur ce produit en temps réel
    centres_possibles = []
    try:
        with obtenir_connexion(DB_LOCAL) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT centre 
                FROM mouvements_stock 
                WHERE produit_id = ? AND centre IS NOT NULL AND centre != ''
                UNION
                SELECT DISTINCT centre 
                FROM stock 
                WHERE id_produit = ? AND quantite > 0 AND centre IS NOT NULL AND centre != ''
            """, (produit_id_retour, produit_id_retour))
            res_centres = cursor.fetchall()
            if res_centres:
                centres_possibles = [r[0] for r in res_centres if r[0]]
    except Exception:
        pass
    # 🛡️ 2. Filet de sécurité : Si aucun centre n'est trouvé spécifiquement pour ce produit,
    # on récupère TOUS les centres disponibles dans la base pour ne pas bloquer l'utilisateur.
    if not centres_possibles:
        try:
            with obtenir_connexion(DB_LOCAL) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT DISTINCT centre FROM mouvements_stock WHERE centre IS NOT NULL AND centre != ''
                    UNION
                    SELECT DISTINCT centre FROM stock WHERE centre IS NOT NULL AND centre != ''
                """)
                res_tous = cursor.fetchall()
                if res_tous:
                    centres_possibles = [r[0] for r in res_tous if r[0]]
        except Exception:
            pass
    # 🌟 3. Ultime secours si la base est complètement vide
    if not centres_possibles:
        centres_possibles = ["Aucun centre disponible"]

    with st.form("form_retour_client"):
        col_r1, col_r2 = st.columns(2)
        with col_r1:
            vente_id_input = st.text_input("ID de la Vente / Facture d'origine", value="V12345")
            # Le centre s'adapte instantanément au produit saisi au-dessus
            centre_retour = st.selectbox("Centre d'origine (Rattachement)", options=centres_possibles)

        with col_r2:
            quantite_retour = st.number_input("Quantité retournée", min_value=1, value=1, step=1)
            # Recherche dynamique des NOMS associés à cet ID dans ce centre précis
            noms_possibles = [f"Produit {produit_id_retour}"]
            try:
                with obtenir_connexion(DB_LOCAL) as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                             SELECT DISTINCT produit_nom  
                             FROM mouvements_stock 
                             WHERE produit_id = ? AND centre = ? AND produit_nom IS NOT NULL
                        """, (produit_id_retour, centre_retour))
                    resultats = cursor.fetchall()
                    if resultats:
                        noms_possibles = [r[0] for r in resultats if r[0]]

            except Exception:
                pass
            nom_produit_reel = st.selectbox("Nom exact du Produit", options=noms_possibles)

        date_vente_input = st.date_input("Date de l'achat initial")
        motif_retour = st.selectbox("Motif du retour", [
            "Produit défectueux",
            "Erreur de taille/modèle",
            "Insatisfaction client",
            "Échange standard"
        ])

        submitted_retour = st.form_submit_button("🛡️ Valider et Traiter le Retour", type="primary")

        if submitted_retour:
            date_jour = datetime.now().date()
            difference_jours = (date_jour - date_vente_input).days

            if difference_jours < 0:
                st.error("❌ La date d'achat ne peut pas être dans le futur.")
            elif difference_jours > DELAI_MAX_RETOURS_JOURS:
                st.error(
                    f"❌ **Retour refusé :** Le délai de rétractation de {DELAI_MAX_RETOURS_JOURS} jours est dépassé (Achat effectué il y a {difference_jours} jours).")
            else:
                try:
                    date_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    date_vente_str = date_vente_input.strftime('%Y-%m-%d')

                    with obtenir_connexion(DB_LOCAL) as conn:
                        # 🔍 1. AJOUTEZ CETTE RECHERCHE ICI : Récupérer le prix unitaire d'origine
                        cursor_p = conn.cursor()
                        cursor_p.execute("""
                            SELECT prix_unitaire FROM mouvements_stock 
                            WHERE produit_id = ? AND centre = ? AND prix_unitaire IS NOT NULL 
                            LIMIT 1
                        """, (produit_id_retour, centre_retour))
                        res_prix = cursor_p.fetchone()
                        prix_unitaire = res_prix[0] if res_prix and res_prix[0] is not None else 0.0
                        # Enregistrer le retour avec le bon centre d'origine et le bon nom
                        conn.execute("""
                            INSERT INTO retours_clients (vente_id, produit_id, produit_nom, quantite, centre, date_vente, date_retour, motif, statut)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Accepté')
                        """, (vente_id_input, produit_id_retour, nom_produit_reel, quantite_retour, centre_retour,
                              date_vente_str, date_str, motif_retour))

                        # Ré-injecter le stock dans le centre exact d'où provenait la vente
                        conn.execute("""
                            INSERT INTO mouvements_stock (centre, produit_id, produit_nom, type_mouvement, quantite, prix_unitaire, date_mouvement)
                            VALUES (?, ?, ?, 'retour_client', ?, ?,?)
                        """, (centre_retour, produit_id_retour, nom_produit_reel, quantite_retour,prix_unitaire, date_str))

                        conn.commit()
                        # === 1. INITIALISATION DE LA TABLE ET DE LA FONCTION D'AUDIT (À placer en haut) ===
                        try:
                            with obtenir_connexion(DB_LOCAL) as conn:
                                conn.execute("""
                                    CREATE TABLE IF NOT EXISTS journal_audit (
                                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                                        date_heure TEXT,
                                        utilisateur TEXT,
                                        action TEXT,
                                        details TEXT
                                    )
                                """)
                                conn.commit()
                        except Exception as e:
                            st.error(f"Erreur initialisation journal d'audit : {e}")

                        def enregistrer_audit(action, details):
                            try:
                                horodatage = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                                with obtenir_connexion(DB_LOCAL) as conn:
                                    conn.execute("""
                                        INSERT INTO journal_audit (date_heure, utilisateur, action, details)
                                        VALUES (?, 'Administrateur', ?, ?)
                                    """, (horodatage, action, details))
                                    conn.commit()
                            except Exception:
                                pass
                        # ✅ Enregistrement sécurisé dans le journal d'audit ici-même !
                        enregistrer_audit("RETOUR CLIENT",f"Validation du retour {produit_id_retour} ({quantite_retour} unités) pour le centre {centre_retour}")
                    st.success(
                        f"✅ Retour validé pour **{nom_produit_reel}** au **{centre_retour}** (+{quantite_retour} unité(s) remises en stock).")
                    time.sleep(1)
                    st.rerun()

                except Exception as e:
                    st.error(f"Erreur critique lors du traitement du retour : {e}")
    st.divider()

    # 4. Tableau de suivi permanent des retours
    st.markdown("### 📋 Historique Global des Retours")
    try:
        with obtenir_connexion(DB_LOCAL) as conn:
            df_retours = pd.read_sql("""
                SELECT id AS [ID Retour], vente_id AS [ID Vente], centre AS [Centre], 
                       produit_id AS [ID Produit], produit_nom AS [Nom Produit], quantite AS [Quantité], 
                       date_vente AS [Date Achat], date_retour AS [Date Retour & Heure], motif AS [Motif], statut AS [Statut]
                FROM retours_clients 
                ORDER BY date_retour DESC
            """, conn)

        if df_retours.empty:
            st.info("Aucun retour client enregistré pour le moment.")
        else:
            st.dataframe(df_retours, use_container_width=True)
    except Exception as e:
        st.error(f"Impossible de charger l'historique des retours : {e}")

    # === ONGLET 7. INITIALISATION DE LA TABLE D'AUDIT ===
    try:
        with obtenir_connexion(DB_LOCAL) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS journal_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date_heure TEXT,
                    utilisateur TEXT,
                    action TEXT,
                    details TEXT
                )
            """)
            conn.commit()
    except Exception as e:
        st.error(f"Erreur initialisation journal d'audit : {e}")

    # === 2. AFFICHAGE DE L'EXPANDER D'AUDIT ===
    with st.expander("📜 Consulter le Journal d'Audit du Système"):
        try:
            with obtenir_connexion(DB_LOCAL) as conn:
                df_audit = pd.read_sql("""
                    SELECT 
                         id AS [ID],
                         date_heure AS [Date & Heure],
                         utilisateur AS [Utilisateur],
                         action AS [Action Réalisée],
                         details AS [Détails]
                    FROM journal_audit 
                    ORDER BY date_heure DESC 
                    LIMIT 50
                """, conn)
            # On nettoie et met en forme directement
            if not df_audit.empty and 'Action Réalisée' in df_audit.columns:
                df_audit['Action Réalisée'] = df_audit['Action Réalisée'].str.replace('_', ' ').str.title()
            st.dataframe(
                df_audit, use_container_width=True,
                column_config={
                    "ID": st.column_config.NumberColumn("ID", width="small"),
                    "Date & Heure": st.column_config.TextColumn("Date & Heure", width="medium"),
                    "Utilisateur": st.column_config.TextColumn("Utilisateur", width="medium"),
                    "Action Réalisée": st.column_config.TextColumn("Action Réalisée", width="medium"),
                    "Détails": st.column_config.TextColumn("Détails Complets", width="large"),
                },
                hide_index=True
            )
        except Exception:
            st.info("Journal d'audit vide.")

    st.divider()

    # === 3. ANALYSE COMPLÈTE DES TAUX DE RETOUR (GRAPHIQUE + TABLEAU STYLISÉ + EXPORTS) ===
    st.markdown("### 📊 Analyse des Taux de Retour et Indicateurs de Risque")

    try:
        with obtenir_connexion(DB_LOCAL) as conn:
            df_analyse = pd.read_sql("""
                SELECT 
                    v.centre AS [Centre],
                    v.produit_id AS [ID Produit],
                    v.produit_nom AS [Nom Produit],
                    SUM(v.quantite) AS [Total Vendu],
                    COALESCE(SUM(r.quantite), 0) AS [Total Retourné],
                    ROUND((CAST(COALESCE(SUM(r.quantite), 0) AS REAL) / NULLIF(SUM(v.quantite), 0)) * 100, 2) AS [Taux de Retour (%)]
                FROM ventes v
                LEFT JOIN retours_clients r ON v.produit_id = r.produit_id AND v.centre = r.centre
                GROUP BY v.centre, v.produit_id, v.produit_nom
            """, conn)

        if df_analyse.empty:
            st.info("Pas assez de données de ventes et retours pour calculer les taux.")
        else:
            # A. Graphique interactif Plotly
            # Note: on utilise les noms de colonnes propres sans espaces pour Plotly pour éviter tout bug
            df_plot = df_analyse.rename(columns={
                'Centre': 'centre',
                'Nom Produit': 'produit_nom',
                'Taux de Retour (%)': 'taux_retour_pct'
            })

            fig = px.bar(
                df_plot,
                x="produit_nom",
                y="taux_retour_pct",
                color="centre",
                title="Taux de retour par produit et par centre (%)",
                labels={"taux_retour_pct": "Taux de retour (%)", "produit_nom": "Produit", "centre": "Centre"},
                text="taux_retour_pct"
            )
            fig.update_traces(texttemplate='%{text}%', textposition='outside')
            st.plotly_chart(fig, use_container_width=True)

            # B. Tableau stylisé avec alerte couleur (Mise en avant si taux > 10%)
            def color_seuil(val):
                return 'background-color: #ffcccc; color: #990000;' if val > 10 else ''

            st.markdown("#### 📋 Tableau détaillé des taux et exports")

            # Affichage avec style conditionnel Pandas sur la colonne des taux
            st.dataframe(
                df_analyse.style.map(color_seuil, subset=['Taux de Retour (%)']),
                use_container_width=True
            )

            # C. Boutons d'exportation (CSV)
            col_exp1, col_exp2 = st.columns(2)

            with col_exp1:
                csv_analyse = df_analyse.to_csv(index=False).encode('utf-8')
                st.download_button(
                    label="📥 Télécharger l'analyse en CSV",
                    data=csv_analyse,
                    file_name=f"analyse_taux_retours_{datetime.now().strftime('%Y%m%d')}.csv",
                    mime="text/csv"
                )

            with col_exp2:
                with obtenir_connexion(DB_LOCAL) as conn_exp:
                    df_retours_export = pd.read_sql("SELECT * FROM retours_clients", conn_exp)
                csv_retours = df_retours_export.to_csv(index=False).encode('utf-8')
                st.download_button(
                    label="📥 Télécharger l'historique complet des retours",
                    data=csv_retours,
                    file_name=f"historique_retours_{datetime.now().strftime('%Y%m%d')}.csv",
                    mime="text/csv"
                )

    except Exception as e:
        st.error(f"Erreur lors de l'analyse des taux de retour : {e}")
# --- Point d'entrée pour l'exécution directe Streamlit ---
if __name__ == "__main__":
    afficher_resume_streamlit()