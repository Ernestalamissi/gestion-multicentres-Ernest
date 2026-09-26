import sqlite3

def creer_connexion(nom_db='centre_local.db'):
    return sqlite3.connect(nom_db)
