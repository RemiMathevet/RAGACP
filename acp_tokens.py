#!/usr/bin/env python3
"""Jetons de téléchargement à usage unique pour le paquet RAGACP (servi par web_acp.py sur /dl/<jeton>).

    python acp_tokens.py nouveau --note "Dr X, test"     # affiche le lien, une seule fois
    python acp_tokens.py liste
    python acp_tokens.py revoquer <id>

Seul le hachage SHA-256 du jeton est stocké (acp_tokens.db) : un lien perdu ne se retrouve pas, on en crée un autre.
Un jeton sert UNE fois : il est consommé au début du téléchargement (un téléchargement interrompu le consomme aussi).
Le lien n'apparaît sur aucune page du site.
"""
import argparse, hashlib, os, secrets, sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent / "acp_tokens.db"
URL = os.environ.get("ACP_URL", "https://acp.adeptus-mechanicus.uk")


def con():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS jetons (id INTEGER PRIMARY KEY, hachage TEXT UNIQUE, note TEXT,
                 cree TEXT DEFAULT (datetime('now','localtime')), utilise TEXT, ip TEXT, revoque INTEGER DEFAULT 0)""")
    return c


def hacher(jeton):
    return hashlib.sha256(jeton.encode()).hexdigest()


def consommer(jeton, ip=""):
    """True si le jeton était valide et inutilisé ; il est marqué utilisé dans la même requête (pas de double usage)."""
    c = con()
    n = c.execute("UPDATE jetons SET utilise=datetime('now','localtime'), ip=? WHERE hachage=? AND utilise IS NULL "
                  "AND revoque=0", (ip, hacher(jeton))).rowcount
    c.commit()
    return n == 1


def _selfcheck():
    global DB
    import tempfile
    DB = Path(tempfile.mkdtemp()) / "t.db"
    j = secrets.token_urlsafe(32)
    con().execute("INSERT INTO jetons (hachage) VALUES (?)", (hacher(j),)).connection.commit()
    assert consommer(j) and not consommer(j) and not consommer("faux")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    n = sp.add_parser("nouveau"); n.add_argument("--note", default="")
    sp.add_parser("liste")
    r = sp.add_parser("revoquer"); r.add_argument("id", type=int)
    sp.add_parser("test")
    a = ap.parse_args()
    if a.cmd == "test":
        _selfcheck(); print("ok")
    elif a.cmd == "nouveau":
        j = secrets.token_urlsafe(32)
        c = con(); c.execute("INSERT INTO jetons (hachage, note) VALUES (?,?)", (hacher(j), a.note)); c.commit()
        print(f"{URL}/dl/{j}")
    elif a.cmd == "liste":
        for row in con().execute("SELECT id, note, cree, utilise, ip, revoque FROM jetons ORDER BY id"):
            print(*row, sep="\t")
    else:
        c = con(); c.execute("UPDATE jetons SET revoque=1 WHERE id=?", (a.id,)); c.commit()
