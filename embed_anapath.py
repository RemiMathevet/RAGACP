#!/usr/bin/env python3
"""Indexe chapitres/ dans anapath_rag.db — BioLORD-2023-M (FR et EN dans un même espace).

    python embed_anapath.py [--dry-run] [--only livres|cours]

Réutilise le découpage et le stockage de ~/Bureau/Embedding_RAG_V2/embed_biolord.py, en changeant :
  - le modèle : BioLORD-2023-M (multilingue, distillé dans l'espace de BioLORD-2023). Test du
    2026-10-21 sur 6 paires de termes FR↔EN : cosinus moyen 0,60 avec BioLORD-2023 contre 0,94 avec -M.
    L'espace n'est PAS celui de rag_v2.db / placenta_rag.db : ne jamais comparer leurs vecteurs ;
  - source_type : chapitres/livres -> 'book', chapitres/cours -> 'cours' (preuve inférieure) ;
  - la langue est détectée par document (CoPath et EPU en français).
Incrémental : un (source_short, chapitre) déjà en base n'est pas réindexé. FTS reconstruit et vérifié.
"""
import argparse, sqlite3, sys
from pathlib import Path

_EXT = Path.home() / "Bureau" / "Embedding_RAG_V2"       # copie dans vendor/ pour le dépôt public
sys.path.insert(0, str(_EXT if _EXT.exists() else Path(__file__).resolve().parent / "vendor"))
import embed_biolord as eb

ROOT = Path(__file__).resolve().parent
DB = ROOT / "anapath_rag.db"
MODEL = "FremyCompany/BioLORD-2023-M"
eb.MODEL_NAME = MODEL

# Clés de split_anapath.BOOKS : année, ISBN au besoin.
YEARS = {"copath2012": 2012, "robbins_cotran": 2026, "robbins_basic": 2022, "oxford_handbook": 2024,
         "colvin_kidney": 2015, "zhang_lung": 2018, "leslie_lung": 2017, "connolly_autopsy": 2016}


def existing(conn):
    try:
        vus = {f"{s}/ch{n}" for s, n in conn.execute("SELECT DISTINCT source_short, chapter_num FROM chunk_meta")}
    except sqlite3.OperationalError:
        return set()
    try:            # chapitres entièrement sortis par biblio_anapath : ne pas les réindexer
        vus |= {f"{s}/ch{n}" for s, n in conn.execute("SELECT DISTINCT source_short, chapter_num FROM chunk_biblio")}
    except sqlite3.OperationalError:
        pass
    return vus


def items_for(kind, conn):
    d = ROOT / "chapitres" / kind
    if not d.is_dir():
        return []
    for b in d.iterdir():
        if b.is_dir() and b.name not in eb.BOOK_META:
            first = next(iter(sorted(b.glob("*.txt"))), None)
            lang = eb.detect_lang(first.read_text(encoding="utf-8")) if first else "en"
            eb.BOOK_META[b.name] = {"source_short": b.name, "year": YEARS.get(b.name),
                                    "language": lang, "isbn": None}
    items = eb.prepare_chapitres(conn, existing(conn), str(d))
    for it in items:
        it["source_type"] = "book" if kind == "livres" else "cours"
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", choices=["livres", "cours"])
    a = ap.parse_args()

    conn = eb.init_db(str(DB))
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    old = conn.execute("SELECT value FROM meta WHERE key='model'").fetchone()
    if old and old[0] != MODEL:
        sys.exit(f"anapath_rag.db indexée avec {old[0]}, pas {MODEL} : espaces incompatibles")
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('model', ?)", (MODEL,))

    items = []
    for kind in ([a.only] if a.only else ["livres", "cours"]):
        got = items_for(kind, conn)
        print(f"{kind}: {len(got)} chunks à indexer")
        items += got
    if a.dry_run:
        for it in items[:3]:
            print(f"  [{it['source_type']}/{it['paragraph_id']}/{it['language']}] {it['chunk_text'][:100]}")
        return

    eb.embed_and_store(items, conn)
    import biblio_anapath
    print(f"bibliographies sorties : {biblio_anapath.purge(conn)}")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts "
                 "USING fts5(chunk_text, content='chunk_meta', content_rowid='rowid')")
    conn.execute("INSERT INTO chunk_fts(chunk_fts) VALUES('rebuild')")
    conn.execute("INSERT INTO chunk_fts(chunk_fts) VALUES('integrity-check')")
    conn.commit()
    # FTS mort = MATCH à 0 alors que LIKE trouve : croiser les deux
    n_like = conn.execute("SELECT count(*) FROM chunk_meta WHERE chunk_text LIKE '%necrosis%'").fetchone()[0]
    n_fts = conn.execute("SELECT count(*) FROM chunk_fts WHERE chunk_fts MATCH 'necrosis'").fetchone()[0]
    print(f"contrôle FTS « necrosis » : LIKE {n_like} / MATCH {n_fts}")
    assert n_like == 0 or n_fts > 0, "index FTS mort"
    for row in conn.execute("SELECT source_type, language, count(*), count(DISTINCT source_short) "
                            "FROM chunk_meta GROUP BY 1, 2"):
        print("  ", row)
    conn.commit()


if __name__ == "__main__":
    main()
