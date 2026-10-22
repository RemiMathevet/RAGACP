#!/usr/bin/env python3
"""Sort les passages de bibliographie de l'index (vectoriel, FTS, pages) vers la table chunk_biblio.

    python biblio_anapath.py [--dry] [--seuil 0.5]

Un titre d'article n'est pas une preuve (rag.md §1.2) : ~10 % des passages étaient des listes de références.
Part estimée = nb de motifs de référence (« 2004;43:605 », « et al », PMID, doi) × 20 mots / nb de mots.
Seuil 0,5 : à 0,5 l'échantillon est déjà 100 % références, à 0,3 encore du texte clinique (sondage du 2026-10-21).
Les passages sortis restent dans chunk_biblio (texte + métadonnées) pour de futures « lectures complémentaires ».
Appelé en fin d'embed_anapath.py ; embed_anapath.existing() compte chunk_biblio pour ne pas réindexer.
"""
import argparse, re, shutil, sqlite3, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "anapath_rag.db"
REF = re.compile(r"\b(?:19|20)\d\d[a-z]?\s*[;,.]\s*\d+\s*(?:\(\s*[\dS-]+\s*\))?\s*[:,]\s*[A-Z]?\d+"
                 r"|\bet al\b\.?|\bPMID\b|\bdoi\s*:", re.I)


def part_biblio(t):
    return min(len(REF.findall(t)) * 20 / (len(t.split()) or 1), 1.0)


def purge(con, seuil=0.5, dry=False):
    con.execute("CREATE TABLE IF NOT EXISTS chunk_biblio AS SELECT * FROM chunk_meta WHERE 0")
    ids = [r for r, t in con.execute("SELECT rowid, chunk_text FROM chunk_meta") if part_biblio(t) >= seuil]
    if dry or not ids:
        return len(ids)
    shutil.copy(DB, ROOT / "refs" / time.strftime("anapath_rag.bak-biblio-%Y%m%d-%H%M.db"))
    for i in range(0, len(ids), 500):
        q = ",".join("?" * len(ids[i:i + 500]))
        b = ids[i:i + 500]
        con.execute(f"INSERT OR REPLACE INTO chunk_biblio SELECT * FROM chunk_meta WHERE rowid IN ({q})", b)
        for t in ("vec_chunks", "chunk_pages", "chunk_meta"):
            con.execute(f"DELETE FROM {t} WHERE rowid IN ({q})", b)
    con.execute("INSERT INTO chunk_fts(chunk_fts) VALUES('rebuild')")
    con.execute("INSERT INTO chunk_fts(chunk_fts) VALUES('integrity-check')")
    con.commit()
    return len(ids)


def _selfcheck():
    assert part_biblio("874. Tanzi RE, Moir RD. Clearance of Abeta. Neuron 2004;43:605–8. 875. Tartaglia MC, et al. "
                       "Sporadic CBS. Acta Neuropathol 2010;119:365–74.") >= 0.5
    assert part_biblio("The Mohs surgeon may need to consult a dermatopathologist to help assess margins in rare "
                       "and difficult tumors, described in 2004 by several groups.") < 0.5


if __name__ == "__main__":
    _selfcheck()
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--seuil", type=float, default=0.5)
    a = ap.parse_args()
    import sqlite_vec
    con = sqlite3.connect(DB)
    con.enable_load_extension(True); sqlite_vec.load(con)
    n = purge(con, a.seuil, a.dry)
    print(f"{n} passages de bibliographie {'repérés' if a.dry else 'sortis vers chunk_biblio'} ; "
          f"reste {con.execute('SELECT count(*) FROM chunk_meta').fetchone()[0]} dans l'index")
