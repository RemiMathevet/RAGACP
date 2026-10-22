#!/usr/bin/env python3
"""Reconstruit anapath_terms.db DE ZÉRO : ADICAP (squelette FR) + MeSH FR/EN (Wikidata) + jointure.

    python build_terms.py [--no-cosinus]

Sources (refs/) :
  ADICAP_2024.xlsx            terminologie ADICAP 2024-10 (ANS) — D3 organes, D4 non tumoral, D5 tumoral,
                              D6 pathologie particulière des organes. Copie du zip officiel, PAS adicap.db
                              (qui porte des tables fœto : rien de la fœto n'entre ici).
  wikidata_mesh_fr_en.tsv     libellés + synonymes fr/en des descripteurs MeSH (Wikidata, CC0)
  wikidata_mesh_tree.tsv      codes d'arbre MeSH ; on garde A (anatomie) et C (maladies)

Jointure ADICAP -> MeSH, deux canaux indépendants :
  1. lexical  : forme normalisée identique (accents, casse, ponctuation, mots vides, pluriels, ordre
                des mots, qualifiants ADICAP « SAI / NCA ») sur libellés ET synonymes FR -> statut 'auto'
  2. cosinus  : BioLORD-2023-M, ADICAP FR contre tous les termes MeSH FR+EN, top 1 ≥ 0,85 ->
                'probable' (≥ 0,95) ou 'a_valider'
Le cosinus ne valide jamais seul : il propose. La base est jetable (jamais éditée à la main) ; les
arbitrages de Rémi vivront dans une table à part, réappliquée à chaque reconstruction.
"""
import argparse, collections, csv, re, sqlite3, time, unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REFS = ROOT / "refs"
DB = ROOT / "anapath_terms.db"
MODEL = "FremyCompany/BioLORD-2023-M"
SEUIL_COS = 0.85        # 0,80-0,85 : surtout du bruit (KYSTE CONGENITAL -> paradental cyst)
SEUIL_PROBABLE = 0.95   # au-dessus : quasi-synonymes ; en dessous : souvent un terme plus large, à valider

STOP = {"de", "du", "des", "la", "le", "les", "l", "d", "un", "une", "et", "ou", "a", "au", "aux", "en",
        "the", "of", "and", "or", "in", "with", "by", "sai", "nca", "nos", "type", "sans", "autre", "precision"}


def norm(s):
    """Forme lexicale : ascii, minuscules, mots vides et qualifiants retirés, pluriel simple, mots triés."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    mots = [m for m in re.split(r"[^a-z0-9]+", s) if m and m not in STOP]
    mots = [m[:-1] if len(m) > 3 and m[-1] in "sx" else m for m in mots]
    return " ".join(sorted(mots))


def load_adicap():
    import openpyxl
    ws = openpyxl.load_workbook(REFS / "ADICAP_2024.xlsx", read_only=True).active
    out = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        uri, code, _, pcode, lib, plib, acode, alab, start, end, dic = (r + (None,) * 11)[:11]
        if dic and lib:
            out.append((uri, dic, str(code), str(pcode or ""), lib.strip(), (plib or "").strip(),
                        acode or "", alab or "", str(end or "")))
    return out


def load_mesh():
    unq = lambda x: x.strip().strip('"')
    tree = collections.defaultdict(set)
    with open(REFS / "wikidata_mesh_tree.tsv") as f:
        next(f)
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) == 2:
                tree[unq(p[0])].add(unq(p[1]))
    keep = {m for m, ts in tree.items() if any(t[:1] in "AC" for t in ts)}
    terms = []
    with open(REFS / "wikidata_mesh_fr_en.tsv") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) != 4:
                continue
            m, lang, kind = unq(p[0]), unq(p[1]), unq(p[2])
            t = re.sub(r'^"(.*)"@\w+$', r"\1", p[3]).replace('\\"', '"')
            if m in keep:
                terms.append((m, lang, kind, t))
    return tree, keep, terms


def build(cosinus=True):
    t0 = time.time()
    if DB.exists():
        DB.unlink()
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE adicap (uri TEXT PRIMARY KEY, dict TEXT, code TEXT, parent_code TEXT, libelle TEXT,
                             libelle_parent TEXT, anatomy_code TEXT, anatomy_label TEXT, end_date TEXT, norm TEXT);
        CREATE TABLE mesh (mesh_id TEXT PRIMARY KEY, trees TEXT, label_fr TEXT, label_en TEXT);
        CREATE TABLE mesh_terme (mesh_id TEXT, lang TEXT, kind TEXT, terme TEXT, norm TEXT);
        CREATE INDEX i_mt_norm ON mesh_terme(norm);
        CREATE INDEX i_mt_id ON mesh_terme(mesh_id);
        CREATE TABLE adicap_mesh (uri TEXT, mesh_id TEXT, canal TEXT, score REAL, terme TEXT, statut TEXT,
                                  PRIMARY KEY (uri, mesh_id, canal));
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    ad = load_adicap()
    con.executemany("INSERT INTO adicap VALUES (?,?,?,?,?,?,?,?,?,?)", [a + (norm(a[4]),) for a in ad])
    tree, keep, terms = load_mesh()
    lab = collections.defaultdict(dict)
    for m, lang, kind, t in terms:
        if kind == "label":
            lab[m][lang] = t
    con.executemany("INSERT INTO mesh VALUES (?,?,?,?)",
                    [(m, ",".join(sorted(tree[m])), lab[m].get("fr"), lab[m].get("en")) for m in sorted(keep)])
    con.executemany("INSERT INTO mesh_terme VALUES (?,?,?,?,?)", [t + (norm(t[3]),) for t in terms])
    print(f"ADICAP {len(ad)} | MeSH A+C {len(keep)} descripteurs, {len(terms)} termes "
          f"({sum(1 for m in keep if lab[m].get('fr'))} avec libellé FR)  [{time.time() - t0:.0f} s]")

    # canal 1 — lexical (ADICAP est français : on apparie aux termes FR, puis EN en secours)
    cibles = ("D3", "D4", "D5", "D6")
    rows = con.execute(f"SELECT uri, norm, libelle FROM adicap WHERE dict IN {cibles} AND norm != ''").fetchall()
    par_norm = collections.defaultdict(set)
    for m, lang, kind, t, n in con.execute("SELECT mesh_id, lang, kind, terme, norm FROM mesh_terme"):
        par_norm[n].add((m, t))
    lex = [(u, m, "lexical", 1.0, t, "auto") for u, n, _ in rows for m, t in par_norm.get(n, ())]
    con.executemany("INSERT OR IGNORE INTO adicap_mesh VALUES (?,?,?,?,?,?)", lex)
    print(f"lexical : {len({x[0] for x in lex})}/{len(rows)} concepts ADICAP appariés ({len(lex)} liens)")

    # canal 2 — cosinus BioLORD-M (propositions seulement)
    if cosinus:
        import numpy as np, torch
        from sentence_transformers import SentenceTransformer
        mod = SentenceTransformer(MODEL, device="cuda" if torch.cuda.is_available() else "cpu")
        mt = con.execute("SELECT mesh_id, terme FROM mesh_terme").fetchall()
        E = lambda xs: mod.encode(xs, batch_size=256, normalize_embeddings=True, show_progress_bar=False)
        em = E([t for _, t in mt])
        ea = E([lib.lower() for _, _, lib in rows])
        cos = []
        for i in range(0, len(ea), 2048):
            s = torch.from_numpy(ea[i:i + 2048]).cuda() @ torch.from_numpy(em).cuda().T
            v, j = s.max(1)
            cos += list(zip(v.cpu().numpy(), j.cpu().numpy()))
        prop = [(rows[k][0], mt[j][0], "cosinus", round(float(v), 4), mt[j][1],
                 "probable" if v >= SEUIL_PROBABLE else "a_valider")
                for k, (v, j) in enumerate(cos) if v >= SEUIL_COS]
        con.executemany("INSERT OR IGNORE INTO adicap_mesh VALUES (?,?,?,?,?,?)", prop)
        seul = {p[0] for p in prop} - {x[0] for x in lex}
        print(f"cosinus ≥ {SEUIL_COS} : {len(prop)} propositions, dont {len(seul)} concepts sans lien lexical")

    # verdicts Qwen (trier_qwen.py), fichier persistant hors base : relation typée, jamais 'auto'
    con.execute("ALTER TABLE adicap_mesh ADD COLUMN relation_qwen TEXT")
    vq = ROOT / "verdicts_qwen.tsv"
    if vq.exists():
        v = [(r["verdict"], r["uri"], r["mesh_id"]) for r in csv.DictReader(open(vq), delimiter="\t")]
        con.executemany("UPDATE adicap_mesh SET relation_qwen=? WHERE uri=? AND mesh_id=? AND canal='cosinus'", v)
        print(f"verdicts Qwen réappliqués : {len(v)}")

    con.executemany("INSERT INTO meta VALUES (?,?)", [("model", MODEL), ("seuil_cos", str(SEUIL_COS)),
                    ("built", time.strftime("%Y-%m-%d %H:%M"))])
    con.commit()
    for d, n, nl in con.execute(f"""SELECT a.dict, count(DISTINCT a.uri),
            count(DISTINCT CASE WHEN m.canal='lexical' THEN a.uri END)
            FROM adicap a LEFT JOIN adicap_mesh m USING (uri) WHERE a.dict IN {cibles} GROUP BY 1"""):
        print(f"  {d} : {n} concepts, {nl} avec lien lexical")
    con.close()
    print(f"{DB.name} reconstruite en {time.time() - t0:.0f} s")


def _selfcheck():
    assert norm("Hyalinose segmentaire et focale") == norm("HYALINOSE FOCALE ET SEGMENTAIRE")
    assert norm("GRANULOMES EPITHELIOIDES (SAI)") == norm("granulome épithélioïde")
    assert norm("Infarctus du myocarde") != norm("Infarctus du rein")


if __name__ == "__main__":
    _selfcheck()
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-cosinus", action="store_true")
    build(not ap.parse_args().no_cosinus)
