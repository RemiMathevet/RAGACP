#!/usr/bin/env python3
"""Construit le paquet téléchargeable : base vecteurs + verbatims, terminologie, fiches -> dist/ragacp_<date>.zip.

    python acp_paquet.py

Contenu : anapath_rag.db (passages, vecteurs BioLORD-2023-M, FTS, pages imprimées, éditions), anapath_terms.db
(ADICAP + MeSH), fiches/*.md et leurs JSON (éléments avec verbatim et source). Jamais ask_log.db (questions posées).
Copie cohérente des bases par l'API backup de SQLite (le site peut lire pendant ce temps). Servi par web_acp.py
sur /dl/<jeton> (acp_tokens.py), un jeton = un téléchargement. Les anciens paquets sont supprimés.
"""
import sqlite3, tempfile, time, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
LISEZMOI = """RAGACP — paquet de données (usage personnel, non commercial, non redistribuable)

anapath_rag.db    passages de livres et de cours (texte), vecteurs BioLORD-2023-M (sqlite-vec), index FTS5,
                  pages imprimées (chunk_pages), éditions et années (source_info)
anapath_terms.db  ADICAP 2024 (ANS) + MeSH FR/EN (Wikidata, CC0) et leur jointure
fiches/           fiches de synthèse (.md) et éléments vérifiés (.json : phrase, verbatim, source, page)

Les passages sont des extraits d'ouvrages sous droits d'auteur de leurs éditeurs : usage personnel de
consultation seulement, ne pas redistribuer. Libre Pathology : CC BY-NC-SA 4.0 (attribution, pas d'usage
commercial, partage à l'identique). Ce n'est pas un outil diagnostique.
Code : https://github.com/RemiMathevet/RAGACP
"""


def construire():
    DIST.mkdir(exist_ok=True)
    sortie = DIST / time.strftime("ragacp_%Y%m%d.zip")
    tmp = sortie.with_suffix(".part")
    with tempfile.TemporaryDirectory(dir=DIST) as d, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for nom in ("anapath_rag.db", "anapath_terms.db"):
            copie = Path(d) / nom
            src, dst = sqlite3.connect(ROOT / nom), sqlite3.connect(copie)
            src.backup(dst); dst.close(); src.close()
            z.write(copie, nom)
        for f in sorted((ROOT / "fiches").rglob("*")):
            if f.is_file() and f.suffix in (".md", ".json", ".tsv"):
                z.write(f, f.relative_to(ROOT))
        z.writestr("LISEZMOI.txt", LISEZMOI)
    tmp.rename(sortie)
    for vieux in DIST.glob("ragacp_*.zip"):
        if vieux != sortie:
            vieux.unlink()
    print(f"{sortie} : {sortie.stat().st_size / 1e6:.0f} Mo")
    return sortie


def dernier():
    return max(DIST.glob("ragacp_*.zip"), default=None)


if __name__ == "__main__":
    construire()
