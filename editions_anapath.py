#!/usr/bin/env python3
"""Édition et année de chaque source -> table source_info (source_short, edition, annee, origine) de anapath_rag.db.

    python editions_anapath.py

But (rag.md §1.1) : montrer au modèle « OMS SNC · 2016 · 4e éd. rév. » pour qu'il signale lui-même une source
antérieure à la classification demandée. Ordre des preuves : nom du fichier (« 5th 2019 »), puis les 8 premières
pages du PDF (« Revised 4th Edition », « © 2016 »), puis l'année de création du PDF (origine 'pdf', approximative).
Libre Pathology : wiki, pas d'édition. Une ligne de CORRIGE l'emporte sur tout.
"""
import re, sqlite3
from pathlib import Path
import fitz
from split_anapath import jobs

ROOT = Path(__file__).resolve().parent
ED = re.compile(r"\b(revised\s+)?(\d{1,2})\s*(?:st|nd|rd|th|e|ème|eme)\s*(?:ed\b|ed\.|edition|édition)", re.I)
AN = re.compile(r"(?:©|copyright|\(c\))[^\n]{0,60}?\b((?:19|20)\d\d)\b", re.I)
ED_NOM = re.compile(r"\b()(\d{1,2})(?:st|nd|rd|th)\b", re.I)      # « 5th 2019 » dans un nom de fichier
AN_NOM = re.compile(r"(?<!\d)((?:19|20)\d\d)(?!\d)")
CORRIGE = {"librepathology": (None, None, "wiki")}


def edition(m):
    return f"{m.group(2)}e éd.{' rév.' if m.group(1) else ''}"


def info(pdf):
    nom = pdf.stem
    ed, an, orig = None, None, None
    if m := ED.search(nom) or ED_NOM.search(nom):
        ed, orig = edition(m), "nom"
    if ans := [int(a) for a in AN_NOM.findall(nom) if 1980 <= int(a) <= 2027]:
        an, orig = max(ans), "nom"
    try:
        doc = fitz.open(pdf)
        tete = "\n".join(doc[i].get_text() for i in range(min(8, len(doc))))
        cree = doc.metadata.get("creationDate", "")[2:6]
    except Exception:
        tete, cree = "", ""
    if not ed and (m := ED.search(tete)):
        ed, orig = edition(m), orig or "pdf_texte"
    if not an and (ans := [int(a) for a in AN.findall(tete) if 1980 <= int(a) <= 2027]):
        an, orig = max(ans), "pdf_texte"
    if not an and cree.isdigit():
        an, orig = int(cree), "pdf_meta"
    return ed, an, orig


def _selfcheck():
    assert edition(ED_NOM.search("WHO Classification of Soft Tissue and Bone Tumours 5th 2019")) == "5e éd."
    assert edition(ED.search("Editors ... Revised 4th Edition Editors")) == "4e éd. rév."
    assert AN.search("Copyright © 2016 International Agency").group(1) == "2016"


if __name__ == "__main__":
    _selfcheck()
    con = sqlite3.connect(ROOT / "anapath_rag.db")
    con.execute("CREATE TABLE IF NOT EXISTS source_info (source_short TEXT PRIMARY KEY, edition TEXT, annee INTEGER, origine TEXT)")
    n = 0
    for k, pdf, _, _ in jobs(None):
        con.execute("INSERT OR REPLACE INTO source_info VALUES (?,?,?,?)", (k, *CORRIGE.get(k, info(pdf))))
        n += 1
    for k, v in CORRIGE.items():
        con.execute("INSERT OR REPLACE INTO source_info VALUES (?,?,?,?)", (k, *v))
    con.commit()
    print(n, "sources ;", dict(con.execute("SELECT origine, count(*) FROM source_info GROUP BY 1").fetchall()))
    for r in con.execute("SELECT * FROM source_info WHERE source_short LIKE 'who%' OR source_short IN "
                         "('robbins_cotran','colvin_kidney','dp_soft_tissue_tumors')"):
        print("  ", r)
