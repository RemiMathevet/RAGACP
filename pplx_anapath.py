#!/usr/bin/env python3
"""A/B d'index, recherche seule : pplx-embed-v2-context-9b-preview (vecteurs contextuels) contre BioLORD-2023-M.

    ~/venvs/pplx/bin/python -u pplx_anapath.py index [--max-docs N]   # nuit, reprise par tranches
    ~/venvs/pplx/bin/python -u pplx_anapath.py eval                   # pplx/gold.tsv -> hit@4, hit@10, MRR

venv à part : transformers>=5.4 exigé (~/venvs/pplx = venv Bureau + transformers récent, voir bureau.pth).
Index = pplx/tranche_*.npz (rowids + float16 2048 d) ; anapath_rag.db n'est pas modifiée.
Document = passages d'un même (livre, chapitre), dans l'ordre, par fenêtres de FENETRE caractères : chaque passage
est encodé en voyant ses voisins (« Negative for desmin, EMA, and WT1 » sait qu'il est dans le tableau de l'Ewing).
Pertinent = passage contenant tous les critères (regex) de gold.tsv : grossier, mais déterministe et identique
pour tous les systèmes. Préversion : vecteurs à refaire à la version suivante, ne pas les mélanger.
"""
import argparse, csv, re, sqlite3, struct, sys, time
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "pplx"
MODELE = "perplexity-ai/pplx-embed-v2-context-9b-preview"
FENETRE = 40000          # caractères par document (~10k tokens) ; moitié en cas d'OOM
PAR_TRANCHE = 100        # documents par fichier de reprise
_m = None
SQL = ("SELECT rowid, source_short, coalesce(chapter_title,''), chunk_text FROM chunk_meta "
       "ORDER BY source_short, rowid")
# --base foeto : même encodeur, base et index fœto séparés (jamais mêlés à l'anapath) ; livres seulement
FOETO = (Path.home() / "Bureau/foeto_base/syndromes_foetaux.db", Path.home() / "Bureau/foeto_base/pplx",
         "SELECT rowid, source_id, '', chunk_text FROM chunk_meta WHERE source_type='book' "
         "ORDER BY source_id, chunk_index, rowid")
DB = ROOT / "anapath_rag.db"


def modele(device="cuda"):
    global _m
    if _m is None:
        import torch
        from transformers import AutoModel
        from transformers import AutoTokenizer
        _m = AutoModel.from_pretrained(MODELE, trust_remote_code=True, dtype=torch.bfloat16).to(device).eval()
        # la propriété tokenizer du code distant échoue (AttributeError masqué par nn.Module) : on le fournit
        _m.tokenizer = AutoTokenizer.from_pretrained(MODELE, trust_remote_code=True, padding_side="right")
    return _m


def documents(con, sql=None):
    """[(rowids, textes)] : un (livre, chapitre) découpé en fenêtres, ordre stable (reprise)."""
    docs, cle, cur = [], None, ([], [], 0)
    for r, src, ch, t in con.execute(sql or SQL):
        if (src, ch) != cle or cur[2] + len(t) > FENETRE:
            if cur[0]:
                docs.append(cur[:2])
            cle, cur = (src, ch), ([], [], 0)
        cur[0].append(r); cur[1].append(t); cur = (cur[0], cur[1], cur[2] + len(t))
    if cur[0]:
        docs.append(cur[:2])
    return docs


def encoder_doc(textes):
    import torch
    try:
        with torch.inference_mode():
            return np.asarray(modele().encode([textes], normalize_embeddings=True, batch_size=1)[0], dtype=np.float32)
    except torch.OutOfMemoryError:
        torch.cuda.empty_cache()
        if len(textes) == 1:
            raise
        h = len(textes) // 2                     # ponytail: on coupe en deux, le contexte se réduit d'autant
        return np.vstack([encoder_doc(textes[:h]), encoder_doc(textes[h:])])


def index(max_docs=None):
    OUT.mkdir(exist_ok=True)
    docs = documents(sqlite3.connect(f"file:{DB}?mode=ro", uri=True))[:max_docs]
    n_tr = (len(docs) + PAR_TRANCHE - 1) // PAR_TRANCHE
    print(f"{len(docs)} documents, {sum(len(d[0]) for d in docs)} passages, {n_tr} tranches", flush=True)
    t0, faits = time.time(), 0
    for i in range(n_tr):
        f = OUT / f"tranche_{i:04d}.npz"
        lot = docs[i * PAR_TRANCHE:(i + 1) * PAR_TRANCHE]
        if f.exists():
            continue
        E = np.vstack([encoder_doc(t) for _, t in lot])
        np.savez(f, rowids=np.array([r for rs, _ in lot for r in rs]), E=E.astype(np.float16))
        faits += sum(len(rs) for rs, _ in lot)
        dt = time.time() - t0
        print(f"tranche {i + 1}/{n_tr} · {faits} passages en {dt / 60:.1f} min · {faits / dt:.1f} p/s", flush=True)


def charger():
    fs = sorted(OUT.glob("tranche_*.npz"))
    R = np.concatenate([np.load(f)["rowids"] for f in fs])
    E = np.vstack([np.load(f)["E"] for f in fs]).astype(np.float32)
    return R, E / np.linalg.norm(E, axis=1, keepdims=True)


def evaluer():
    import sqlite_vec
    sys.path.insert(0, str(ROOT))
    import recherche
    rc = sqlite3.connect(ROOT / "anapath_rag.db"); rc.enable_load_extension(True); sqlite_vec.load(rc)
    tc = sqlite3.connect(ROOT / "anapath_terms.db")
    R, E = charger()
    couvert = set(R.tolist())
    poids = {r: (1.0 if st == "book" else 0.8) for r, st in rc.execute("SELECT rowid, source_type FROM chunk_meta")}
    textes = dict(rc.execute("SELECT rowid, chunk_text FROM chunk_meta"))

    def bio(vs):          # KNN BioLORD, top 40 par variante
        return [[r for r, _ in rc.execute("SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? AND k=40",
                                          (struct.pack(f"{len(v)}f", *v),))] for v in recherche.enc(vs)]

    def pp(vs):           # cosinus pplx, top 40 par variante
        Q = np.asarray(modele().encode_queries([[v] for v in vs], normalize_embeddings=True), dtype=np.float32)
        return [R[np.argsort(-(E @ q.reshape(-1)))[:40]].tolist() for q in Q]

    def classer(q, dense, hybride):
        var = recherche.expand(q, tc)[1] if hybride else [q]
        rang, best = {}, {}
        for lst in dense(var):
            for i, r in enumerate(lst):
                best[r] = min(i, best.get(r, 99))
        for i, r in enumerate(sorted(best, key=best.get)):
            rang[r] = 1 / (60 + i)
        if hybride:       # même BM25 + MeSH que recherche.search
            fts = " OR ".join('"' + t.replace('"', "") + '"' for t in var)
            for i, (r,) in enumerate(rc.execute("SELECT rowid FROM chunk_fts WHERE chunk_fts MATCH ? ORDER BY rank LIMIT 40", (fts,))):
                rang[r] = rang.get(r, 0) + 1 / (60 + i)
        return sorted(rang, key=lambda r: -rang[r] * poids.get(r, 1))[:40]

    systemes = {"BioLORD seul": (bio, False), "pplx seul": (pp, False),
                "hybride BioLORD (actuel)": (bio, True), "hybride pplx": (pp, True)}
    gold = list(csv.DictReader(open(OUT / "gold.tsv"), delimiter="\t"))
    res, lignes = {}, []
    for g in gold:
        pats = [re.compile(p.strip(), re.I) for p in g["criteres"].split(";;")]
        pert = {r for r in couvert if all(p.search(textes[r]) for p in pats)}
        for langue, q in (("FR", g["question_fr"]), ("EN", g["query_en"])):
            for nom, (dense, hyb) in systemes.items():
                top = classer(q, dense, hyb)
                rg = next((i + 1 for i, r in enumerate(top) if r in pert), None)
                res.setdefault((nom, langue), []).append(rg)
                lignes.append(f"{g['id']:14s} {langue} {nom:26s} rang {rg or '>40'}  ({len(pert)} pertinents)")
    out = ["Recherche seule, 20 questions (pplx/gold.tsv), " + f"{len(couvert)} passages indexés par pplx", "",
           f"{'système':28s} {'langue':6s} {'hit@4':>6s} {'hit@10':>7s} {'MRR@40':>7s}"]
    for (nom, langue), rgs in res.items():
        h4 = sum(1 for x in rgs if x and x <= 4); h10 = sum(1 for x in rgs if x and x <= 10)
        out.append(f"{nom:28s} {langue:6s} {h4:>3d}/{len(rgs)} {h10:>4d}/{len(rgs)} {np.mean([1 / x if x else 0 for x in rgs]):7.3f}")
    rapport = "\n".join(out + ["", "Détail (rang du 1er passage pertinent) :"] + lignes)
    (OUT / "eval.txt").write_text(rapport)
    print(rapport)
    return rapport


def _selfcheck():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE chunk_meta (rowid INTEGER PRIMARY KEY, source_short TEXT, chapter_title TEXT, chunk_text TEXT)")
    con.executemany("INSERT INTO chunk_meta VALUES (?,?,?,?)",
                    [(1, "a", "c1", "x" * 30000), (2, "a", "c1", "y" * 20000), (3, "a", "c2", "z"), (4, "b", "c1", "w")])
    d = documents(con)
    assert [rs for rs, _ in d] == [[1], [2], [3], [4]], d       # fenêtre dépassée, puis chapitre, puis livre


if __name__ == "__main__":
    _selfcheck()
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["index", "eval"])
    ap.add_argument("--max-docs", type=int)
    ap.add_argument("--base", choices=["acp", "foeto"], default="acp")
    a = ap.parse_args()
    if a.base == "foeto":
        DB, OUT, SQL = FOETO
    index(a.max_docs) if a.action == "index" else evaluer()
