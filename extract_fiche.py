#!/usr/bin/env python3
"""Extraction par Qwen (Magos) des champs du § 5 d'une fiche, entité par entité, avec contrôle VERBATIM.

    python extract_fiche.py fiches/entites_5R1.tsv --out fiches/rein_natif [--num 1,9] [--model ...]

Pour chaque entité (ligne du TSV : num, famille, fichier_colvin, entite_fr) :
  1. Sources numérotées [S1..Sn] : passages du chapitre Colvin (priorité microscopie / IF / ME /
     différentiel, budget MOTS_COLVIN) + passages des autres livres et cours par recherche.py (expansion
     FR↔EN), chacun avec sa page imprimée (chunk_pages).
  2. Qwen remplit les champs en JSON ; chaque élément = {"fr": phrase française, "verbatim": citation
     EXACTE copiée de la source, "src": "S3"}.
  3. Contrôle : verbatim (normalisé espaces/césures/ligatures/guillemets) présent dans la source citée,
     sinon l'élément est REJETÉ et compté. Un champ tenu seulement par des sources 'cours' est marqué.
Sortie : <out>/<num>_<slug>.json (sources, réponse brute, éléments gardés/rejetés). Relançable :
une entité déjà extraite n'est pas refaite (--force pour refaire).
"""
import argparse, csv, json, re, sqlite3, time, unicodedata
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent
MAGOS = "http://127.0.0.1:8200"
MOTS_COLVIN = 4500
N_AUTRES, MOTS_AUTRE = 4, 350
CHAMPS = {
    "definition": "définition / terminologie",
    "critere_positif": "critères morphologiques positifs en microscopie optique",
    "mesure": "ce qu'il faut mesurer ou compter, et la valeur qui fait basculer",
    "distribution": "distribution des lésions (focale, segmentaire, diffuse, globale…)",
    "gradation": "gradation ou classification nommée (ex. ISN/RPS, Oxford MEST-C, Columbia)",
    "if_attendue": "immunofluorescence attendue",
    "me_attendue": "microscopie électronique attendue",
    "differentiel": "diagnostic différentiel et comment trancher",
    "technique": "technique complémentaire qui tranche",
    "ne_pas_conclure": "ce qu'il ne faut pas conclure / pièges",
}
CHAMPS_FOIE = {
    "definition": "définition / terminologie",
    "critere_positif": "critères morphologiques positifs en microscopie optique",
    "topographie": "topographie des lésions (portale, périportale/interface, lobulaire, centrolobulaire zone 3, sinusoïdale…)",
    "mesure": "ce qu'il faut mesurer ou compter, et la valeur qui fait basculer",
    "gradation": "score ou classification nommée (METAVIR, Ishak, NAS, SAF, Nakanuma, Ludwig, Banff…)",
    "colorations": "colorations spéciales attendues (réticuline, trichrome/Sirius, Perls, PAS-diastase, rhodanine/orcéine…)",
    "ihc": "immunohistochimie attendue (CK7, CK19, HBsAg/HBc, A1AT, C4d…)",
    "differentiel": "diagnostic différentiel et comment trancher",
    "technique": "examen complémentaire qui tranche (biologie, sérologie, dosage du fer ou du cuivre, génétique)",
    "ne_pas_conclure": "ce qu'il ne faut pas conclure / pièges",
}
ORGANES = {"rein": ("colvin_kidney", "rein natif, biopsie", CHAMPS),
           "foie": ("bis_liver", "foie, biopsie hépatique médicale", CHAMPS_FOIE)}
PRIO = re.compile(r"MICROSCOP|Light Microscopy|Immunofluorescence|Electron|DIFFERENTIAL|DIAGNOSTIC CHECKLIST|"
                  r"Histolog|Glomeruli|Tubules|Interstitium|Vessels", re.I)

PROMPT = """Tu es pathologiste. Entité : « {ent} » ({contexte}).

SOURCES :
{sources}

Remplis la fiche en JSON strict, clés : {cles}.
Chaque clé = liste (éventuellement vide) d'éléments {{"fr": "...", "verbatim": "...", "src": "S1"}} :
- "verbatim" = phrase ou fragment COPIÉ MOT POUR MOT de la source "src" (pas de reformulation, 8 à 40 mots) ;
  n'ajoute AUCUN mot, même dans un tableau (ex. ne pas écrire « Class » si la ligne commence par « I ») : copie la ligne telle qu'elle est écrite ;
- "fr" = ce que dit ce verbatim, en français, une phrase ;
- n'utilise QUE les sources ; une information absente des sources ne s'écrit pas.
Signification des clés : {sens}
Réponds uniquement par le JSON."""


def norm(s):
    """Lettres et chiffres seuls : puces (○ •), flèches de figure (﬈), guillemets et césures n'entrent pas
    dans la comparaison — Qwen les retire en copiant, le livre les porte."""
    s = unicodedata.normalize("NFKD", s.replace("­", "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.findall(r"[a-z0-9]+", s))


ETIQUETTE = re.compile(r"^(class|classe|type|grade|stage|stade) ")


def present(verbatim, texte, etiquette=False):
    """Chaque segment entre « … » (au moins 3 mots) figure dans le texte normalisé. etiquette=True :
    un mot d'étiquette ajouté en tête de segment (« Class I … » pour une ligne de tableau « I … ») est ignoré."""
    segs = [norm(x) for x in re.split(r"\.\.\.|…", verbatim)]
    segs = [ETIQUETTE.sub("", x) if etiquette else x for x in segs if x]
    # sans espaces : le PDF coupe les mots en fin de ligne (« fea tures »), le modèle les recolle (« features »)
    plat = texte.replace(" ", "")
    return bool(segs) and sum(len(x.split()) for x in segs) >= 4 and \
        all(len(x.split()) >= 3 and x.replace(" ", "") in plat for x in segs)


def sources_for(rc, ch_num, ent_fr, livre="colvin_kidney", ent_en=""):
    rows = rc.execute("""SELECT m.rowid, m.chunk_text, p.page_label FROM chunk_meta m LEFT JOIN chunk_pages p
                         USING (rowid) WHERE m.source_short=? AND m.chapter_num=?
                         ORDER BY m.rowid""", (livre, ch_num)).fetchall()
    titre = rc.execute("SELECT chapter_title FROM chunk_meta WHERE source_short=? AND chapter_num=?",
                       (livre, ch_num)).fetchone()[0]
    rang = {}
    if livre != "colvin_kidney":     # chapitre = une famille (bis_liver) : passages de l'entité d'abord
        from recherche import search
        rang = {t: i for i, (_, _, _, ch, _, t) in enumerate(search(ent_en or ent_fr, k=40, livre=livre)[2]) if ch == titre}
    ordre = sorted(range(len(rows)), key=lambda i: (rows[i][1] not in rang, rang.get(rows[i][1], 0),
                                                    not PRIO.search(rows[i][1]), i))
    garde, mots = set(), 0
    for i in ordre:
        w = len(rows[i][1].split())
        if mots + w > MOTS_COLVIN and garde:
            continue
        garde.add(i); mots += w
    src = [(livre, "book", rows[i][2], rows[i][1]) for i in sorted(garde)]
    from recherche import search
    _, _, out = search(f"{ent_en or ent_fr} {titre}", k=N_AUTRES + 6)
    for st, _, s, ch, pl, txt in out:
        if s == livre or len(src) >= len(garde) + N_AUTRES:
            continue
        src.append((s, st, pl, " ".join(txt.split()[:MOTS_AUTRE])))
    return titre, src


def ask_albert(model, prompt):
    """model = « albert:<id> » : API DINUM, même clé que la page Demander. Livres seulement, jamais de donnée patient."""
    cle = open(Path.home() / ".config/albert.key").read().strip()
    for k in range(4):
        try:
            r = requests.post("https://albert.api.etalab.gouv.fr/v1/chat/completions", timeout=900,
                              headers={"Authorization": f"Bearer {cle}"},
                              json={"model": model.split(":", 1)[1], "temperature": 0, "max_tokens": 16000,
                                    "messages": [{"role": "user", "content": prompt}]}).json()
            return (r["choices"][0]["message"].get("content") or "").rpartition("</think>")[2]
        except (requests.RequestException, KeyError) as e:
            print("   albert:", e, flush=True)
        time.sleep(30 * (k + 1))
    return ""


def relire(gard, rej, src):
    """Relecteur (ask_anapath.juger, gpt-oss-120b via Albert) : le verbatim soutient-il la phrase ? C/H -> rejeté."""
    import ask_anapath
    plats = [(k, e) for k, v in gard.items() for e in v]
    textes = [norm(s[3]) for s in src]
    # passage = celui qui contient le verbatim (plusieurs passages Colvin partagent une page : livre+page ne suffit pas)
    num = lambda e: next((i for i, t in enumerate(textes, 1) if present(e["verbatim"], t) or present(e["verbatim"], t, True)), 1)
    shown = {i: {"title": f"{s[0]}, p. {s[2] or '?'}", "chunk_text": s[3]} for i, s in enumerate(src, 1)}
    gardes = [(e["fr"], e["verbatim"], num(e), []) for _, e in plats]
    vd = ask_anapath.juger("(pas de synthèse : élément 0 sans objet, réponds S)", gardes, shown)
    if not vd:
        return gard, rej, "relecteur indisponible"
    out = {}
    for i, (k, e) in enumerate(plats, 1):
        v, pq = vd.get(i, ("", ""))
        if v in ("C", "H"):
            rej.append({"champ": k, **e, "relecteur": f"{v} : {pq}"})
        else:
            out.setdefault(k, []).append({**e, **({"relecteur": f"P : {pq}"} if v == "P" else {})})
    return out, rej, f"{sum(1 for v, _ in vd.values() if v in 'CH')} retirés par le relecteur"


def ask(model, prompt):
    if model.startswith("albert:"):
        return ask_albert(model, prompt)
    for k in range(4):
        try:
            j = requests.post(f"{MAGOS}/jobs", json={"model": model, "prompt": prompt, "client_id": "anapath_fiche",
                                                     "options": {"temperature": 0}}, timeout=30).json()
            while True:
                r = requests.get(f"{MAGOS}/jobs/{j['uuid']}/wait", params={"timeout": 600}, timeout=700)
                if r.status_code != 408:
                    break
            r = r.json()
            if r["status"] == "done":
                return r["response"] or ""
            print("   magos:", r.get("error"), flush=True)
        except requests.RequestException as e:
            print("   magos:", e, flush=True)
        time.sleep(30 * (k + 1))
    return ""


def parse_json(rep):
    m = re.search(r"\{.*\}", rep, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0), strict=False)    # strict=False : caractères de contrôle recopiés du PDF (\x02)
    except json.JSONDecodeError:
        pass
    # guillemets du livre recopiés sans échappement : réparer ligne par ligne (une clé par ligne)
    def esc(l):
        g = re.match(r'(\s*"(?:fr|verbatim|src)"\s*:\s*")(.*)("\s*,?\s*)$', l)
        return g.group(1) + g.group(2).replace('\\"', '"').replace('"', '\\"') + g.group(3) if g else l
    try:
        return json.loads("\n".join(esc(l) for l in m.group(0).splitlines()), strict=False)
    except json.JSONDecodeError:
        return {}


def controle(data, src):
    """Verbatim cherché d'abord dans la source citée, puis dans les autres (src corrigée et notée)."""
    textes = [norm(x[3]) for x in src]
    gard, rej = {}, []
    for k in CHAMPS:
        for el in data.get(k) or []:
            if not isinstance(el, dict):
                continue
            m = re.match(r"S(\d+)$", str(el.get("src", "")).strip())
            i = int(m.group(1)) - 1 if m else -1
            v = str(el.get("verbatim", ""))
            ordre = ([i] if 0 <= i < len(src) else []) + [j for j in range(len(src)) if j != i]
            j = next((j for j in ordre if present(v, textes[j])), None)
            ajuste = False
            if j is None:
                j = next((j for j in ordre if present(v, textes[j], etiquette=True)), None)
                ajuste = j is not None
            if j is None:
                rej.append({"champ": k, **el})
                continue
            s = src[j]
            gard.setdefault(k, []).append({"fr": el.get("fr", ""), "verbatim": v, "source": s[0], "type": s[1],
                                           "page": s[2], **({"src_corrigee": f"S{i + 1}->S{j + 1}"} if j != i else {}),
                                           **({"verbatim_ajuste": "mot d'étiquette en tête ignoré"} if ajuste else {})})
    return gard, rej


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tsv")
    ap.add_argument("--out", required=True)
    ap.add_argument("--num", help="ex. 1,9")
    ap.add_argument("--model", default="Qwen3.6-35B-direct")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--organe", choices=ORGANES, default="rein")
    ap.add_argument("--recontrole", action="store_true", help="refait contrôle + relecteur sur la réponse brute déjà stockée")
    ap.add_argument("--relecteur", action="store_true", help="2e passe gpt-oss-120b via Albert (ACP_LLM=albert)")
    a = ap.parse_args()
    global CHAMPS
    livre, contexte, CHAMPS = ORGANES[a.organe]
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    rc = sqlite3.connect(ROOT / "anapath_rag.db")
    nums = set(a.num.split(",")) if a.num else None
    for r in csv.DictReader(open(a.tsv), delimiter="\t"):
        if nums and r["num"] not in nums:
            continue
        slug = re.sub(r"[^a-z0-9]+", "_", unicodedata.normalize("NFKD", r["entite_fr"]).encode("ascii", "ignore")
                      .decode().lower()).strip("_")[:40]
        f = out / f"{int(r['num']):02d}_{slug}.json"
        if f.exists() and not (a.force or a.recontrole):
            continue
        t0 = time.time()
        titre, src = sources_for(rc, int((r.get("chapitre") or r.get("fichier_colvin"))[2:]), r["entite_fr"], livre,
                                 r.get("entite_en", ""))
        bloc = "\n\n".join(f"[S{i}] ({s[0]}, p. {s[2] or '?'})\n{' '.join(s[3].split())}" for i, s in enumerate(src, 1))
        rep = json.load(open(f))["reponse_brute"] if a.recontrole else ask(a.model, PROMPT.format(ent=r["entite_fr"], contexte=contexte, sources=bloc, cles=", ".join(CHAMPS),
                                         sens="; ".join(f"{k} = {v}" for k, v in CHAMPS.items())))
        gard, rej = controle(parse_json(rep), src)
        relu = ""
        if a.relecteur and gard:
            gard, rej, relu = relire(gard, rej, src)
        n = sum(len(v) for v in gard.values())
        f.write_text(json.dumps({"entite": r, "colvin_titre": titre, "champs": gard, "rejetes": rej,
                                 "sources": [{"S": i, "source": s[0], "type": s[1], "page": s[2]}
                                             for i, s in enumerate(src, 1)],
                                 "reponse_brute": rep, "modele": a.model, "livre_pivot": livre,
                                 "relecteur": relu,
                                 "date": time.strftime("%Y-%m-%d %H:%M")}, ensure_ascii=False, indent=1))
        print(f"{r['num']:>3} {r['entite_fr'][:45]:45s} {len(src):2d} sources | {n:3d} gardés, "
              f"{len(rej):2d} rejetés | {relu} | {time.time() - t0:.0f} s", flush=True)


if __name__ == "__main__":
    main()
