# === Fichier: centre_central/serveur_central.py ===
import logging
import sqlite3
from pathlib import Path
from typing import List
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, ConfigDict, AliasChoices

DB_CENTRAL = Path(__file__).resolve().parent / "centre_central.db"
print(f"📂 Chemin de la base centrale utilisée : {DB_CENTRAL}")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("serveur_central")


class MouvementSchema(BaseModel):
    centre: str
    produit_id: str
    produit_nom: str
    quantite: int
    prix: float = Field(..., validation_alias=AliasChoices('prix', 'prix_unitaire'))
    date: str
    type_mouvement: str

    model_config = ConfigDict(populate_by_name=True)


def init_db():
    """Crée les tables centrales, index et contraintes idempotentes."""
    DB_CENTRAL.parent.mkdir(parents=True, exist_ok=True)
    print(f"📂 Connexion à la base centrale : {DB_CENTRAL}")
    with sqlite3.connect(DB_CENTRAL) as conn:
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
        except sqlite3.OperationalError:
            pass

        # 1. Historique consolidé (Idempotence via contrainte UNIQUE)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mouvements_stock (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                centre TEXT NOT NULL,
                produit_id TEXT NOT NULL,
                produit_nom TEXT NOT NULL,
                type_mouvement TEXT NOT NULL DEFAULT 'sortie',
                quantite INTEGER NOT NULL,
                prix_unitaire REAL NOT NULL,
                date_mouvement TEXT NOT NULL,
                UNIQUE(centre, produit_id, date_mouvement, quantite, type_mouvement)
            )
        """)

        # 2. État des stocks par centre
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                centre TEXT NOT NULL,
                produit_id TEXT NOT NULL,
                produit_nom TEXT NOT NULL,
                quantite_disponible INTEGER NOT NULL DEFAULT 0,
                prix_unitaire REAL NOT NULL,
                UNIQUE(centre, produit_id)
            )
        """)

        # 3. Indexation pour requêtes et consolidations rapides
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mvt_type ON mouvements_stock(type_mouvement);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stock_centre ON stock(centre, produit_id);")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialisation sécurisée des bases au démarrage de l'application."""
    init_db()
    yield


app = FastAPI(
    title="API Centralisation",
    lifespan=lifespan
)


@app.post("/api/mouvements/batch")
def recevoir_mouvements(mouvements: List[MouvementSchema]):
    """Reçoit un lot de mouvements, enregistre l'historique et met à jour le stock consolidé (sans ON CONFLICT)."""
    if not mouvements:
        return {"status": "success", "message": "Aucun mouvement à traiter", "nombre_insere": 0}

    TYPES_ENTREE = {'entree', 'entrée', 'approvisionnement', 'entree_stock', 'correction_entree', 'retour_client','transfert_entree', 'ajustement_positif'}
    TYPES_SORTIE = {'sortie', 'vente', 'correction_sortie', 'suppression', 'retour_fournisseur', 'transfert_sortie','perte', 'casse', 'ajustement_negatif'}
    TYPES_MODIFICATION = {'modification_produit', 'modification'}

    try:
        with sqlite3.connect(DB_CENTRAL, timeout=15, isolation_level="IMMEDIATE") as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA busy_timeout=5000;")
            nouveaux_inseres = 0

            for m in mouvements:
                centre = m.centre.strip()
                produit_id = m.produit_id.strip().upper()
                produit_nom = m.produit_nom.strip()
                type_mvt = (m.type_mouvement or "").lower().strip()
                qte_abs = abs(m.quantite)

                # 1. Calcul du delta de stock
                if type_mvt in TYPES_ENTREE:
                    delta_qte = qte_abs
                elif type_mvt in TYPES_SORTIE:
                    delta_qte = -qte_abs
                elif type_mvt in TYPES_MODIFICATION:
                    delta_qte = 0
                else:
                    continue

                # 2. Insertion dans l'historique avec dédoublonnage automatique
                cursor.execute("""
                    INSERT OR IGNORE INTO mouvements_stock 
                    (centre, produit_id, produit_nom, quantite, prix_unitaire, date_mouvement, type_mouvement)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (centre, produit_id, produit_nom, qte_abs, m.prix, m.date.strip(), type_mvt))

                # Si l'enregistrement existait déjà (doublon ignoré), on ne touche pas au stock
                if cursor.rowcount <= 0:
                    continue

                nouveaux_inseres += 1

                # 3. Gestion manuelle de l'UPSERT (Sécurisée, sans dépendre de ON CONFLICT)
                cursor.execute("""
                    SELECT id, quantite_disponible FROM stock 
                    WHERE centre = ? AND produit_id = ?
                """, (centre, produit_id))
                ligne_existante = cursor.fetchone()

                if ligne_existante:
                    # Le produit existe déjà dans ce centre : on met à jour
                    cursor.execute("""
                        UPDATE stock 
                        SET quantite_disponible = MAX(0, quantite_disponible + ?),
                            prix_unitaire = CASE WHEN ? > 0 THEN ? ELSE prix_unitaire END,
                            produit_nom = ?
                        WHERE centre = ? AND produit_id = ?
                    """, (delta_qte, m.prix, m.prix, produit_nom, centre, produit_id))
                else:
                    # Le produit n'existe pas : on l'insère
                    qte_initiale = max(0, delta_qte)
                    cursor.execute("""
                        INSERT INTO stock (centre, produit_id, produit_nom, quantite_disponible, prix_unitaire)
                        VALUES (?, ?, ?, ?, ?)
                    """, (centre, produit_id, produit_nom, qte_initiale, m.prix))

            conn.commit()

            return {
                "status": "success",
                "message": f"Lot de {len(mouvements)} mouvements reçu ({nouveaux_inseres} nouveau(x) consolidé(s)).",
                "nombre_insere": nouveaux_inseres
            }

    except Exception as err:
        logger.error(f"Erreur lors de la synchronisation batch : {err}")
        raise HTTPException(status_code=500, detail=f"Erreur serveur central : {str(err)}") from None