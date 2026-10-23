#!/usr/bin/env python3
"""Assemble une fiche markdown depuis anapath_fiches.db (source de vérité, 23/10 ; voir fiches_db.py).

    python assemble_fiche.py 5R1 fiches/fiche_5R1_rein_natif.md [--titre "5.R.1 — Rein natif, biopsie médicale"]

Chaque élément : phrase française, verbatim entre guillemets, [source, p. imprimée]. Marques :
(cours) = source de rang inférieur ; ⚠ ajusté = mot d'étiquette ignoré au contrôle ; ✱ source corrigée.
Codes : MeSH par recherche.expand (lexical puis cosinus ≥ 0,90), ADICAP par adicap_mesh (auto / probable /
équivalent selon Qwen) — affichés avec leur statut, à valider, jamais présentés comme arbitrés.
"""
import argparse, re, sqlite3, time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CHAMPS = [("definition", "Définition"), ("critere_positif", "Critères positifs (MO)"), ("mesure", "À mesurer / compter"),
          ("distribution", "Distribution"), ("gradation", "Gradation / classification"),
          ("if_attendue", "Immunofluorescence"), ("me_attendue", "Microscopie électronique"),
          ("differentiel", "Diagnostic différentiel"), ("technique", "Technique qui tranche"),
          ("ne_pas_conclure", "Pièges / ne pas conclure")]
CHAMPS_GEN = [("definition", "Définition"), ("critere_positif", "Critères positifs (MO)"), ("architecture", "Architecture / topographie"),
              ("mesure", "À mesurer / compter"), ("gradation", "Grade / stade / classification"), ("ihc", "Immunohistochimie"),
              ("moleculaire", "Biologie moléculaire"), ("differentiel", "Diagnostic différentiel"),
              ("compte_rendu", "Compte rendu"), ("ne_pas_conclure", "Pièges / ne pas conclure")]
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
    if str(e.get("relecteur", "")).startswith("P"):
        marque.append("◐ soutien partiel")
    if e.get("seuils"):
        marque.append("⚠ seuil " + e["seuils"].split(" : ")[0])
    v = " ".join(e["verbatim"].split())
    return f"- {e['fr']}  \n  « *{v}* » [{e['source']}, p. {e.get('page') or '?'}] {' '.join(marque)}".rstrip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("code", help="5DIG1, 5R1… (table fiches d'anapath_fiches.db)"); ap.add_argument("sortie")
    ap.add_argument("--titre", help="sinon celui de la base")
    a = ap.parse_args()
    import fiches_db
    fc = fiches_db.connect()
    fc.row_factory = sqlite3.Row
    f = fc.execute("SELECT * FROM fiches WHERE code=?", (a.code,)).fetchone()
    if not f:
        raise SystemExit(f"fiche {a.code} absente d'anapath_fiches.db (python fiches_db.py importe)")
    if a.titre:
        fc.execute("UPDATE fiches SET titre=? WHERE code=?", (a.titre, a.code)); fc.commit()
    titre = a.titre or f["titre"]
    pref = re.sub(r"^5(\D+)(\d+)$", r"5.\1.\2", a.code)
    tc = sqlite3.connect(ROOT / "anapath_terms.db")
    src, garde, rej = Counter(), 0, 0
    corps, fam = [], None
    for r in fc.execute("SELECT * FROM entites WHERE fiche=? ORDER BY num", (a.code,)).fetchall():
        if r["famille"] != fam:
            fam = r["famille"]
            corps.append(f"\n## {fam}\n")
        corps.append(f"### {pref}.{r['num']} {r['entite_fr']}\n")
        els = fc.execute("SELECT * FROM elements WHERE entite_id=? AND statut IN ('actif','rejete') "
                         "ORDER BY champ, ordre, id", (r["id"],)).fetchall()
        if not els:
            corps.append("*non extraite*\n")
            continue
        corps.append(f"*Chapitre de référence : {r['livre_pivot'] or 'colvin_kidney'} — {r['chapitre_titre']}*"
                     f"{' · ' + r['modele'] if r['modele'] else ''}{' · relecteur : ' + r['relecteur'] if r['relecteur'] else ''}\n")
        cs = codes(r["entite_fr"], tc)
        if cs:
            for m, lab, ad in cs:
                adt = " ; ".join(f"{c} {l.lower()} ({canal}/{st or ''}{'·qwen=' + q if q else ''})"
                                 for c, l, canal, st, q in ad[:3]) or "aucun ADICAP équivalent"
                corps.append(f"**Codes (à valider)** — MeSH {m} *{lab}* · ADICAP : {adt}\n")
        else:
            corps.append("**Codes** — aucun MeSH rattaché (à rattacher à la main)\n")
        actifs = [dict(e) for e in els if e["statut"] == "actif"]
        rej += len(els) - len(actifs)
        for k, t in {"foie": CHAMPS_FOIE, "rein": CHAMPS}.get(f["organe"], CHAMPS_GEN):
            ch = [e for e in actifs if e["champ"] == k]
            if ch:
                corps.append(f"**{t}**\n" + "\n".join(ligne(e) for e in ch) + "\n")
                src.update(e["source"] for e in ch)
                garde += len(ch)
    tete = [f"# FICHE {titre}", "",
            f"`version :` 0.1 (extraction automatique) · `générée le :` {time.strftime('%Y-%m-%d %H:%M')} · "
            f"`relue le :` — · `état :` brouillon", "",
            "Chaque ligne : phrase française, puis le verbatim **copié mot pour mot** de la source et contrôlé "
            "contre le texte du livre, puis [source, page imprimée]. *(cours)* = source de rang inférieur ; "
            "⚠ ajusté = un mot d'étiquette (« Class », « Type ») ajouté par l'extracteur a été ignoré au contrôle ; "
            "✱ = la source citée a été corrigée. Les codes ADICAP/MeSH sont des **propositions**.", "",
            f"**Éléments gardés : {garde} · rejetés au contrôle verbatim : {rej}**", "",
            "**Sources citées** : " + ", ".join(f"{s} ({n})" for s, n in src.most_common())]
    Path(a.sortie).write_text("\n".join(tete) + "\n" + "\n".join(corps), encoding="utf-8")
    print(f"{a.sortie} : {a.code}, {garde} éléments, {rej} rejetés, {len(src)} sources")


if __name__ == "__main__":
    main()
