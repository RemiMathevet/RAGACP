#!/usr/bin/env python3
"""Télécharge Libre Pathology (CC BY-NC-SA 4.0) par l'API MediaWiki -> chapitres/cours/librepathology/.

    python fetch_librepathology.py [--max 0]

Couche 'cours' (wiki : interprétation, preuve inférieure au livre). Usage local non commercial, avec
attribution : chaque fichier commence par sa ligne de licence et l'URL de l'article. Une fiche qui
REPRODUIT ce texte hérite de la licence CC BY-NC-SA (à ne pas publier sous une autre licence).
Brut conservé dans refs/librepathology_raw.jsonl (wikitext + horodatage de révision). 1 requête/s, 50
articles par requête ; redirections ignorées ; relançable (un article déjà écrit n'est pas réécrit).
"""
import argparse, os, json, re, time
from pathlib import Path
import mwparserfromhell, requests

ROOT = Path(__file__).resolve().parent
API = "https://librepathology.org/w/api.php"
OUT = ROOT / "chapitres" / "cours" / "librepathology"
RAW = ROOT / "refs" / "librepathology_raw.jsonl"
UA = {"User-Agent": "anapath-generale/0.1 (local non-commercial research; " + os.environ.get("ACP_CONTACT", "contact unset") + ")"}
SKIP_SECTIONS = re.compile(r"^(see also|references|external links|further reading)$", re.I)


def pages():
    p = {"action": "query", "generator": "allpages", "gapnamespace": 0, "gapfilterredir": "nonredirects",
         "gaplimit": 50, "prop": "revisions", "rvprop": "content|timestamp", "rvslots": "main",
         "format": "json", "formatversion": 2}
    while True:
        for k in range(4):
            try:
                r = requests.get(API, params=p, headers=UA, timeout=60).json()
                break
            except (requests.RequestException, ValueError):
                time.sleep(20 * (k + 1))
        else:
            raise SystemExit("API Libre Pathology injoignable")
        for pg in r.get("query", {}).get("pages", []):
            rev = pg["revisions"][0]
            yield pg["title"], rev["timestamp"], rev["slots"]["main"]["content"]
        if "continue" not in r:
            return
        p.update(r["continue"])
        time.sleep(1)


def texte(wiki):
    """Wikitext -> texte : sections de bibliographie retirées, gabarits et liens aplatis."""
    code = mwparserfromhell.parse(wiki)
    for sec in code.get_sections(levels=[2, 3]):
        h = sec.filter_headings()
        if h and SKIP_SECTIONS.match(h[0].title.strip_code().strip()):
            try:
                code.remove(sec)
            except ValueError:
                pass
    t = code.strip_code(normalize=True, collapse=True)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def slug(t):
    return re.sub(r"[^A-Za-z0-9]+", "_", t).strip("_").lower()[:48] or "article"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=0, help="arrêter après N articles (test)")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    n = ecrits = courts = 0
    with open(RAW, "w") as raw:
        for title, ts, wiki in pages():
            n += 1
            raw.write(json.dumps({"title": title, "rev": ts, "wikitext": wiki}, ensure_ascii=False) + "\n")
            f = OUT / f"ch{n:04d}_{slug(title)}.txt"
            t = texte(wiki)
            if len(t) < 600:                                   # ébauches, pages de liste
                courts += 1
            elif not f.exists():
                url = "https://librepathology.org/wiki/" + title.replace(" ", "_")
                f.write_text(f"{title}\n[Libre Pathology, CC BY-NC-SA 4.0, {url}, révision {ts}]\n\n{t}\n",
                             encoding="utf-8")
                ecrits += 1
            if n % 500 == 0:
                print(f"  {n} articles", flush=True)
            if a.max and n >= a.max:
                break
    print(f"{n} articles lus, {ecrits} écrits, {courts} trop courts ignorés -> {OUT}")


if __name__ == "__main__":
    main()
