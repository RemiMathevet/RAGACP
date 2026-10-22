#!/usr/bin/env python3
"""Pages citables pour les chunks de livres de rag_v2.db (plan 2d27053f987e, étape 1a).

Les chunks n'ont pas de page (page_start vide). On relit chaque PDF page par page (PyMuPDF), on normalise
(lettres et chiffres seuls, sans accents ni ligatures ni césures, insensible aux espaces et aux colonnes) et
on retrouve 3 ancres de 60 caractères par chunk (début, milieu, fin) dans le texte du livre. Page = celle de
l'ancre la plus tôt ; page_fin = la plus tardive.

Page citée = numéro IMPRIMÉ, pas l'index PDF : étiquettes de page du PDF si présentes, sinon décalage
détecté sur les numéros en tête/pied de page (mode de imprimé − index sur le livre). Sans décalage fiable,
page_label reste vide et seul l'index PDF est donné (marqué).

Écrit la table chunk_pages (chunk_meta n'est pas modifiée) et un rapport des chunks non appariés.
Le livre -> PDF est trouvé par les ancres elles-mêmes (le PDF qui contient le plus d'ancres d'un livre).

    python pages_chunks.py [--rapport pages_rapport.tsv]     (sauvegarde de la base avant écriture)
"""
import argparse
import collections
import re
import shutil
import sqlite3
import time
import unicodedata
from pathlib import Path

import fitz

ICI = Path(__file__).resolve().parent
DB = ICI / "rag_v2.db"
B = Path.home() / "Bureau"
PDFS = sorted({p.resolve() for d in (B / "HPO_Foeto/P620", B / "livres_placenta", B / "Livres", B)
               for p in d.glob("*.pdf")})
ANCRE = 60


def norm(s):
    s = unicodedata.normalize("NFKD", s.replace("-\n", "").replace("­", ""))
    return re.sub(r"[^a-z0-9]", "", s.encode("ascii", "ignore").decode().lower())


def pages_pdf(p):
    """(texte normalisé du livre, début de chaque page dans ce texte, étiquette imprimée par page ou None)."""
    doc = fitz.open(p)
    morceaux, debuts, o, num = [], [], 0, []
    labels = [doc[i].get_label() for i in range(len(doc))]
    for i, pg in enumerate(doc):
        t = pg.get_text()
        n = norm(t)
        debuts.append(o); morceaux.append(n); o += len(n)
        lignes = [l.strip() for l in t.splitlines() if l.strip()]
        bord = lignes[:3] + lignes[-3:]
        num.append([int(l) for l in bord if re.fullmatch(r"\d{1,4}", l)])
    doc.close()
    if any(labels) and sum(bool(l) for l in labels) > 0.5 * len(labels):
        return "".join(morceaux), debuts, [l or None for l in labels], "etiquettes_pdf"
    # décalage LOCAL (planches intercalées : devneuro passe de -11 à +5) : sur ±6 pages, le décalage
    # imprimé − index le plus fréquent, retenu s'il est porté par au moins 3 pages voisines
    dec = [[n - (i + 1) for n in ns] for i, ns in enumerate(num)]
    labels, n_ok = [], 0
    for i in range(len(num)):
        c = collections.Counter(x for j in range(max(0, i - 6), min(len(num), i + 7)) for x in dec[j])
        if c and c.most_common(1)[0][1] >= 3 and i + 1 + c.most_common(1)[0][0] >= 1:
            labels.append(str(i + 1 + c.most_common(1)[0][0])); n_ok += 1
        else:
            labels.append(None)
    if n_ok >= 0.3 * len(num):
        return "".join(morceaux), debuts, labels, f"décalage local ({n_ok}/{len(num)} pages)"
    return "".join(morceaux), debuts, [None] * len(num), "aucun numéro imprimé fiable"


def ancres(txt):
    n = norm(txt)
    if len(n) < ANCRE:
        return [n] if len(n) >= 25 else []
    return [n[:ANCRE], n[len(n) // 2 - ANCRE // 2: len(n) // 2 + ANCRE // 2], n[-ANCRE:]]


def page_de(debuts, pos):
    import bisect
    return bisect.bisect_right(debuts, pos) - 1


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--rapport", default=str(ICI / "pages_rapport.tsv"))
    a = a.parse_args()
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT rowid, source_short, chunk_text FROM chunk_meta WHERE source_type='book'").fetchall()
    par_livre = collections.defaultdict(list)
    for r, s, t in rows:
        par_livre[s].append((r, t))
    t0 = time.time()
    textes = {}
    for p in PDFS:
        try:
            textes[p] = pages_pdf(p)
        except Exception as e:
            print(f"  illisible {p.name[:60]} : {e}")
    print(f"{len(textes)} PDF lus en {time.time() - t0:.0f} s", flush=True)
    sortie, rapport = [], []
    for livre, ch in sorted(par_livre.items()):
        ech = [x for r, t in ch[:: max(1, len(ch) // 40)] for x in ancres(t)[:1]]
        score = {p: sum(x in tx[0] for x in ech) for p, tx in textes.items()}
        pdf = max(score, key=score.get)
        if score[pdf] < 0.3 * len(ech):
            print(f"{livre:16s} aucun PDF ({score[pdf]}/{len(ech)} ancres) -> non apparié")
            rapport += [(livre, r, "", "pdf introuvable") for r, _ in ch]
            continue
        tx, debuts, labels, mode = textes[pdf]
        ok = 0
        for r, t in ch:
            pos = [tx.find(x) for x in ancres(t)]
            pos = [q for q in pos if q >= 0]
            if not pos:
                rapport.append((livre, r, pdf.name, "aucune ancre")); continue
            p0, p1 = page_de(debuts, min(pos)), page_de(debuts, max(pos))
            if p1 - p0 > 15:                     # ancres dispersées : ancre isolée tombée ailleurs
                p1 = p0
            sortie.append((r, pdf.name, p0 + 1, p1 + 1, labels[p0], labels[p1], len(pos)))
            ok += 1
        print(f"{livre:16s} {ok:5d}/{len(ch):5d} ({100 * ok / len(ch):5.1f} %)  {pdf.name[:55]}  | pages : {mode}", flush=True)
    sauve = DB.with_name(DB.stem + time.strftime(".bak-pages-%Y%m%d-%H%M%S.db"))
    shutil.copy(DB, sauve)
    con.execute("DROP TABLE IF EXISTS chunk_pages")
    con.execute("CREATE TABLE chunk_pages (rowid INTEGER PRIMARY KEY, pdf TEXT, page_pdf INTEGER, page_pdf_fin INTEGER, "
                "page_label TEXT, page_label_fin TEXT, n_ancres INTEGER)")
    con.executemany("INSERT INTO chunk_pages VALUES (?,?,?,?,?,?,?)", sortie)
    con.commit()
    Path(a.rapport).write_text("livre\trowid\tpdf\tmotif\n" + "".join("\t".join(map(str, x)) + "\n" for x in rapport))
    print(f"\n{len(sortie):,}/{len(rows):,} chunks avec page ; {len(rapport):,} non appariés -> {a.rapport} ; "
          f"sauvegarde {sauve.name}")


if __name__ == "__main__":
    main()
