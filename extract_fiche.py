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
CHAMPS_GEN = {      # organes hors rein/foie : tumeurs et lésions médicales, même grille
    "definition": "définition / terminologie",
    "critere_positif": "critères morphologiques positifs en microscopie optique",
    "architecture": "architecture et topographie des lésions",
    "mesure": "ce qu'il faut mesurer ou compter, et la valeur qui fait basculer",
    "gradation": "grade, stade ou classification nommée (OMS, Gleason/ISUP, Nottingham, FIGO, Bethesda, Paris…)",
    "ihc": "immunohistochimie attendue et son profil",
    "moleculaire": "biologie moléculaire (altération, fusion, technique qui la montre)",
    "differentiel": "diagnostic différentiel et comment trancher",
    "compte_rendu": "éléments du compte rendu (marges, invasion, facteurs pronostiques, ce qu'il faut écrire)",
    "ne_pas_conclure": "ce qu'il ne faut pas conclure / pièges",
}
_GEN = {"poumon": ("bis_lung", "poumon, biopsie et pièce"), "sein": ("bis_breast", "sein, biopsie et pièce"),
        "thyroide": ("bis_thyroid", "thyroïde, cytologie et histologie"), "prostate": ("bis_prostate", "prostate, biopsie"),
        "peau": ("bis_skin", "peau, tumeurs"), "digestif": ("bis_gastrointestinal_tract_mucosa", "tube digestif, biopsie muqueuse"),
        "uterus": ("bis_uterine_cervix_and_corpus", "col et corps utérin"), "vessie": ("bis_bladder", "vessie, biopsie et résection"),
        "snc": ("bis_central_nervous_system", "système nerveux central, biopsie"),
        "tissus_mous": ("bis_soft_tissue_tumors", "tumeurs des tissus mous"),
        "orl": ("bis_upper_aerodigestiv_tract_and_ear", "voies aérodigestives supérieures et oreille")}
ORGANES = {"rein": ("colvin_kidney", "rein natif, biopsie", CHAMPS),
           "foie": ("bis_liver", "foie, biopsie hépatique médicale", CHAMPS_FOIE),
           **{k: (livre, ctx, CHAMPS_GEN) for k, (livre, ctx) in _GEN.items()}}
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


_SCHEMA_RELU = {"type": "object", "additionalProperties": False, "required": ["verdicts"], "properties": {"verdicts": {
    "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["i", "v", "pourquoi"],
                               "properties": {"i": {"type": "integer"}, "v": {"type": "string", "enum": ["S", "P", "C", "H"]},
                                              "pourquoi": {"type": "string"}}}}}}


def relire(gard, rej, src, juge="albert"):
    """Relecteur : le verbatim soutient-il la phrase ? C/H -> rejeté, P -> marqué. juge = albert (ask_anapath.juger,
    gpt-oss-120b, 1 000 appels/jour) ou claude (Sonnet 5.5, même consigne JUGE, verdicts imposés par schéma)."""
    import ask_anapath
    plats = [(k, e) for k, v in gard.items() for e in v]
    textes = [norm(s[3]) for s in src]
    # passage = celui qui contient le verbatim (plusieurs passages Colvin partagent une page : livre+page ne suffit pas)
    num = lambda e: next((i for i, t in enumerate(textes, 1) if present(e["verbatim"], t) or present(e["verbatim"], t, True)), 1)
    shown = {i: {"title": f"{s[0]}, p. {s[2] or '?'}", "chunk_text": s[3]} for i, s in enumerate(src, 1)}
    gardes = [(e["fr"], e["verbatim"], num(e), []) for _, e in plats]
    if juge == "claude":
        txt = "".join(f"PASSAGE #{n} — {shown[n]['title']}\n{shown[n]['chunk_text'][:6000]}\n\n" for n in sorted({g[2] for g in gardes}))
        txt += "ÉLÉMENT 0 (synthèse) : sans objet, réponds S\n" + "".join(
            f"ÉLÉMENT {i} : FR = {fr}\n   VERBATIM (#{n}) = {v}\n" for i, (fr, v, n, _) in enumerate(gardes, 1))
        try:
            d = parse_json(claude_json(txt, ask_anapath.JUGE, _SCHEMA_RELU, effort="medium", max_tokens=16000))
            vd = {int(x["i"]): (x["v"], x["pourquoi"]) for x in d.get("verdicts", [])}
        except Exception as e:
            print("   relecteur claude:", type(e).__name__, str(e)[:150], flush=True); vd = {}
    else:
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


CHIFFRE = re.compile(r"\d|%|[<>≤≥]|\b(?:classe|class|grade|score|stade|stage)\b", re.I)
SEUILS = """Tu contrôles UN élément d'une fiche d'anatomie pathologique : une phrase française (FR) appuyée sur une
citation exacte du livre (VERBATIM), avec le passage d'où elle vient. Ne juge que les SEUILS, CHIFFRES et CLASSES :
pour chacun, la valeur, le sens de l'inégalité (<, ≤, >, ≥), l'unité et CE À QUOI il s'applique (par zone
mésangiale, par glomérule, proportion de glomérules, de la circonférence, de la biopsie…) sont-ils EXACTEMENT ceux
du livre ? Une condition du livre omise dans FR (ex. « dans plus de 50 % des glomérules ») rend FR faux ou incomplet.
Verdicts : OK = conforme ; INCOMPLET = juste mais une condition du livre manque ; FAUX = valeur, sens, unité ou
référence différents du livre, ou classe attribuée au mauvais critère.
Réponds UNIQUEMENT par {"v": "OK" | "INCOMPLET" | "FAUX", "pourquoi": "≤ 20 mots"}."""


CLAUDE = "claude-sonnet-5-5"
_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["v", "pourquoi"],
           "properties": {"v": {"type": "string", "enum": ["OK", "INCOMPLET", "FAUX"]}, "pourquoi": {"type": "string"}}}
JETONS = {"in": 0, "out": 0}
_cl = None


def client_anthropic():
    """Clé dédiée ~/.config/anthropic.key si présente (sinon ANTHROPIC_API_KEY) ; une clé non rattachée à un espace de
    travail exige l'en-tête anthropic-workspace-id, lu dans ~/.config/anthropic.workspace."""
    import anthropic
    cle, ws = Path.home() / ".config/anthropic.key", Path.home() / ".config/anthropic.workspace"
    return anthropic.Anthropic(max_retries=6, **({"api_key": cle.read_text().strip()} if cle.exists() else {}),
                               default_headers={"anthropic-workspace-id": ws.read_text().strip()} if ws.exists() else None)


def juge_claude(prompt, system):
    return claude_json(prompt, system, _SCHEMA)


def claude_json(prompt, system, schema, effort="low", max_tokens=4000):
    """Appel Sonnet 5.5 (API Anthropic) : JSON imposé par schéma, bascule serveur si refus ; jetons comptés dans JETONS."""
    global _cl
    _cl = _cl or client_anthropic()
    r = _cl.beta.messages.create(model=CLAUDE, max_tokens=max_tokens, system=system, betas=["server-side-fallback-2026-07-01"],
                                 messages=[{"role": "user", "content": prompt}],
                                 extra_body={"fallbacks": "default",
                                             "output_config": {"effort": effort,
                                                               "format": {"type": "json_schema", "schema": schema}}})
    JETONS["in"] += r.usage.input_tokens; JETONS["out"] += r.usage.output_tokens
    if r.stop_reason == "refusal":
        return ""
    return next((b.text for b in r.content if b.type == "text"), "")


def _passage_de(e, textes):
    return next((i for i, t in enumerate(textes) if present(e["verbatim"], t) or present(e["verbatim"], t, True)), 0)


def _appliquer_seuils(gard, rej, cibles, verdicts):
    """cibles = [(champ, élément)], verdicts = [(v, pourquoi)] : FAUX -> rejeté, INCOMPLET -> marqué."""
    faux = set()
    for (k, e), (v, pq) in zip(cibles, verdicts):
        if v == "FAUX":
            rej.append({"champ": k, **e, "seuils": f"FAUX : {pq}"}); faux.add(id(e))
        elif v == "INCOMPLET":
            e["seuils"] = f"incomplet : {pq}"
    gard = {k: [e for e in v if id(e) not in faux] for k, v in gard.items()}
    n_inc = sum(v == "INCOMPLET" for v, _ in verdicts)
    return {k: v for k, v in gard.items() if v}, rej, (f"seuils : {len(cibles)} vérifiés, {len(faux)} faux retirés, "
                                                       f"{n_inc} incomplets, {sum(not v for v, _ in verdicts)} sans verdict")


def seuils_lot(entites, out, prix_in=2.0, prix_out=10.0, direct=False):
    """Passe des seuils par l'API Batch d'Anthropic (-50 %) pour toute une fiche, avec cache de prompt :
    système = consigne + passages de l'entité (cache_control 1 h, partagé par tous ses éléments) ; message = l'élément.
    entites = [(fichier_json, src)]. L'identifiant du lot est noté dans <out>/_lot_seuils.txt : une relance reprend
    le même lot au lieu d'en payer un second."""
    cl = client_anthropic()
    reqs, ctx = [], {}
    for f, src in entites:
        j = json.load(open(f))
        textes = [norm(x[3]) for x in src]
        systeme = [{"type": "text", "text": SEUILS},
                   {"type": "text", "cache_control": {"type": "ephemeral", "ttl": "1h"},
                    "text": "".join(f"[PASSAGE n° {i}] ({x[0]}, p. {x[2] or '?'})\n{x[3][:6000]}\n\n"
                                    for i, x in enumerate(src, 1))}]
        cibles = [(k, e) for k, v in j["champs"].items() for e in v if CHIFFRE.search(e["fr"])]
        ctx[f] = (j, cibles)
        for n, (k, e) in enumerate(cibles):
            reqs.append({"custom_id": f"{f.stem[:40]}-{n}", "params": {
                "model": CLAUDE, "max_tokens": 4000, "system": systeme,
                "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}},
                "messages": [{"role": "user", "content": f"Élément à contrôler (il s'appuie sur le PASSAGE n° "
                              f"{_passage_de(e, textes) + 1}) :\nFR : {e['fr']}\nVERBATIM : {e['verbatim']}"}]}})
    if not reqs:
        print("rien à contrôler"); return
    trace = out / "_lot_seuils.txt"
    if direct:      # mêmes requêtes, appel direct (prix plein) : le cache sert dès le 2e élément d'une entité
        from concurrent.futures import ThreadPoolExecutor
        def un(q):
            try:
                return q["custom_id"], cl.messages.create(**q["params"])
            except Exception as e:
                print("   claude:", type(e).__name__, str(e)[:150], flush=True); return q["custom_id"], None
        par_entite = {}
        for q in reqs:
            par_entite.setdefault(q["custom_id"].rsplit("-", 1)[0], []).append(q)
        msgs = []
        with ThreadPoolExecutor(4) as ex:    # entités en parallèle, éléments d'une entité à la suite (cache chaud)
            for lst in ex.map(lambda l: [un(q) for q in l], par_entite.values()):
                msgs += lst
    else:
        lot_id = trace.read_text().strip() if trace.exists() else None
        if not lot_id:
            lot_id = cl.messages.batches.create(requests=reqs).id
            trace.write_text(lot_id)
        print(f"lot {lot_id} : {len(reqs)} éléments, {len(ctx)} entités", flush=True)
        while (b := cl.messages.batches.retrieve(lot_id)).processing_status != "ended":
            print(f"  {time.strftime('%H:%M')} en cours : {b.request_counts.processing}, faits : {b.request_counts.succeeded}", flush=True)
            time.sleep(60)
        msgs = [(r.custom_id, r.result.message) for r in cl.messages.batches.results(lot_id) if r.result.type == "succeeded"]
    res, u = {}, {"in": 0, "cw": 0, "cr": 0, "out": 0}
    for cid, m in msgs:
        if m is None:
            continue
        u["in"] += m.usage.input_tokens; u["out"] += m.usage.output_tokens
        u["cw"] += m.usage.cache_creation_input_tokens or 0; u["cr"] += m.usage.cache_read_input_tokens or 0
        if m.stop_reason != "refusal":
            d = parse_json(next((x.text for x in m.content if x.type == "text"), ""))
            if str(d.get("v", "")).upper() in ("OK", "INCOMPLET", "FAUX"):
                res[cid] = (str(d["v"]).upper(), str(d.get("pourquoi", "")))
    for f, (j, cibles) in ctx.items():
        verdicts = [res.get(f"{f.stem[:40]}-{n}", ("", "")) for n in range(len(cibles))]
        j["champs"], j["rejetes"], j["seuils"] = _appliquer_seuils(j["champs"], j["rejetes"], cibles, verdicts)
        j["seuils"] += f" (juge {CLAUDE}, {'direct' if direct else 'lot'})"
        f.write_text(json.dumps(j, ensure_ascii=False, indent=1))
        print(f"  {j['entite']['entite_fr'][:45]:45s} | {j['seuils']}", flush=True)
    cout = (1 if direct else 0.5) * (u["in"] * prix_in + u["cw"] * prix_in * 2 + u["cr"] * prix_in * 0.1 + u["out"] * prix_out) / 1e6
    print(f"jetons : {u['in']} entrée, {u['cw']} écrits en cache, {u['cr']} lus en cache, {u['out']} sortie "
          f"→ ≈ {cout:.2f} $ ({'direct' if direct else 'lot -50 %'})", flush=True)
    trace.unlink(missing_ok=True)


def seuils(gard, rej, src, juge="albert"):
    """Passe des seuils, élément par élément (2 en parallèle, 10 req/min chez Albert), sur les seuls éléments chiffrés.
    FAUX -> rejeté ; INCOMPLET -> gardé, marqué. Le relecteur en lot ne voit pas une règle déformée autour
    d'un chiffre juste (« M0 si < 4, M1 si ≥ 4 » : la condition « > 50 % des glomérules » avait disparu)."""
    import ask_anapath
    from concurrent.futures import ThreadPoolExecutor
    textes = [norm(x[3]) for x in src]

    def un(e):
        i = next((i for i, t in enumerate(textes) if present(e["verbatim"], t) or present(e["verbatim"], t, True)), 0)
        p = (f"PASSAGE ({src[i][0]}, p. {src[i][2] or '?'}) :\n{src[i][3][:6000]}\n\nFR : {e['fr']}\n"
             f"VERBATIM : {e['verbatim']}")
        for _ in range(2):                           # une nouvelle tentative si la sortie n'est pas lisible
            try:
                d = parse_json(juge_claude(p, SEUILS) if juge == "claude" else
                               ask_anapath._magos(p, system=SEUILS, model=ask_anapath.JUGE_MODEL or None))
                if str(d.get("v", "")).upper() in ("OK", "INCOMPLET", "FAUX"):
                    return str(d["v"]).upper(), str(d.get("pourquoi", ""))
            except Exception:
                pass
        return "", ""
    cibles = [(k, e) for k, v in gard.items() for e in v if CHIFFRE.search(e["fr"])]
    with ThreadPoolExecutor(8 if juge == "claude" else 2) as ex:      # ponytail: Albert plafonne à 10 req/min ; 2 suffisent, les 429 sont rattrapés
        verdicts = list(ex.map(lambda ke: un(ke[1]), cibles))
    return _appliquer_seuils(gard, rej, cibles, verdicts)


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
    ap.add_argument("--relecteur", action="store_true", help="2e passe : relecture du sens (voir --juge-relecture)")
    ap.add_argument("--juge-relecture", choices=["albert", "claude"], default="albert", help="albert = gpt-oss ; claude = Sonnet 5.5")
    ap.add_argument("--seuils", action="store_true", help="3e passe : seuils/chiffres élément par élément")
    ap.add_argument("--seuils-seuls", action="store_true", help="applique seulement la passe des seuils aux JSON existants")
    ap.add_argument("--juge-seuils", choices=["albert", "claude", "claude-lot", "claude-direct"], default="albert",
                    help="claude = Sonnet 5.5 appel par appel ; claude-lot = API Batch + cache (-50 %%), pour une fiche entière")
    a = ap.parse_args()
    global CHAMPS
    livre, contexte, CHAMPS = ORGANES[a.organe]
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    rc = sqlite3.connect(ROOT / "anapath_rag.db")
    nums = set(a.num.split(",")) if a.num else None
    lot = []
    for r in csv.DictReader(open(a.tsv), delimiter="\t"):
        if nums and r["num"] not in nums:
            continue
        slug = re.sub(r"[^a-z0-9]+", "_", unicodedata.normalize("NFKD", r["entite_fr"]).encode("ascii", "ignore")
                      .decode().lower()).strip("_")[:40]
        f = out / f"{int(r['num']):02d}_{slug}.json"
        if a.seuils_seuls:
            if f.exists():
                j = json.load(open(f))
                if j.get("seuils"):
                    continue                         # déjà passé
                _, src = sources_for(rc, int((r.get("chapitre") or r.get("fichier_colvin"))[2:]), r["entite_fr"],
                                     livre, r.get("entite_en", ""))
                if a.juge_seuils in ("claude-lot", "claude-direct"):
                    lot.append((f, src)); continue
                j["champs"], j["rejetes"], j["seuils"] = seuils(j["champs"], j["rejetes"], src, a.juge_seuils)
                f.write_text(json.dumps(j, ensure_ascii=False, indent=1))
                print(f"{r['num']:>3} {r['entite_fr'][:45]:45s} | {j['seuils']}"
                      + (f" | {JETONS['in']} in / {JETONS['out']} out, cumul ≈ {(JETONS['in'] * 2 + JETONS['out'] * 10) / 1e6:.2f} $"
                         if a.juge_seuils == "claude" else ""), flush=True)
            continue
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
            gard, rej, relu = relire(gard, rej, src, a.juge_relecture)
        if a.seuils and gard:
            gard, rej, s_ = seuils(gard, rej, src, a.juge_seuils)
            relu = f"{relu} ; {s_}" if relu else s_
        n = sum(len(v) for v in gard.values())
        f.write_text(json.dumps({"entite": r, "colvin_titre": titre, "champs": gard, "rejetes": rej,
                                 "sources": [{"S": i, "source": s[0], "type": s[1], "page": s[2]}
                                             for i, s in enumerate(src, 1)],
                                 "reponse_brute": rep, "modele": a.model, "livre_pivot": livre,
                                 "relecteur": relu,
                                 "date": time.strftime("%Y-%m-%d %H:%M")}, ensure_ascii=False, indent=1))
        print(f"{r['num']:>3} {r['entite_fr'][:45]:45s} {len(src):2d} sources | {n:3d} gardés, "
              f"{len(rej):2d} rejetés | {relu} | {time.time() - t0:.0f} s", flush=True)
    if lot:
        seuils_lot(lot, out, direct=a.juge_seuils == "claude-direct")
    if JETONS["in"] or JETONS["out"]:
        print(f"Sonnet (appels directs) : {JETONS['in']} jetons en entrée, {JETONS['out']} en sortie "
              f"≈ {(JETONS['in'] * 2 + JETONS['out'] * 10) / 1e6:.2f} $", flush=True)


if __name__ == "__main__":
    main()
