#!/usr/bin/env python3
"""Assemble une fiche markdown à partir des JSON d'extract_fiche.py.

    python assemble_fiche.py fiches/entites_5R1.tsv fiches/rein_natif fiches/fiche_5R1_rein_natif.md \
        --titre "5.R.1 — Rein natif, biopsie médicale"

Chaque élément : phrase française, verbatim entre guillemets, [source, p. imprimée]. Marques :
(cours) = source de rang inférieur ; ⚠ ajusté = mot d'étiquette ignoré au contrôle ; ✱ source corrigée.
Codes : MeSH par recherche.expand (lexical puis cosinus ≥ 0,90), ADICAP par adicap_mesh (auto / probable /
équivalent selon Qwen) — affichés avec leur statut, à valider, jamais présentés comme arbitrés.
"""
import argparse, csv, json, re, sqlite3, time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CHAMPS = [("definition", "Définition"), ("critere_positif", "Critères positifs (MO)"), ("mesure", "À mesurer / compter"),
          ("distribution", "Distribution"), ("gradation", "Gradation / classification"),
          ("if_attendue", "Immunofluorescence"), ("me_attendue", "Microscopie électronique"),
          ("differentiel", "Diagnostic différentiel"), ("technique", "Technique qui tranche"),
          ("ne_pas_conclure", "Pièges / ne pas conclure")]
CHAMPS_FOIE = [("definition", "Définition"), ("critere_positif", "Critères positifs (MO)"), ("topographie", "Topographie"),
               ("mesure", "À mesurer / compter"), ("gradation", "Score / classification"),
               ("colorations", "Colorations spéciales"), ("ihc", "Immunohistochimie"),
               ("differentiel", "Diagnostic différentiel"), ("technique", "Examen qui tranche"),
               ("ne_pas_conclure", "Pièges / ne pas conclure")]


def codes(ent, tc):
    from recherche import expand
    mesh, _ = expand(ent, tc)
    out = []
    for m in mesh:
        fr, en = tc.execute("SELECT label_fr, label_en FROM mesh WHERE mesh_id=?", (m,)).fetchone() or (None, None)
        ad = tc.execute("""SELECT a.dict || ' ' || a.code, a.libelle, j.canal, j.statut, j.relation_qwen
                           FROM adicap_mesh j JOIN adicap a USING (uri) WHERE j.mesh_id=?
                           AND (j.canal='lexical' OR j.relation_qwen='equivalent') ORDER BY j.canal DESC""",
                        (m,)).fetchall()
        out.append((m, fr or en, ad))
    return out


def ligne(e):
    marque = []
    if e.get("type") == "cours":
        marque.append("(cours)")
    if e.get("verbatim_ajuste"):
        marque.append("⚠ ajusté")
    if e.get("src_corrigee"):
        marque.append("✱")
    v = " ".join(e["verbatim"].split())
    return f"- {e['fr']}  \n  « *{v}* » [{e['source']}, p. {e.get('page') or '?'}] {' '.join(marque)}".rstrip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tsv"); ap.add_argument("dossier"); ap.add_argument("sortie")
    ap.add_argument("--titre", default="Fiche")
    ap.add_argument("--organe", choices=["rein", "foie"], default="rein")
    a = ap.parse_args()
    tc = sqlite3.connect(ROOT / "anapath_terms.db")
    ents = list(csv.DictReader(open(a.tsv), delimiter="\t"))
    js = {int(f.name[:2]): json.load(open(f)) for f in Path(a.dossier).glob("*.json")}
    src, garde, rej = Counter(), 0, 0
    corps, fam = [], None
    for r in ents:
        d = js.get(int(r["num"]))
        if r["famille"] != fam:
            fam = r["famille"]
            corps.append(f"\n## {fam}\n")
        corps.append(f"### 5.R.1.{r['num']} {r['entite_fr']}\n")
        if not d:
            corps.append("*non extraite*\n")
            continue
        corps.append(f"*Chapitre de référence : {d.get('livre_pivot', 'colvin_kidney')} — {d['colvin_titre']}*"
                     f"{' · ' + d['modele'] if d.get('modele') else ''}{' · relecteur : ' + d['relecteur'] if d.get('relecteur') else ''}\n")
        cs = codes(r["entite_fr"], tc)
        if cs:
            for m, lab, ad in cs:
                adt = " ; ".join(f"{c} {l.lower()} ({canal}/{st or ''}{'·qwen=' + q if q else ''})"
                                 for c, l, canal, st, q in ad[:3]) or "aucun ADICAP équivalent"
                corps.append(f"**Codes (à valider)** — MeSH {m} *{lab}* · ADICAP : {adt}\n")
        else:
            corps.append("**Codes** — aucun MeSH rattaché (à rattacher à la main)\n")
        for k, titre in (CHAMPS_FOIE if a.organe == "foie" else CHAMPS):
            els = d["champs"].get(k)
            if els:
                corps.append(f"**{titre}**\n" + "\n".join(ligne(e) for e in els) + "\n")
                src.update(e["source"] for e in els)
                garde += len(els)
        rej += len(d["rejetes"])
    tete = [f"# FICHE {a.titre}", "",
            f"`version :` 0.1 (extraction automatique) · `générée le :` {time.strftime('%Y-%m-%d %H:%M')} · "
            f"`relue le :` — · `état :` brouillon", "",
            "Chaque ligne : phrase française, puis le verbatim **copié mot pour mot** de la source et contrôlé "
            "contre le texte du livre, puis [source, page imprimée]. *(cours)* = source de rang inférieur ; "
            "⚠ ajusté = un mot d'étiquette (« Class », « Type ») ajouté par l'extracteur a été ignoré au contrôle ; "
            "✱ = la source citée a été corrigée. Les codes ADICAP/MeSH sont des **propositions**.", "",
            f"**Éléments gardés : {garde} · rejetés au contrôle verbatim : {rej}**", "",
            "**Sources citées** : " + ", ".join(f"{s} ({n})" for s, n in src.most_common())]
    Path(a.sortie).write_text("\n".join(tete) + "\n" + "\n".join(corps), encoding="utf-8")
    print(f"{a.sortie} : {len(ents)} entités, {garde} éléments, {rej} rejetés, {len(src)} sources")


if __name__ == "__main__":
    main()
