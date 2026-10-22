#!/usr/bin/env python3
"""Pages imprimées des chunks de anapath_rag.db (livres ET cours) -> table chunk_pages.

    python pages_anapath.py [--workers 8] [--tout]

Réutilise les fonctions de ~/Bureau/Embedding_RAG_V2/pages_chunks.py (texte normalisé, ancres de 60 caractères,
étiquettes PDF ou décalage imprimé − index). Différence : le PDF de chaque source vient de split_anapath.jobs()
(clé -> PDF), au lieu d'être deviné en cherchant les ancres dans tous les PDF (trop lent sur 1 100 PDF).
Incrémental : seuls les chunks sans ligne dans chunk_pages sont traités (--tout pour tout refaire).
Sauvegarde de la base avant écriture. Libre Pathology (pas de PDF) : pas de page.
"""
import argparse, collections, shutil, sqlite3, sys, time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

_EXT = Path.home() / "Bureau" / "Embedding_RAG_V2"       # copie dans vendor/ pour le dépôt public
sys.path.insert(0, str(_EXT if _EXT.exists() else Path(__file__).resolve().parent / "vendor"))
import pages_chunks as pc
from split_anapath import jobs

ROOT = Path(__file__).resolve().parent
DB = ROOT / "anapath_rag.db"


def une_source(arg):
    src, pdf, ch = arg
    try:
        tx, debuts, labels, mode = pc.pages_pdf(pdf)
    except Exception as e:
        return src, [], f"illisible : {e}"
    out = []
    for r, t in ch:
        pos = [q for q in (tx.find(x) for x in pc.ancres(t)) if q >= 0]
        if not pos:
            continue
        p0, p1 = pc.page_de(debuts, min(pos)), pc.page_de(debuts, max(pos))
        if p1 - p0 > 15:
            p1 = p0
        out.append((r, pdf.name, p0 + 1, p1 + 1, labels[p0], labels[p1], len(pos)))
    return src, out, mode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--tout", action="store_true")
    a = ap.parse_args()
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS chunk_pages (rowid INTEGER PRIMARY KEY, pdf TEXT, page_pdf INTEGER, "
                "page_pdf_fin INTEGER, page_label TEXT, page_label_fin TEXT, n_ancres INTEGER)")
    sql = "SELECT m.rowid, m.source_short, m.chunk_text FROM chunk_meta m"
    if not a.tout:
        sql += " LEFT JOIN chunk_pages p USING (rowid) WHERE p.rowid IS NULL"
    par = collections.defaultdict(list)
    for r, s, t in con.execute(sql):
        par[s].append((r, t))
    pdf_of = {k: p for k, p, _, _ in jobs(None)}
    taches = [(s, pdf_of[s], ch) for s, ch in par.items() if s in pdf_of]
    sans = sum(len(ch) for s, ch in par.items() if s not in pdf_of)
    print(f"{sum(len(c) for c in par.values())} chunks à paginer, {len(taches)} sources ; "
          f"{sans} sans PDF (Libre Pathology…)", flush=True)
    shutil.copy(DB, ROOT / "refs" / time.strftime("anapath_rag.bak-pages-%Y%m%d-%H%M.db"))
    n_ok = 0
    with ProcessPoolExecutor(a.workers) as ex:
        for i, (src, out, mode) in enumerate(ex.map(une_source, taches, chunksize=4), 1):
            con.executemany("INSERT OR REPLACE INTO chunk_pages VALUES (?,?,?,?,?,?,?)", out)
            n_ok += len(out)
            if i % 50 == 0 or len(par[src]) > 500:
                con.commit()
                print(f"  {i}/{len(taches)}  {src[:40]:40s} {len(out)}/{len(par[src])}  ({mode})", flush=True)
    con.commit()
    print(f"{n_ok} chunks paginés.")


if __name__ == "__main__":
    main()
