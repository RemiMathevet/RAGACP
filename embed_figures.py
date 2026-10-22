#!/usr/bin/env python3
"""Vecteurs image des figures de livres -> anapath_figures.db, table figures_vec (id, modele, vec float16 normé).

    python embed_figures.py [--modele uni2] [--lot 64]
    python embed_figures.py --cherche image.jpg [--k 10]      # recherche par image (cosinus)

UNI2-h en attendant notre ViT ouvert (décision de Rémi, 22/10) : la colonne `modele` garde l'index remplaçable,
un autre encodeur s'ajoute à côté sans rien écraser. Banc livre <-> web (PREFECT 8b55c6034a84) : UNI2-h 75 %
de bonne entité, D4v2 / DINOv2 au hasard. Vue = image entière, petit côté ramené à 224 px, centre 224×224.
Reprend là où il s'est arrêté (figures déjà encodées pour ce modèle sautées).
"""
import argparse, sqlite3, sys
from pathlib import Path
import numpy as np
import torch
from PIL import Image

ROOT = Path(__file__).resolve().parent
DB = ROOT / "anapath_figures.db"
sys.path.insert(0, "/home/mathevet/Bureau/FoetoPath_Luminarium_V2/Lumi/embeddings")
import embed_multimag as em  # noqa: E402

CHARGEURS = {"uni2": em.load_uni2}
MEAN, STD = torch.tensor(em.IMAGENET_MEAN).view(3, 1, 1), torch.tensor(em.IMAGENET_STD).view(3, 1, 1)


def ouvrir(p):
    """Pillow lit mal une partie des JPEG 2000 sortis des PDF (13 % du corpus, 22/10) : repli MuPDF, qui les décode."""
    try:
        return Image.open(p).convert("RGB")
    except Exception:
        import fitz
        px = fitz.Pixmap(str(p))
        if px.n - px.alpha != 3:
            px = fitz.Pixmap(fitz.csRGB, px)
        return Image.frombytes("RGBA" if px.alpha else "RGB", (px.width, px.height), px.samples).convert("RGB")


def vue(p):
    im = ouvrir(p)
    w, h = im.size
    s = 224 / min(w, h)
    im = im.resize((max(224, round(w * s)), max(224, round(h * s))), Image.BICUBIC)
    w, h = im.size
    im = im.crop(((w - 224) // 2, (h - 224) // 2, (w - 224) // 2 + 224, (h - 224) // 2 + 224))
    t = torch.from_numpy(np.asarray(im).copy()).permute(2, 0, 1).float() / 255.0
    return (t - MEAN) / STD


@torch.no_grad()
def encoder(model, paths):
    t = torch.stack([vue(p) for p in paths]).cuda()
    with torch.autocast("cuda", torch.float16):
        v = model(t).float()
    return torch.nn.functional.normalize(v, dim=-1).cpu().numpy().astype(np.float16)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modele", default="uni2")
    ap.add_argument("--lot", type=int, default=64)
    ap.add_argument("--cherche")
    ap.add_argument("--k", type=int, default=10)
    a = ap.parse_args()
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS figures_vec (id INTEGER, modele TEXT, vec BLOB, PRIMARY KEY (id, modele))")
    model = CHARGEURS[a.modele]("cuda")
    if a.cherche:
        q = encoder(model, [a.cherche])[0].astype(np.float32)
        ids, V = zip(*[(i, np.frombuffer(v, np.float16)) for i, v in
                       con.execute("SELECT id, vec FROM figures_vec WHERE modele=?", (a.modele,))])
        s = np.stack(V).astype(np.float32) @ q
        for j in np.argsort(-s)[:a.k]:
            r = con.execute("SELECT livre, page_label, figure, panneau, coalesce(sous_legende, legende), fichier "
                            "FROM figures WHERE id=?", (ids[j],)).fetchone()
            print(f"{s[j]:.3f}  {r[0]} p.{r[1]} fig.{r[2]}{r[3] or ''}  {(r[4] or '')[:90]}  [{r[5]}]")
        return
    todo = con.execute("SELECT f.id, f.fichier FROM figures f WHERE NOT EXISTS (SELECT 1 FROM figures_vec v "
                       "WHERE v.id=f.id AND v.modele=?) ORDER BY f.id", (a.modele,)).fetchall()
    print(f"{len(todo)} figures à encoder ({a.modele})", flush=True)
    ko = 0
    for i in range(0, len(todo), a.lot):
        lot = todo[i:i + a.lot]
        try:
            V = encoder(model, [ROOT / f for _, f in lot])
            con.executemany("INSERT OR REPLACE INTO figures_vec VALUES (?,?,?)",
                            [(fid, a.modele, v.tobytes()) for (fid, _), v in zip(lot, V)])
        except Exception:                                  # une image illisible ne bloque pas le lot : une à une
            for fid, f in lot:
                try:
                    con.execute("INSERT OR REPLACE INTO figures_vec VALUES (?,?,?)",
                                (fid, a.modele, encoder(model, [ROOT / f])[0].tobytes()))
                except Exception as e:
                    ko += 1
                    print(f"  illisible {f} : {str(e)[:80]}", flush=True)
        con.commit()
        if i // a.lot % 50 == 0:
            print(f"  {i + len(lot)}/{len(todo)}", flush=True)
    print(f"fini : {len(todo) - ko} encodées, {ko} illisibles", flush=True)


if __name__ == "__main__":
    main()
