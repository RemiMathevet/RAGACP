#!/usr/bin/env python3
"""Figures des livres -> figures/<livre>/*.<ext natif> + anapath_figures.db (table figures), une ligne par PANNEAU.

    python figures_anapath.py [--livre <motif>] [--pages 100-160]

Chaque panneau garde : livre (source_short), pdf, page PDF, page imprimée, n° de figure, lettre de panneau,
légende entière, sous-légende du panneau, titre au-dessus (Diagnostic Pathology), bbox, image NATIVE (aucun
réencodage). Deux mises en page :
  - « Fig. N » (Springer, HYP, BIS, WHO…) : légende sous / à côté des images ; lettres a, b, c en texte posé sur
    le panneau, sinon ordre de lecture (methode='ordre') ;
  - Diagnostic Pathology : légende en marge « (Left) … (Right) … », titre de chaque panneau au-dessus.
Lettres INCRUSTÉES dans les pixels (une seule image, plusieurs sous-légendes) : panneau='*', incruste=1, à découper
plus tard. Une légende longue se vérifie sur l'image : pdftotext peut tresser deux colonnes (PREFECT 9f5dc1476aa3).
"""
import argparse, collections, hashlib, re, sqlite3
from pathlib import Path
import fitz

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "figures"
DB = ROOT / "anapath_figures.db"
MIN_PT = 60                         # côté minimal d'une image (points) : écarte puces, logos, pictos
SCHEMA = """CREATE TABLE IF NOT EXISTS figures (
    id INTEGER PRIMARY KEY, livre TEXT, pdf TEXT, page_pdf INT, page_label TEXT, figure TEXT, panneau TEXT,
    legende TEXT, sous_legende TEXT, titre TEXT, x0 REAL, y0 REAL, x1 REAL, y1 REAL, largeur_px INT, hauteur_px INT,
    fichier TEXT, sha1 TEXT, methode TEXT, incruste INT DEFAULT 0, UNIQUE(pdf, page_pdf, x0, y0, xref));
CREATE INDEX IF NOT EXISTS i_fig ON figures(livre, figure);"""
FIG = re.compile(r"^\s*(?:Fig(?:ure)?|FIG(?:URE)?)\.?\s*(\d+(?:[.\-–]\d+)*[A-Za-z]?)[.:]?\s+")
DPPOS = re.compile(r"\((Left|Right|Top|Bottom|Upper left|Upper right|Lower left|Lower right|Top left|Top right|"
                   r"Bottom left|Bottom right|Middle left|Middle right|Center)\)", re.I)
# marqueurs de panneau dans une légende : « (a) », « (A) », « a, », « A: », « A and B, » en tête de phrase
PAN = re.compile(r"(?:\(([a-hA-H](?:\s*(?:,|and|–|-)\s*[a-hA-H])*)(?:\)|(?=[,;]\s))|(?:(?<=^)|(?<=[.;]\s)|(?<=\)\s))([A-Ha-h])(?:\s*(?:and|–|-|,)\s*[A-Ha-h])*[,:.]\s)")


def livres_pdf(con):
    """pdf -> source_short, lu dans la base (chunk_pages.rowid = chunk_meta.rowid)."""
    return dict(con.execute("SELECT p.pdf, m.source_short FROM chunk_pages p JOIN chunk_meta m ON m.rowid = p.rowid "
                            "WHERE m.source_type='book' GROUP BY p.pdf"))


def sous_legendes(leg):
    """{lettre: texte} ; texte commun avant la première lettre sous la clé ''. Deux styles :
    en tête (« (a) Low power. (b) … », « A and B, … » : le texte SUIT la lettre) et en renvoi
    (« fibrochondroid matrix (A), chicken-wire calcifications (B) » : le texte PRÉCÈDE la lettre)."""
    ms = list(PAN.finditer(leg))
    if sum(len(re.findall(r"\b[A-Ha-h]\b", m.group(0))) for m in ms) < 2:
        return {}
    out, fin_prec = {"": leg[:ms[0].start()].strip()}, 0
    for k, m in enumerate(ms):
        avant = leg[:m.start()].rstrip()
        prefixe = (f := FIG.match(avant + " ")) and f.end() >= len(avant)     # « Fig 3. (A) … » : en tête
        renvoi = m.group(1) and avant and avant[-1] not in ".;:" and not prefixe
        if renvoi:
            debut = max(fin_prec, max(avant.rfind(c) for c in ",;.") + 1)
            seg = leg[debut:m.end()]
        else:
            seg = leg[m.start():ms[k + 1].start() if k + 1 < len(ms) else len(leg)]
        for l in re.findall(r"\b[A-Ha-h]\b", m.group(0)):
            out.setdefault(l.lower(), seg.strip(" ,;"))
        fin_prec = m.end()
    return out


def recouvre(a0, a1, b0, b1):
    return max(0, min(a1, b1) - max(a0, b0)) / max(1e-6, min(a1 - a0, b1 - b0))


def images(doc, page, freq):
    vus, out = set(), []
    W, H = page.rect.width, page.rect.height
    for x in page.get_image_info(xrefs=True):
        x0, y0, x1, y1 = x["bbox"]
        if x1 - x0 < MIN_PT or y1 - y0 < MIN_PT or not x["xref"] or freq[x["xref"]] > 3:
            continue
        if (x1 - x0) > 0.9 * W and (y1 - y0) > 0.9 * H:          # page scannée entière : pas une figure
            continue
        if y1 < 0.16 * H and (x1 - x0) > 0.8 * W:                 # bandeau de tête
            continue
        k = tuple(round(v) for v in x["bbox"])
        if k in vus:                                              # même cadre en double (masque, calque)
            continue
        vus.add(k)
        out.append({"bbox": (x0, y0, x1, y1), "xref": x["xref"]})
    return out


def tailles(page):
    """bbox arrondie d'un bloc -> taille de police de sa DERNIÈRE ligne (le corps d'une légende, pas « Fig. » en gras)."""
    return {tuple(round(v) for v in b["bbox"]): round(b["lines"][-1]["spans"][-1]["size"], 1)
            for b in page.get_text("dict")["blocks"] if b.get("type") == 0 and b["lines"] and b["lines"][-1]["spans"]}


def legendes_fig(blocks, taille=None):
    """Blocs « Fig. N … » + blocs suivants collés dessous (même colonne) ou en tête de la colonne voisine ;
    une suite qui reprend plus haut dans la colonne de droite (sous une image) est reconnue à sa police."""
    caps, pris = [], set()
    taille = taille or {}
    tb = lambda b: taille.get(tuple(round(v) for v in b[:4]))
    for i, b in enumerate(blocks):
        if m := FIG.match(b[4]):
            txt, bas, droite = b[4], b[3], b[2]
            for c in blocks[i + 1:] + blocks[:i]:      # la suite peut commencer un poil plus haut (668,0 < 668,4)
                if FIG.match(c[4]) or id(c) in pris:
                    continue
                dessous = 0 <= c[1] - bas < 6 and recouvre(b[0], droite, c[0], c[2]) > 0.6
                colonne = abs(c[1] - b[1]) < 5 and 0 <= c[0] - droite < 30     # suite dans la colonne voisine (Springer)
                suite = (c[0] >= droite - 5 and tb(c) and tb(c) == tb(b) and re.match(r"\s*[a-z(]", c[4])
                         and i not in pris and id(c) not in pris)
                if dessous or colonne or suite:
                    txt, bas, droite = txt + " " + c[4], max(bas, c[3]), max(droite, c[2])
                    pris.add(id(c))
                    if suite:
                        break
            caps.append({"fig": m.group(1), "bbox": (b[0], b[1], droite, bas), "texte": " ".join(txt.split())})
    return caps


def assigner(img, caps, lettre=None):
    """Légende la plus proche SOUS l'image (même colonne) ou À CÔTÉ (même hauteur). Un panneau qui porte une
    lettre va d'abord aux légendes qui annoncent cette lettre (sinon la légende voisine d'une figure simple le prend)."""
    x0, y0, x1, y1 = img["bbox"]
    best, dmin = None, 1e9
    if lettre and (annoncent := [c for c in caps if lettre in sous_legendes(c["texte"])]):
        if r := assigner(img, annoncent):
            return r
    for c in caps:
        cx0, cy0, cx1, cy1 = c["bbox"]
        if recouvre(x0, x1, cx0, cx1) > 0.3 and cy0 >= y1 - 8:
            d = cy0 - y1
        elif recouvre(y0, y1, cy0, cy1) > 0.6 and min(abs(cx0 - x1), abs(x0 - cx1)) < 30:
            d = min(abs(cx0 - x1), abs(x0 - cx1)) + 20
        else:
            continue
        if d < dmin:
            best, dmin = c, d
    return best


def lettre_posee(img, mots):
    """Lettre seule posée sur le panneau, la plus proche du coin haut gauche ; « a | b » est une seule ligne
    pour deux panneaux, d'où les MOTS et non les lignes."""
    x0, y0, x1, y1 = img["bbox"]
    seules = collections.defaultdict(list)                      # ligne -> mots ; une ligne de lettres seules seulement
    for w in mots:
        seules[w[5], w[6]].append(w[4])
    c = [(abs(a0 - x0) + abs(b0 - y0), t.lower()) for a0, b0, a1, b1, t, bl, li, _ in mots
         if all(re.fullmatch(r"[a-hA-H]", u) for u in seules[bl, li])
         and x0 - 4 <= a0 and a1 <= x1 + 4 and y0 - 4 <= b0 and b1 <= y1 + 4]
    return min(c)[1] if c else None


def lignes(page):
    return [(" ".join(s["text"] for s in l["spans"]).strip(), l["bbox"])
            for b in page.get_text("dict")["blocks"] if b.get("type") == 0 for l in b["lines"]]


def page_dp(page, imgs, lines, blocks):
    """Diagnostic Pathology : légende en marge (Left)/(Right), titre au-dessus de chaque panneau."""
    lines = lines[1]
    rows = []
    for b in blocks:
        if DPPOS.search(b[4]):
            leg = " ".join(b[4].split())
            parts = {m.group(1).lower(): s for m, s in zip(DPPOS.finditer(leg),
                     [leg[a.start():(n.start() if n else len(leg))] for a, n in
                      zip(DPPOS.finditer(leg), list(DPPOS.finditer(leg))[1:] + [None])])}
            rows.append((b, leg, parts))
    out = []
    for img in imgs:
        x0, y0, x1, y1 = img["bbox"]
        row = next((r for r in rows if recouvre(y0, y1, r[0][1], r[0][3]) > 0.3), None)
        voisins = sorted((i for i in imgs if recouvre(y0, y1, i["bbox"][1], i["bbox"][3]) > 0.5), key=lambda i: i["bbox"][0])
        pos = ("left" if voisins[0] is img else "right") if len(voisins) == 2 else None
        titre = " ".join(t for t, bb in lines if 0 <= y0 - bb[3] < 22 and recouvre(x0, x1, bb[0], bb[2]) > 0.5)
        out.append((img, None, pos, row[1] if row else None, (row[2].get(pos) if row and pos else None), titre, "dp"))
    return out


def page_fig(page, imgs, lines, blocks):
    lines = lines[0]
    caps = legendes_fig(blocks, tailles(page))
    groupes = collections.defaultdict(list)
    for img in imgs:
        groupes[id(c) if (c := assigner(img, caps, lettre_posee(img, lines))) else None].append((img, c))
    # orphelin portant une lettre posée -> la légende de la page qui annonce cette lettre et ne l'a pas encore
    for img, _ in list(groupes.get(None, [])):
        l = lettre_posee(img, lines)
        pris = {id(c): {lettre_posee(i, lines) for i, _ in groupes.get(id(c), [])} for c in caps}
        voisine = lambda c: l and l not in pris[id(c)] and {chr(ord(l) - 1), chr(ord(l) + 1)} & pris[id(c)]
        if l and (c := next((c for c in caps if l in sous_legendes(c["texte"]) and l not in pris[id(c)]), None)
                  or next((c for c in caps if voisine(c)), None)):
            groupes[None].remove((img, None))
            groupes[id(c)].append((img, c))
    if not groupes.get(None):
        groupes.pop(None, None)
    out = []
    for k, lst in groupes.items():
        if not lst:
            continue
        cap = lst[0][1]
        subs = sous_legendes(cap["texte"]) if cap else {}
        ordre = sorted(lst, key=lambda t: (round(t[0]["bbox"][1] / 30), t[0]["bbox"][0]))
        for j, (img, _) in enumerate(ordre):
            l, meth = lettre_posee(img, lines), "lettre"
            if not l and len(ordre) > 1 and subs:
                l, meth = "abcdefgh"[j], "ordre"
            if not l and len(ordre) == 1 and len(subs) > 2:          # une image, plusieurs sous-légendes
                out.append((img, cap and cap["fig"], "*", cap["texte"], None, None, "incruste"))
                continue
            out.append((img, cap and cap["fig"], l, cap and cap["texte"], subs.get(l) if l else None, None,
                        meth if cap else "sans_legende"))
    return out


def traiter(pdf, livre, con, pages=None):
    doc = fitz.open(pdf)
    rng = range(*pages) if pages else range(len(doc))
    freq = collections.Counter(x[0] for i in rng for x in doc[i].get_images())   # logos répétés
    dp = sum(bool(DPPOS.search(doc[i].get_text())) for i in list(rng)[::max(1, len(rng) // 30)]) > 3
    d = OUT / livre
    d.mkdir(parents=True, exist_ok=True)
    n = 0
    for i in rng:
        page = doc[i]
        imgs = images(doc, page, freq)
        if not imgs:
            continue
        lines, blocks = (page.get_text("words"), lignes(page)), sorted(page.get_text("blocks"), key=lambda b: (b[1], b[0]))
        for img, fig, pan, leg, sub, titre, meth in (page_dp if dp else page_fig)(page, imgs, lines, blocks):
            x = doc.extract_image(img["xref"])                    # octets natifs : aucune recompression
            sha = hashlib.sha1(x["image"]).hexdigest()
            f = d / f"p{i + 1:04d}_x{img['xref']}.{x['ext']}"
            if not f.exists():
                f.write_bytes(x["image"])
            con.execute("INSERT OR REPLACE INTO figures (livre, pdf, page_pdf, page_label, figure, panneau, legende, "
                        "sous_legende, titre, x0, y0, x1, y1, largeur_px, hauteur_px, fichier, sha1, methode, incruste, xref) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (livre, Path(pdf).name, i + 1, page.get_label() or str(i + 1), fig, pan, leg, sub, titre,
                         *img["bbox"], x["width"], x["height"], str(f.relative_to(ROOT)), sha, meth,
                         int(meth == "incruste"), img["xref"]))
            n += 1
    con.commit()
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--livre", default="")
    ap.add_argument("--pages", default="")
    a = ap.parse_args()
    assert sous_legendes("Fig. 4.47 Diffuse damage. (a) Low power. (b) GMS stain shows cysts.") == \
        {"": "Fig. 4.47 Diffuse damage.", "a": "(a) Low power.", "b": "(b) GMS stain shows cysts."}
    assert set(sous_legendes("Fig 1. Imaging. A, On radiograph, sclerosis. B, Low signal on T1.")) == {"", "a", "b"}
    assert sous_legendes("Fig. 2 A single image of tumor.") == {}
    s7 = sous_legendes("Fig 7. A and B, In classic examples, the small cell component.")
    assert s7["a"] == s7["b"] == "A and B, In classic examples, the small cell component.", s7
    s3 = sous_legendes("Fig 3. The triad include fibrochondroid matrix (A), chicken-wire calcifications (B), and giant cells (C).")
    assert sous_legendes("Fig. 6.31 Eosinophilic pneumonia. (a, b) Two views.")["b"] == "(a, b) Two views."
    assert s3["b"] == "chicken-wire calcifications (B)" and s3["a"].endswith("fibrochondroid matrix (A)"), s3
    rag = sqlite3.connect(f"file:{ROOT / 'anapath_rag.db'}?mode=ro", uri=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA.replace("UNIQUE(pdf", "xref INT, UNIQUE(pdf"))
    pg = tuple(int(x) for x in a.pages.split("-")) if a.pages else None
    for pdf, livre in sorted(livres_pdf(rag).items()):
        if a.livre.lower() in pdf.lower() and (ROOT / "livres" / pdf).exists():
            print(f"{livre:45s} {traiter(ROOT / 'livres' / pdf, livre, con, pg)} panneaux", flush=True)
