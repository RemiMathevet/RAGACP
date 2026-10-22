#!/usr/bin/env python3
"""OCR de nuit des scans (refs/tri_entrantes.tsv, colonne ocr=oui) par ocrmypdf (Tesseract fra+eng).

    python ocr_scans.py [--workers 8] [--dry]

Choix (test du 2026-10-21 sur 3 pages : OMS Œil 3 colonnes, EPU Sein 2021, diapositive) : ocrmypdf plutôt
que Qwen3.6-35B vision. Ses erreurs sont visibles (« 34BE12 ») là où Qwen réécrit en silence
(« {243} » -> « [243] »), et la couche texte est DANS le PDF : split, pages imprimées et verbatims ne changent pas.
--skip-text : une page qui a déjà du texte est laissée telle quelle. L'original est déplacé dans
ocr_originaux/<même chemin> ; le PDF OCRisé prend sa place. Relançable (fichier déjà fait = sauté).
Fin : un mail sans pièce jointe (08_notify).
"""
import argparse, os, csv, importlib.util, shutil, subprocess, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ORIG = ROOT / "ocr_originaux"
NOTIFY, MAIL = os.environ.get("ACP_NOTIFY"), os.environ.get("ACP_MAIL")   # module send_email(sujet, corps, à) ; facultatif


def un(chemin):
    src = ROOT / chemin
    orig = ORIG / chemin
    if orig.exists():
        return chemin, "déjà fait", 0
    if not src.exists():
        return chemin, "absent", 0
    tmp = src.with_suffix(".ocr_tmp.pdf")
    t = time.time()
    r = subprocess.run(["ocrmypdf", "-l", "fra+eng", "--skip-text", "--jobs", "2", "-q", str(src), str(tmp)],
                       capture_output=True, text=True)
    if r.returncode not in (0, 6) or not tmp.exists():          # 6 = déjà du texte partout
        tmp.unlink(missing_ok=True)
        return chemin, f"échec {r.returncode}: {r.stderr.strip()[-200:]}", time.time() - t
    orig.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(src, orig)
    shutil.move(tmp, src)
    return chemin, "ok", time.time() - t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    rows = [r for r in csv.DictReader(open(ROOT / "refs" / "tri_entrantes.tsv"), delimiter="\t") if r["ocr"] == "oui"]
    rows.sort(key=lambda r: (r["dest"] != "livres", -int(r["pages"] or 0)))    # livres d'abord, gros d'abord
    print(f"{len(rows)} scans, {sum(int(r['pages'] or 0) for r in rows)} pages", flush=True)
    if a.dry:
        return
    t0, res = time.time(), []
    with ThreadPoolExecutor(a.workers) as ex:
        for chemin, etat, dt in ex.map(un, [r["chemin"] for r in rows]):
            res.append((chemin, etat))
            print(f"{etat:10s} {dt:6.0f} s  {chemin[:90]}", flush=True)
    ok = sum(e == "ok" for _, e in res)
    ech = [c for c, e in res if e.startswith("échec")]
    corps = (f"OCR ocrmypdf fra+eng : {ok}/{len(res)} scans OCRisés en {(time.time() - t0) / 60:.0f} min.\n"
             f"Échecs : {len(ech)}\n" + "\n".join(ech[:30]) +
             f"\n\nOriginaux : {ORIG}/\n"
             "Suite : split_anapath.py puis embed_anapath.py (cours) et pages_anapath.py.")
    print(corps)
    if NOTIFY and MAIL:
        spec = importlib.util.spec_from_file_location("notify", NOTIFY)
        n = importlib.util.module_from_spec(spec); spec.loader.exec_module(n)
        n.send_email(f"[anapath] OCR de nuit : {ok}/{len(res)} scans", corps, MAIL)


if __name__ == "__main__":
    main()
