#!/usr/bin/env python3
"""Recherche dans anapath_rag.db avec EXPANSION FR↔EN par le MeSH (anapath_terms.db).

    python recherche.py "hyalinose segmentaire et focale" [-k 8] [--brut]

1. La question est rattachée à des descripteurs MeSH : forme lexicale normalisée identique (build_terms.norm),
   sinon cosinus BioLORD-2023-M ≥ 0,90 contre les termes MeSH (top 3).
2. Variantes = question + libellés/synonymes FR et EN de ces descripteurs.
3. Vectoriel : chaque variante, distance minimale par chunk. FTS : variantes en phrases exactes.
   Fusion par rang réciproque (RRF). --brut : la question seule, pour comparer.
Cours pondérés 0,8 face au livre ; la page imprimée vient de chunk_pages.
"""
import argparse, sqlite3, struct
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent
MODEL = "FremyCompany/BioLORD-2023-M"
CACHE = ROOT / "refs" / "mesh_termes_M.npz"
_mod = None


def model():
    global _mod
    if _mod is None:
        from sentence_transformers import SentenceTransformer
        _mod = SentenceTransformer(MODEL, device="cpu")    # ponytail: CPU, le GPU sert à Magos
    return _mod


def enc(xs):
    return model().encode(xs, batch_size=256, normalize_embeddings=True, show_progress_bar=False)


def mesh_index(tc):
    rows = tc.execute("SELECT mesh_id, terme FROM mesh_terme").fetchall()
    if CACHE.exists():
        z = np.load(CACHE, allow_pickle=True)
        if len(z["ids"]) == len(rows):
            return z["ids"], z["E"]
    ids, E = np.array([r[0] for r in rows]), enc([r[1] for r in rows])
    np.savez(CACHE, ids=ids, E=E)
    return ids, E


def expand(q, tc):
    from build_terms import norm
    hit = {m for (m,) in tc.execute("SELECT DISTINCT mesh_id FROM mesh_terme WHERE norm=?", (norm(q),))}
    if not hit:
        ids, E = mesh_index(tc)
        s = E @ enc([q])[0]
        hit = {ids[i] for i in np.argsort(-s)[:3] if s[i] >= 0.90}
    var = {q}
    for m in hit:
        var |= {t for (t,) in tc.execute("SELECT terme FROM mesh_terme WHERE mesh_id=?", (m,))}
    return sorted(hit), sorted(var, key=len)[:12]


def search(q, k=8, brut=False, livre=None):
    rc = sqlite3.connect(ROOT / "anapath_rag.db")
    rc.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(rc)
    tc = sqlite3.connect(ROOT / "anapath_terms.db")
    hit, var = ([], [q]) if brut else expand(q, tc)
    rang = {}
    vecs = enc(var)
    best = {}
    # ponytail: filtre livre = KNN élargi à 400 puis filtré ; partition sqlite-vec si un livre rate ses passages
    permis = {r for (r,) in rc.execute("SELECT rowid FROM chunk_meta WHERE source_short=?", (livre,))} if livre else None
    for v in vecs:
        for r, d in rc.execute(f"SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? AND k={400 if livre else 40}",
                               (struct.pack(f"{len(v)}f", *v),)):
            if permis is None or r in permis:
                best[r] = min(d, best.get(r, 9))
    for i, r in enumerate(sorted(best, key=best.get)):
        rang[r] = rang.get(r, 0) + 1 / (60 + i)
    fts = " OR ".join('"' + t.replace('"', "") + '"' for t in var)
    filtre = " AND rowid IN (SELECT rowid FROM chunk_meta WHERE source_short=?)" if livre else ""
    for i, (r,) in enumerate(rc.execute(f"SELECT rowid FROM chunk_fts WHERE chunk_fts MATCH ?{filtre} ORDER BY rank LIMIT 40",
                                        (fts, livre) if livre else (fts,))):
        rang[r] = rang.get(r, 0) + 1 / (60 + i)
    out = []
    for r in sorted(rang, key=rang.get, reverse=True)[:k * 3]:
        st, src, ch, pl, txt = rc.execute("""SELECT m.source_type, m.source_short, m.chapter_title,
                p.page_label, m.chunk_text FROM chunk_meta m LEFT JOIN chunk_pages p USING (rowid)
                WHERE m.rowid=?""", (r,)).fetchone()
        poids = 1.0 if st == "book" else 0.8                     # cours = preuve inférieure
        out.append((st, rang[r] * poids, src, ch, pl, txt))
    out.sort(key=lambda x: -x[1])
    return hit, var, out[:k]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("q")
    ap.add_argument("-k", type=int, default=8)
    ap.add_argument("--brut", action="store_true")
    a = ap.parse_args()
    hit, var, out = search(a.q, a.k, a.brut)
    print("MeSH :", ", ".join(hit) or "—", "\nvariantes :", " | ".join(var))
    for _, s, src, ch, pl, txt in out:
        print(f"  {s:.4f}  {src:18s} p.{pl or '?':>5s}  {ch[:40]:40s}  {' '.join(txt.split())[:70]}")
