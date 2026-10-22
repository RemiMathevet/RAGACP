#!/usr/bin/env python3
"""Rejoue tests_rag.tsv dans la boucle « Demander » et compare (rag.md §3).

    python rejouer_tests.py [--tag base_v2]

Chaque question est journalisée dans ask_log.db (origine = test:<tag>:<id>). Tableau : citations vertes/orange/rouges,
requêtes refusées, livre attendu parmi les passages cités, durée. La note de fond (pièges) reste humaine : colonne note.
"""
import argparse, csv, json, sqlite3, threading, time
import ask_anapath as A

ap = argparse.ArgumentParser()
ap.add_argument("--tag", default=time.strftime("%Y%m%d-%H%M"))
ap.add_argument("--fichier", default="tests_rag.tsv")              # bench_acp.tsv : benchmark ACP (20 questions)
a = ap.parse_args()
verrou = threading.Lock()
lignes = []
for t in csv.DictReader(open(A.recherche.ROOT / a.fichier), delimiter="\t"):
    ask = {"q": t["question"], "status": "running", "steps": [], "t0": time.time(), "origine": f"test:{a.tag}:{t['id']}"}
    A.run(ask, verrou)
    h = ask.get("answer_html", "")
    cites = " ".join(p["title"] for p in ask.get("passages", []))
    bon = any(l in cites for l in t["livres_attendus"].split(",") if l)
    import re
    nus = int((re.search(r"(\d+) renvoi\(s\)", h) or [0, 0])[1])
    refus = sum(1 for s in ask["steps"] if s.get("refus"))
    nreq = sum(1 for s in ask["steps"] if s["type"] == "search")
    lignes.append((t["id"], h.count('"cit-ok"'), h.count('"cit-warn"'), h.count("cit-bad"), f"{refus}/{nreq}",
                   "oui" if bon else "NON", round(time.time() - ask["t0"]), ask["status"], nus))
    print(f"{t['id']:20s} vert {lignes[-1][1]:2d} orange {lignes[-1][2]:2d} rouge {lignes[-1][3]:2d} | refus {lignes[-1][4]} "
          f"| sans citation {ask.get('nus_avant', 0):2d}->{nus:2d} | livre attendu cité : {lignes[-1][5]:3s} | {lignes[-1][6]} s {ask['status']}", flush=True)
v = sum(l[1] for l in lignes); o = sum(l[2] for l in lignes); r = sum(l[3] for l in lignes)
print(f"TOTAL {a.tag} : vert {v} orange {o} rouge {r} sans citation {sum(l[8] for l in lignes)} ; livre attendu {sum(l[5] == 'oui' for l in lignes)}/{len(lignes)}")
