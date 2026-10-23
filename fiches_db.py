#!/usr/bin/env python3
"""Fiches anapath en base : anapath_fiches.db est la SOURCE DE VÉRITÉ (décision de Rémi, 23/10).
Les JSON d'extract_fiche.py ne sont plus qu'une archive d'extraction ; assemble_fiche.py lit la base.

    python fiches_db.py importe [digestif ...]       # JSON -> base (tous les organes de production par défaut)
    python fiches_db.py cherche "CD117" [--champ ihc] [--fiche 5DIG1] [--tous]
    python fiches_db.py corrige ID [--fr "..."] [--verbatim "..."] [--page 12] [--auteur remi] [--force]
    python fiches_db.py retire ID --motif "..." [--auteur remi]
    python fiches_db.py ajoute ENTITE_ID CHAMP --fr "..." --verbatim "..." --source S --page P [--auteur remi]

Rien ne s'efface : une correction retire l'ancienne ligne (statut 'remplace', remplace_par = nouvelle) ; un
rejet de l'extraction est gardé en statut 'rejete'. Un verbatim saisi à la main doit se trouver dans le livre
cité (anapath_rag.db), sinon refus (--force pour passer outre, marqué). Ré-importer un organe remplace les seules
lignes d'extraction : les corrections à la main restent actives. corrige / retire / ajoute réassemblent la fiche
.md touchée (servie telle quelle par acp.adeptus).
"""
import argparse, json, sqlite3, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "anapath_fiches.db"
# dossier de production -> (code, organe d'assemble_fiche) ; les dossiers d'essai (rein_natif_albert…) restent dehors
ORGANES = {"digestif": ("5DIG1", "digestif"), "foie_biopsie": ("5F1", "foie"), "orl": ("5ORL1", "orl"),
           "peau": ("5PEA1", "peau"), "poumon": ("5POU1", "poumon"), "prostate": ("5PRO1", "prostate"),
           "rein_natif": ("5R1", "rein"), "sein": ("5SEI1", "sein"), "snc": ("5SNC1", "snc"),
           "thyroide": ("5THY1", "thyroide"), "tissus_mous": ("5TMO1", "tissus_mous"), "uterus": ("5UTE1", "uterus"),
           "vessie": ("5VES1", "vessie")}

SCHEMA = """
CREATE TABLE IF NOT EXISTS fiches (code TEXT PRIMARY KEY, dossier TEXT, organe TEXT, titre TEXT);
CREATE TABLE IF NOT EXISTS entites (id INTEGER PRIMARY KEY, fiche TEXT REFERENCES fiches(code), num INTEGER,
    famille TEXT, entite_fr TEXT, entite_en TEXT, chapitre TEXT, chapitre_titre TEXT, livre_pivot TEXT,
    modele TEXT, relecteur TEXT, seuils TEXT, extrait_le TEXT, UNIQUE (fiche, num));
CREATE TABLE IF NOT EXISTS elements (id INTEGER PRIMARY KEY, entite_id INTEGER REFERENCES entites(id),
    champ TEXT, ordre INTEGER, fr TEXT, verbatim TEXT, source TEXT, type TEXT, page TEXT,
    verdict TEXT, relecteur TEXT, seuils TEXT, src_corrigee TEXT, verbatim_ajuste INTEGER DEFAULT 0,
    statut TEXT NOT NULL DEFAULT 'actif' CHECK (statut IN ('actif','rejete','retire','remplace')),
    motif TEXT, remplace_par INTEGER REFERENCES elements(id), auteur TEXT, maj TEXT);
CREATE INDEX IF NOT EXISTS idx_el_entite ON elements(entite_id, statut, champ, ordre);
CREATE INDEX IF NOT EXISTS idx_el_source ON elements(source, page);
CREATE VIRTUAL TABLE IF NOT EXISTS elements_fts USING fts5(fr, verbatim, content='elements', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS el_ai AFTER INSERT ON elements BEGIN
    INSERT INTO elements_fts(rowid, fr, verbatim) VALUES (new.id, new.fr, new.verbatim); END;
CREATE TRIGGER IF NOT EXISTS el_au AFTER UPDATE OF fr, verbatim ON elements BEGIN
    INSERT INTO elements_fts(elements_fts, rowid, fr, verbatim) VALUES ('delete', old.id, old.fr, old.verbatim);
    INSERT INTO elements_fts(rowid, fr, verbatim) VALUES (new.id, new.fr, new.verbatim); END;
CREATE VIEW IF NOT EXISTS v_elements AS SELECT e.id, f.code AS fiche, n.num, n.entite_fr, e.champ, e.ordre,
    e.fr, e.verbatim, e.source, e.page, e.verdict, e.statut, e.auteur, e.maj
    FROM elements e JOIN entites n ON n.id = e.entite_id JOIN fiches f ON f.code = n.fiche;
"""


def connect():
    con = sqlite3.connect(DB)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def maintenant():
    return time.strftime("%Y-%m-%d %H:%M")


def _ligne(e, statut="actif", motif=None):
    rel = str(e.get("relecteur") or "")
    return dict(fr=e.get("fr"), verbatim=e.get("verbatim"), source=e.get("source") or e.get("src"),
                type=e.get("type"), page=None if e.get("page") is None else str(e["page"]),
                verdict=rel[:1] if rel[:1] in "SPCH" and rel else None, relecteur=rel or None,
                seuils=e.get("seuils"), src_corrigee=e.get("src_corrigee"),
                verbatim_ajuste=int(bool(e.get("verbatim_ajuste"))), statut=statut, motif=motif)


def inserer(con, entite_id, champ, ordre, row, auteur, maj):
    cols = ["entite_id", "champ", "ordre", *row, "auteur", "maj"]
    return con.execute(f"INSERT INTO elements ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                       [entite_id, champ, ordre, *row.values(), auteur, maj]).lastrowid


def importer(con, dossier):
    """Un organe : JSON d'extraction -> base. Les lignes d'extraction précédentes passent en 'remplace'."""
    code, organe = ORGANES[dossier]
    md = ROOT / "fiches" / f"fiche_{code}_{dossier}.md"     # pas de glob : fiche_5R1_rein_natif_albert.md (essai)
    titre = md.read_text(encoding="utf-8").splitlines()[0].removeprefix("# FICHE ").strip() if md.exists() else code
    con.execute("INSERT INTO fiches VALUES (?,?,?,?) ON CONFLICT(code) DO UPDATE SET dossier=excluded.dossier, "
                "organe=excluded.organe", (code, dossier, organe, titre))
    import csv
    tsv = {int(r["num"]): r for r in csv.DictReader(open(ROOT / "fiches" / f"entites_{code}.tsv"), delimiter="\t")}
    js = {int(f.name[:2]): json.load(open(f)) for f in (ROOT / "fiches" / dossier).glob("*.json")}
    n_el = n_rej = 0
    for num, r in tsv.items():
        d = js.get(num, {})
        ent = d.get("entite", r)
        con.execute("""INSERT INTO entites (fiche, num, famille, entite_fr, entite_en, chapitre, chapitre_titre,
                       livre_pivot, modele, relecteur, seuils, extrait_le) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(fiche, num) DO UPDATE SET famille=excluded.famille, entite_fr=excluded.entite_fr,
                       entite_en=excluded.entite_en, chapitre=excluded.chapitre, chapitre_titre=excluded.chapitre_titre,
                       livre_pivot=excluded.livre_pivot, modele=excluded.modele, relecteur=excluded.relecteur,
                       seuils=excluded.seuils, extrait_le=excluded.extrait_le""",
                    (code, num, r["famille"], r["entite_fr"], ent.get("entite_en") or r.get("entite_en"),
                     ent.get("chapitre") or r.get("chapitre"), d.get("colvin_titre"), d.get("livre_pivot"),
                     d.get("modele"), d.get("relecteur"), d.get("seuils"), d.get("date")))
        eid = con.execute("SELECT id FROM entites WHERE fiche=? AND num=?", (code, num)).fetchone()[0]
        if not d:
            continue
        con.execute("UPDATE elements SET statut='remplace', motif='ré-extraction', maj=? WHERE entite_id=? "
                    "AND auteur='extraction' AND statut IN ('actif','rejete')", (maintenant(), eid))
        maj = d.get("date") or maintenant()
        # verbatims désavoués à la main (corrigés ou retirés) : une ré-extraction ne les ramène pas
        desavoues = {v for (v,) in con.execute("""SELECT verbatim FROM elements WHERE entite_id=? AND
            ((statut='remplace' AND remplace_par IS NOT NULL) OR (statut='retire' AND auteur!='extraction'))""", (eid,))}
        for champ, els in d.get("champs", {}).items():
            for i, e in enumerate(els):
                if e.get("verbatim") in desavoues:
                    continue
                inserer(con, eid, champ, i, _ligne(e), "extraction", maj); n_el += 1
        for i, e in enumerate(d.get("rejetes", [])):
            motif = e.get("relecteur") or ("seuil : " + e["seuils"] if e.get("seuils") else "verbatim non retrouvé")
            inserer(con, eid, e.get("champ"), i, _ligne(e, "rejete", motif), "extraction", maj); n_rej += 1
    con.commit()
    return code, len(tsv), n_el, n_rej


def dans_le_livre(source, verbatim):
    """Type de la source (book / cours) si le verbatim est dans un de ses passages (anapath_rag.db), sinon None.
    Même contrôle que la chaîne de réponse (ask_anapath._contains)."""
    from ask_anapath import _contains
    rc = sqlite3.connect(ROOT / "anapath_rag.db")
    return next((ty for t, ty in rc.execute("SELECT chunk_text, source_type FROM chunk_meta WHERE source_short=?",
                                            (source,)) if _contains(t, verbatim)), None)


def corriger(con, eid, auteur, force=False, **champs):
    old = con.execute("SELECT * FROM elements WHERE id=?", (eid,)).fetchone()
    if not old:
        sys.exit(f"élément {eid} introuvable")
    cols = [c[1] for c in con.execute("PRAGMA table_info(elements)")]
    old = dict(zip(cols, old))
    if old["statut"] not in ("actif", "rejete"):
        sys.exit(f"élément {eid} déjà {old['statut']} (voir remplace_par={old['remplace_par']})")
    new = {k: old[k] for k in ("fr", "verbatim", "source", "type", "page", "verdict", "relecteur", "seuils",
                               "src_corrigee", "verbatim_ajuste")} | {k: v for k, v in champs.items() if v is not None}
    motif = None
    if champs.get("verbatim") or champs.get("source"):
        if ty := dans_le_livre(new["source"], new["verbatim"]):
            new["type"] = ty
        else:
            if not force:
                sys.exit(f"verbatim absent de « {new['source']} » dans anapath_rag.db : refusé (--force pour passer outre)")
            motif = "verbatim NON retrouvé dans le livre (forcé)"
        new["verdict"] = new["relecteur"] = None          # le verdict du relecteur portait sur l'ancien texte
    new |= dict(statut="actif", motif=motif)
    m = maintenant()
    nid = inserer(con, old["entite_id"], old["champ"], old["ordre"], new, auteur, m)
    con.execute("UPDATE elements SET statut='remplace', remplace_par=?, maj=? WHERE id=?", (nid, m, eid))
    con.commit()
    return nid


def reassembler(con, eid):
    """La fiche .md (lue par acp.adeptus à chaque visite) suit la base : réassemblée après chaque modification."""
    import subprocess
    code, dossier = con.execute("""SELECT f.code, f.dossier FROM elements e JOIN entites n ON n.id = e.entite_id
                                   JOIN fiches f ON f.code = n.fiche WHERE e.id=?""", (eid,)).fetchone()
    subprocess.run([sys.executable, str(ROOT / "assemble_fiche.py"), code,
                    str(ROOT / "fiches" / f"fiche_{code}_{dossier}.md")], cwd=ROOT, check=True)


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("importe"); p.add_argument("dossiers", nargs="*")
    p = sp.add_parser("cherche"); p.add_argument("q"); p.add_argument("--champ"); p.add_argument("--fiche")
    p.add_argument("--tous", action="store_true", help="aussi rejetés / retirés / remplacés")
    p = sp.add_parser("corrige"); p.add_argument("id", type=int)
    for k in ("--fr", "--verbatim", "--source", "--page"):
        p.add_argument(k)
    p.add_argument("--auteur", default="remi"); p.add_argument("--force", action="store_true")
    p = sp.add_parser("retire"); p.add_argument("id", type=int); p.add_argument("--motif", required=True)
    p.add_argument("--auteur", default="remi")
    p = sp.add_parser("ajoute"); p.add_argument("entite_id", type=int); p.add_argument("champ")
    for k in ("--fr", "--verbatim", "--source", "--page"):
        p.add_argument(k, required=True)
    p.add_argument("--auteur", default="remi"); p.add_argument("--force", action="store_true")
    sp.add_parser("test", help="auto-test sur une copie de la base")
    a = ap.parse_args()
    if a.cmd == "test":
        return autotest()
    con = connect()
    if a.cmd == "importe":
        for d in a.dossiers or ORGANES:
            print("%s : %d entités, %d éléments, %d rejetés" % importer(con, d))
        con.execute("INSERT INTO elements_fts(elements_fts) VALUES ('integrity-check')")
    elif a.cmd == "cherche":
        sql = ("SELECT v.id, v.fiche, v.num, v.entite_fr, v.champ, v.statut, v.fr, v.verbatim, v.source, v.page "
               "FROM elements_fts JOIN v_elements v ON v.id = elements_fts.rowid WHERE elements_fts MATCH ?")
        args = [a.q]
        if not a.tous:
            sql += " AND v.statut='actif'"
        if a.champ:
            sql += " AND v.champ=?"; args.append(a.champ)
        if a.fiche:
            sql += " AND v.fiche=?"; args.append(a.fiche)
        for r in con.execute(sql + " ORDER BY v.fiche, v.num, v.champ", args):
            print(f"#{r[0]} {r[1]}.{r[2]} {r[3]} · {r[4]}{'' if r[5] == 'actif' else ' [' + r[5] + ']'}\n"
                  f"   {r[6]}\n   « {' '.join(r[7].split())[:300]} » [{r[8]}, p. {r[9]}]")
    elif a.cmd == "corrige":
        nid = corriger(con, a.id, a.auteur, a.force, fr=a.fr, verbatim=a.verbatim, source=a.source, page=a.page)
        print("nouvel élément", nid)
        reassembler(con, nid)
    elif a.cmd == "retire":
        n = con.execute("UPDATE elements SET statut='retire', motif=?, auteur=?, maj=? WHERE id=? AND statut='actif'",
                        (a.motif, a.auteur, maintenant(), a.id)).rowcount
        con.commit(); print("retiré" if n else f"élément {a.id} introuvable ou pas actif")
        if n:
            reassembler(con, a.id)
    elif a.cmd == "ajoute":
        motif, ty = None, dans_le_livre(a.source, a.verbatim)
        if not ty:
            if not a.force:
                sys.exit(f"verbatim absent de « {a.source} » dans anapath_rag.db : refusé (--force pour passer outre)")
            motif = "verbatim NON retrouvé dans le livre (forcé)"
        ordre = con.execute("SELECT coalesce(max(ordre)+1, 0) FROM elements WHERE entite_id=? AND champ=?",
                            (a.entite_id, a.champ)).fetchone()[0]
        row = _ligne({"fr": a.fr, "verbatim": a.verbatim, "source": a.source, "page": a.page, "type": ty or "book"},
                     motif=motif)
        nid = inserer(con, a.entite_id, a.champ, ordre, row, a.auteur, maintenant())
        con.commit(); print("nouvel élément", nid)
        reassembler(con, nid)


def autotest():
    """Copie de la base : correction refusée hors livre, correction du sens, retrait, survie à la ré-importation."""
    import shutil, tempfile
    global DB
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    shutil.copy(DB, tmp); DB = tmp
    con = connect()
    eid, _, verb = con.execute("""SELECT e.id, e.source, e.verbatim FROM elements e JOIN entites n ON n.id=e.entite_id
        WHERE n.fiche='5DIG1' AND e.statut='actif' AND e.type='book' ORDER BY e.id LIMIT 1""").fetchone()
    try:
        corriger(con, eid, "test", verbatim="phrase inventée de toutes pièces")
        raise AssertionError("verbatim hors livre accepté")
    except SystemExit as e:
        assert "refusé" in str(e)
    nid = corriger(con, eid, "test", fr="sens corrigé à la main")
    assert con.execute("SELECT statut, remplace_par FROM elements WHERE id=?", (eid,)).fetchone() == ("remplace", nid)
    assert con.execute("SELECT fr, verbatim FROM elements WHERE id=?", (nid,)).fetchone() == ("sens corrigé à la main", verb)
    assert con.execute("SELECT count(*) FROM elements_fts WHERE elements_fts MATCH '\"sens corrigé\"'").fetchone()[0] == 1
    importer(con, "digestif")                     # ré-extraction : la correction à la main reste active
    assert con.execute("SELECT statut FROM elements WHERE id=?", (nid,)).fetchone()[0] == "actif"
    n = con.execute("SELECT count(*) FROM elements e JOIN entites n ON n.id=e.entite_id WHERE n.fiche='5DIG1' "
                    "AND e.statut='actif' AND e.auteur='extraction'").fetchone()[0]
    assert n == 188, n               # pas de doublon, et l'original corrigé à la main ne revient pas
    print("auto-test OK")


if __name__ == "__main__":
    main()
