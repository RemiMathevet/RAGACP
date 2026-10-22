#!/usr/bin/env python3
"""Découpe le corpus anapath générale en chapitres, à partir des SIGNETS du PDF.

    python split_anapath.py --dry
    python split_anapath.py [--book robbins_cotran]

livres/<pdf>  -> chapitres/livres/<cle>/chNNN_slug.txt   (source_type 'book')
cours/<pdf>   -> chapitres/cours/<cle>/ch001_slug.txt    (source_type 'cours', document entier)

Corpus AUTONOME : rien de partagé avec la fœto (rag_v2.db) ni le placenta (placenta_rag.db).
Disposition attendue par Embedding_RAG_V2/embed_biolord.py (--chapitres).

Unité = signet FEUILLE de niveau <= level : un en-tête qui a des enfants à ce niveau
(« Part I », « SECTION 1 », « Volume 1 ») est sauté, ses enfants sont pris. Couvre les
tables à profondeur mixte (Diagnostic Pathology : « Chapter 72.Meningioma » au niveau 3,
entités au niveau 4 ailleurs). Numérotation = rang dans la liste (pas le numéro du titre,
qui manque ou se répète) ; un fichier déjà écrit n'est jamais réécrit.
"""
import argparse, re, unicodedata, zlib
from pathlib import Path
import fitz

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "chapitres"

# cle -> (début du nom de fichier dans livres/, niveau de signet). level None = pas de signets.
BOOKS = {
    "copath2012":       ("CoPath", 1),
    "robbins_cotran":   ("Robbins, Cotran & Kumar Pathologic Basis of Disease, 11th", 1),   # 11e (2026) ; 10e écartée
    "robbins_basic":    ("(Robbins Pathology)", 1),
    "oxford_handbook":  ("(Oxford Medical Handbooks)", 2),
    "netter_patho":     ("Buja M.", 1),
    "wheater_patho":    ("(Wheater's", 2),
    "colvin_kidney":    ("[Diagnostic Pathology] Robert B Colvin", 2),
    "zhang_lung":       ("(Atlas of Anatomic Pathology) Chen Zhang", 1),
    "leslie_lung":      ("(Pattern Recognition) Kevin O. Leslie", 1),
    "popper_lung":      ("Popper, Murer", 1),
    "connolly_autopsy": ("Andrew J Connolly", None),
    "kleinschmidt_neuro": ("Neuropathology (B.K. Kleinschmidt", 4),
    "greenfield_neuro": ("Greenfield", 2),
}

# Liminaires, annexes, index : pas du contenu médical.
SKIP = re.compile(
    r"^\s*(preface|acknowledg|contents?\b|table (of contents|des mati)|chapter contents|contributors?|"
    r"contributing|image contributors|index|references|further reading|suggested reading|appendix|"
    r"about\s+the|copyright|glossary|list\s+of|symbols|dedications?|front\s*cover|back\s*cover|cover\b|"
    r"ifc|ibc|endsheet|inside front|imprint|series preface|what is new|sections?\s*$|bonus|"
    r"avant[- ]propos|pr[ée]face|les auteurs|compl[ée]ments en ligne|liste des|corrig[ée]|"
    r"pattern recognition series|netterapp|greenbookad|half title|title page|instructions for online|"
    r"online resources|abbreviations|selected references|[A-Z]\s*$)", re.I)
PREFIX = re.compile(r"^\s*(chapitre|chapter)\s*\d+\s*[:.\-–]?\s*", re.I)
MIN_CAR = 2000     # page de titre, faux-titre : < 2000 caractères


def slug(title, n=48):
    s = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    s = re.sub(r"^\s*(chapitre|chapter)?\s*\d+\s*[:.\-–]?\s*", "", s, flags=re.I)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    return s[:n].rstrip("_") or "chapitre"


def chapters(doc, level):
    """(slug, page_debut, page_fin) des signets feuilles de niveau <= level."""
    toc = [t for t in doc.get_toc() if t[0] <= level]
    out = []
    for i, (lv, title, page) in enumerate(toc):
        nxt = toc[i + 1] if i + 1 < len(toc) else None
        if (nxt and nxt[0] > lv) or SKIP.match(PREFIX.sub("", title)) or page < 1:
            continue
        end = next((p2 - 1 for lv2, _, p2 in toc[i + 1:] if lv2 <= lv), len(doc))
        out.append((slug(title), page, max(page, end)))
    return out


LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"})


def auto_level(doc):
    """Niveau le plus FIN dont les unités font >= 3 pages en médiane (et >= 8 unités) ; None sans signets.
    Règle appliquée de fait aux 13 premiers livres : chapitre, ou entité pour les Diagnostic Pathology."""
    best = repli = None
    for lv in range(1, 7):
        u = chapters(doc, lv)
        if len(u) >= 8:
            med = sorted(p2 - p1 + 1 for _, p1, p2 in u)[len(u) // 2]
            if med >= 3:
                best = lv
            elif med >= 1 and repli is None:
                repli = lv          # entités d'1-2 pages (livres bleus OMS) : mieux qu'un bloc unique
    return best or repli


def page_text(doc, i):
    try:
        return doc[i].get_text()
    except RuntimeError:                       # page corrompue (« object is not a stream ») : vide, on continue
        return ""


def text_of(doc, p1, p2):
    """Ligatures dépliées : sinon « ﬁbrosis » échappe au FTS (cas Ashworth côté fœto)."""
    return "\n\n".join(page_text(doc, i) for i in range(p1 - 1, min(p2, len(doc)))).translate(LIGATURES)


COLLECTIONS = [("bis_biopsy_interpretation_of_the_", "bis_"), ("bis_biopsy_interpretation_of_", "bis_"),
               ("diagnostic_pathology_", "dp_"), ("diagnostic_pahtology_", "dp_"), ("dp_", "dp_"),
               ("who_classification_of_tumours_of_the_", "who_"), ("who_classification_of_tumours_", "who_"),
               ("who_classification_of_tumors_of_", "who_"), ("who_classification_of_", "who_"), ("hyp_", "hyp_")]


def book_key(stem):
    """Clé courte et distincte : préfixe de collection remplacé par un tag (bis_, dp_, who_, hyp_)."""
    k = slug(stem, 200)
    for long, tag in COLLECTIONS:
        if k.startswith(long):
            k = tag + k[len(long):]
            break
    return k[:40].rstrip("_")


def jobs(only):
    """(cle, pdf, level, sous-dossier) ; level None = document entier ; 'auto' = auto_level()."""
    pdfs = sorted(p for p in (ROOT / "livres").glob("*.pdf") if not p.name.endswith(".ocr_tmp.pdf"))
    connus = set()
    for key, (prefix, level) in BOOKS.items():
        hit = [p for p in pdfs if p.name.startswith(prefix)]
        if len(hit) != 1:
            print(f"{key:20s} {len(hit)} PDF pour « {prefix} » — ignoré")
            continue
        connus.add(hit[0])
        yield key, hit[0], level, "livres"
    vus = set(BOOKS)
    for p in pdfs:
        if p not in connus:
            k = book_key(p.stem)
            if k in vus:
                k = f"{k[:35]}_{zlib.crc32(p.name.encode()) % 10000:04d}"
            vus.add(k)
            yield k, p, "auto", "livres"
    vus = set()
    for p in sorted(p for p in (ROOT / "cours").rglob("*.pdf") if not p.name.endswith(".ocr_tmp.pdf")):   # fichiers de travail d'ocr_scans
        k = slug(p.stem)[:40]
        if k in vus:                                   # même nom dans deux sous-dossiers
            k = f"{k}_{zlib.crc32(str(p.relative_to(ROOT)).encode()) % 10000:04d}"   # crc32 : stable entre exécutions
        vus.add(k)
        yield k, p, None, "cours"


def _selfcheck():
    assert "ﬁbrosis".translate(LIGATURES) == "fibrosis"
    assert book_key("BIS-Biopsy Interpretation of the Kidney and Adrenal Gland") == "bis_kidney_and_adrenal_gland"
    assert book_key("DP-Breast") == "dp_breast"
    assert slug("Chapter 72.Meningioma") == "meningioma"
    assert slug("Chapitre 3: La réaction inflammatoire") == "la_reaction_inflammatoire"
    assert slug("43: Decidual Vasculopathy") == "decidual_vasculopathy"
    for t in ("Index", "Front Cover", "IFC1", "Further Reading", "Table des matières", "B",
              "Avant propos", "Chapter Contents", "Dedications", "Chapitre 12: Corrigé des entraînements"):
        assert SKIP.match(PREFIX.sub("", t)), t
    for t in ("Chapter 72.Meningioma", "Bacterial Infections", "2 Cellular responses to injury",
              "Chapitre 2: Lésions élémentaires", "Breast", "Infarction"):
        assert not SKIP.match(t), t
    # feuilles : la section qui a des enfants saute, ses enfants restent
    class D(list):
        def get_toc(self): return [[1, "Part I", 1], [2, "Glioma", 1], [2, "Meningioma", 5],
                                   [1, "Breast", 9]]
    assert chapters(D([0] * 12), 2) == [("glioma", 1, 4), ("meningioma", 5, 8), ("breast", 9, 12)]


def main():
    _selfcheck()
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--book", help="ne traiter que cette clé")
    args = ap.parse_args()

    total = 0
    for key, pdf, level, kind in jobs(args.book):
        if args.book and args.book != key:
            continue
        doc = fitz.open(pdf)
        if level == "auto":
            level = auto_level(doc)
        items = chapters(doc, level) if level else [(slug(pdf.stem), 1, len(doc))]
        if level and not items:                       # signets inexploitables
            items = [(slug(pdf.stem), 1, len(doc))]
        d = OUT / kind / key
        if kind == "livres" or args.book:
            print(f"{kind}/{key:32s} {len(doc):5d}p | niveau {level} | {len(items):4d} unités", flush=True)
        for n, (sl, p1, p2) in enumerate(items, 1):
            f = d / f"ch{n:03d}_{sl}.txt"
            if f.exists():
                continue
            txt = text_of(doc, p1, p2)
            if len(txt.strip()) < MIN_CAR:
                print(f"    {f.name}  IGNORÉ (trop court ou sans couche texte, {len(txt.strip())} car.)")
                continue
            if args.dry:
                if n <= 3 or n == len(items):
                    print(f"    {f.name}  p{p1}-{p2}  {len(txt):7d} car.  [dry]")
                continue
            d.mkdir(parents=True, exist_ok=True)
            f.write_text(txt, encoding="utf-8")
            total += 1
        doc.close()
    print(f"\n{total} unités écrites dans {OUT}")


if __name__ == "__main__":
    main()
