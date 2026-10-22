#!/usr/bin/env python3
"""Compare deux extractions de fiche (dossiers de JSON d'extract_fiche.py), entité par entité.

    python comparer_fiches.py fiches/rein_natif_qwen_relu fiches/rein_natif_albert [--noms Qwen DeepSeek]

Par entité : éléments gardés, rejetés au contrôle verbatim, retirés par le relecteur (C/H), soutien partiel (P),
champs remplis sur 10, et recouvrement des verbatims (même phrase du livre citée par les deux).
"""
import argparse, glob, json, os
from extract_fiche import norm


def charge(d):
    out = {}
    for f in sorted(glob.glob(f"{d}/*.json")):
        j = json.load(open(f))
        els = [e for v in j["champs"].values() for e in v]
        rej = j["rejetes"]
        out[os.path.basename(f)] = {
            "ent": j["entite"]["entite_fr"], "garde": len(els),
            "rej": sum(1 for e in rej if "relecteur" not in e), "relu": sum(1 for e in rej if "relecteur" in e),
            "P": sum(1 for e in els if str(e.get("relecteur", "")).startswith("P")),
            "champs": sum(1 for v in j["champs"].values() if v),
            "v": {norm(e["verbatim"]) for e in els}}
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("a"); ap.add_argument("b")
    ap.add_argument("--noms", nargs=2, default=["A", "B"])
    x = ap.parse_args()
    A, B = charge(x.a), charge(x.b)
    na, nb = x.noms
    print(f"{'entité':38s} | {na:^24s} | {nb:^24s} | communs")
    print(f"{'':38s} | {'gardé rej relu P champs':24s} | {'gardé rej relu P champs':24s} |")
    tot = {k: [0, 0] for k in ("garde", "rej", "relu", "P", "champs")}
    com = 0
    for f in sorted(set(A) | set(B)):
        a, b = A.get(f), B.get(f)
        if not (a and b):
            print(f"{f}: absent d'un côté"); continue
        c = sum(1 for v in a["v"] if any(v in w or w in v for w in b["v"]))
        com += c
        for k in tot:
            tot[k][0] += a[k]; tot[k][1] += b[k]
        f5 = lambda d: f"{d['garde']:5d} {d['rej']:3d} {d['relu']:4d} {d['P']:2d} {d['champs']:4d}/10"
        print(f"{a['ent'][:38]:38s} | {f5(a):24s} | {f5(b):24s} | {c}")
    print("-" * 110)
    for k, (ta, tb) in tot.items():
        print(f"TOTAL {k:7s} {na}: {ta:5d}   {nb}: {tb:5d}")
    print(f"verbatims communs (même passage cité, recouvrement) : {com}")
    for nom, t in ((na, tot["garde"][0] + tot["rej"][0] + tot["relu"][0]), (nb, tot["garde"][1] + tot["rej"][1] + tot["relu"][1])):
        i = 0 if nom == na else 1
        print(f"{nom} : {t} proposés, {100 * tot['rej'][i] / max(t, 1):.0f} % rejetés au contrôle, "
              f"{100 * tot['relu'][i] / max(t, 1):.0f} % retirés par le relecteur")
