#!/usr/bin/env python3
"""Page de test pour quelques collègues : recherche dans anapath_rag.db + fiches (fiches/*.md).

    python web_acp.py            # 127.0.0.1:5087, exposé par acp-tunnel -> acp.adeptus-mechanicus.uk

Accès : formulaire de connexion + cookie de session ; identifiant/mot de passe OBLIGATOIRES dans ACP_USER / ACP_PASS
(aucun défaut), clé de session dans ~/.config/acp_secret. /dl/<jeton> : téléchargement du paquet, jeton à usage unique.
Lecture seule. Libre Pathology : CC BY-NC-SA 4.0, attribué avec lien sous chaque passage.
"""
import csv, functools, hmac, json, os, re, sqlite3, threading, time, uuid
from html import escape
from pathlib import Path
from flask import Flask, Response, abort, jsonify, redirect, request, send_file, session, url_for
import markdown
import recherche
import ask_anapath

ROOT = Path(__file__).resolve().parent
USER, PASS = os.environ["ACP_USER"], os.environ["ACP_PASS"]       # pas de défaut : rien de secret dans le code
app = Flask(__name__)
app.secret_key = (Path.home() / ".config/acp_secret").read_text().strip()      # cookie de session signé
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=12 * 3600)
verrou = threading.Lock()          # ponytail: une recherche à la fois (modèle CPU partagé), suffit pour quelques testeurs

CSS = """<style>
:root{--bg:#fbfaf7;--fg:#222;--mut:#666;--acc:#7a2e2e;--card:#fff;--bd:#ddd}
@media (prefers-color-scheme:dark){:root{--bg:#1b1b1d;--fg:#e6e6e6;--mut:#999;--acc:#e08a7a;--card:#242427;--bd:#3a3a3e}}
body{background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif;max-width:900px;margin:auto;padding:0 16px}
a{color:var(--acc)} header{display:flex;gap:1em;align-items:baseline;border-bottom:1px solid var(--bd);margin-bottom:1em}
input[type=search],input[name=u],input[type=password]{width:100%;font-size:1.1em;padding:.5em;box-sizing:border-box;background:var(--card);color:var(--fg);border:1px solid var(--bd)}
.r{background:var(--card);border:1px solid var(--bd);border-radius:6px;padding:.7em 1em;margin:.7em 0}
.m{color:var(--mut);font-size:.85em} .cours{border-left:4px solid #c9a227} .book{border-left:4px solid var(--acc)}
mark.cit-ok{background:#d1f7c4;color:#222}mark.cit-warn{background:#ffe8a3;color:#222}mark.cit-bad{background:#ffc9c9;color:#222;text-decoration:line-through}
.passage{white-space:pre-wrap;font-size:.9em}textarea{width:100%;box-sizing:border-box;font-size:1em;background:var(--card);color:var(--fg);border:1px solid var(--bd)}
blockquote{color:var(--mut)} table{border-collapse:collapse} td,th{border:1px solid var(--bd);padding:.2em .5em}
</style>"""


def page(titre, corps):
    return (f"<!doctype html><html lang=fr><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<title>{escape(titre)}</title>{CSS}<header><h2><a href='/demander'>Anapath ACP</a></h2>"
            f"<a href='/demander'>Demander</a><a href='/bench'>Benchmark</a><a href='/fiches'>Fiches</a><a href='/methode'>Méthode</a><span class=m>test — fiches en brouillon</span><a href='/deconnexion' style='margin-left:auto'>Déconnexion</a></header>"
            f"{AVERT}{corps}" + ("<script src='/widget-feedback.js' defer></script>" if WIDGET.exists() else ""))


AVERT = ("<p class='r m' role=note><b>Outil documentaire, pas un avis diagnostique.</b> Les réponses citent des livres "
         "dont l'édition peut être antérieure à la classification en vigueur (ex. OMS SNC 2016, pas 2021) : vérifier "
         "dans la source. <b>Ne saisir aucune donnée patient</b> : ni nom, ni date de naissance, ni n° d'examen ou "
         "d'IPP, ni date ou lieu de prélèvement. Les questions sont enregistrées pour évaluer l'outil.</p>")


@app.before_request
def auth():
    # formulaire + cookie de session (l'auth HTTP basique ne permet pas de se déconnecter : le navigateur la garde)
    if request.endpoint in ("connexion", "static", "telecharger") or session.get("ok"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "Session expirée : reconnecte-toi."}), 401
    return redirect(url_for("connexion", suite=request.full_path if request.method == "GET" else "/"))


@app.route("/connexion", methods=["GET", "POST"])
def connexion():
    err = ""
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("u", ""), USER) and hmac.compare_digest(request.form.get("p", ""), PASS):
            session.clear(); session.permanent = True; session["ok"] = True
            suite = request.args.get("suite", "/")
            return redirect(suite if suite.startswith("/") and not suite.startswith("//") else "/")
        time.sleep(1)                                     # ponytail: frein simple aux essais en rafale
        err = "<p class=m style='color:var(--acc)'>Identifiant ou mot de passe incorrect.</p>"
    return (f"<!doctype html><html lang=fr><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<title>Connexion</title>{CSS}<h2>Anapath ACP</h2>{err}<form method=post>"
            f"<p><input name=u placeholder=Identifiant autocomplete=username required autofocus></p>"
            f"<p><input name=p type=password placeholder='Mot de passe' autocomplete=current-password required></p>"
            f"<button>Se connecter</button></form>")


# ─── Ampoule de retours : widget de data.pazuzu (source unique foetodata_hub), même format de fichier ───
WIDGET = Path(os.environ.get("ACP_WIDGET", Path.home() / "Bureau/foetodata_hub/static/widget-feedback.js"))
FEEDBACK = ROOT / "feedback"
TYPES = {"idee", "bug", "ux", "autre"}


@app.get("/widget-feedback.js")
def widget_feedback():
    return send_file(WIDGET, mimetype="application/javascript", max_age=0) if WIDGET.exists() else abort(404)


@app.route("/api/feedback", methods=["GET", "POST"])
def api_feedback():
    """Sous session (before_request). GET = liste ; POST = un .txt dans feedback/ (hors dépôt, hors paquet)."""
    if request.method == "GET":
        items = []
        for f in sorted(FEEDBACK.glob("*.txt"), reverse=True)[:500]:
            tete, _, corps = f.read_text(encoding="utf-8").partition("\n---\n")
            champs = dict(l.split(" : ", 1) for l in tete.splitlines() if " : " in l)
            items.append({"date": champs.get("Date", ""), "author": champs.get("Auteur", ""), "type": champs.get("Type", ""),
                          "page": champs.get("Page", ""), "text": corps.strip()})
        return jsonify({"items": items})
    d = request.get_json(silent=True) or {}
    texte = str(d.get("text") or "").strip()[:5000]
    if not texte:
        return jsonify({"ok": False, "error": "texte requis"}), 400
    FEEDBACK.mkdir(exist_ok=True)
    fichiers = list(FEEDBACK.glob("*.txt"))
    recents = sum(1 for f in fichiers if f.stat().st_mtime > time.time() - 3600)
    if len(fichiers) >= 2000 or recents >= 30:          # plafonds : 2000 retours, 30 par heure
        return jsonify({"ok": False, "error": "trop de retours, réessaie plus tard"}), 429
    nom = re.sub(r"[^\w -]", "", str(d.get("name") or "Anonyme"))[:30].strip() or "Anonyme"
    typ = d.get("type") if d.get("type") in TYPES else "autre"
    page_ = re.sub(r"[^\w/-]", "", str(d.get("page") or ""))[:40]
    t = time.strftime("%Y-%m-%d %H:%M:%S")
    (FEEDBACK / f"{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}_{typ}.txt").write_text(
        f"Date : {t}\nAuteur : {nom}\nType : {typ}\nPage : {page_}\n---\n{texte}\n", encoding="utf-8")
    return jsonify({"ok": True})


@app.get("/dl/<jeton>")
def telecharger(jeton):
    """Lien non affiché, hors session : un jeton (acp_tokens.py) = un téléchargement du dernier paquet."""
    import acp_paquet, acp_tokens
    f = acp_paquet.dernier()
    ip = request.headers.get("CF-Connecting-IP", request.remote_addr or "")
    if not (f and len(jeton) < 100 and acp_tokens.consommer(jeton, ip)):
        time.sleep(1)                                     # frein aux essais de jetons
        abort(404)
    print(f"téléchargement {f.name} -> {ip}", flush=True)
    return send_file(f, as_attachment=True, download_name=f.name, max_age=0)


@app.get("/deconnexion")
def deconnexion():
    session.clear()
    return redirect(url_for("connexion"))


@app.get("/")
def racine():
    return redirect("/demander")


@functools.lru_cache(maxsize=1)
def ouvrages():
    """Liste repliable des sources de la base (titre = nom du PDF), calculée une fois par démarrage."""
    from split_anapath import jobs
    con = sqlite3.connect(ROOT / "anapath_rag.db")
    n = dict(con.execute("SELECT source_short, count(*) FROM chunk_meta GROUP BY 1"))
    info = {k: (e, a) for k, e, a in con.execute("SELECT source_short, edition, annee FROM source_info")}
    livres, cours = [], {}
    for k, pdf, _, dossier in jobs(None):
        if k not in n:
            continue
        if dossier == "livres":
            e, an = info.get(k, (None, None))
            # mentions de sites de téléchargement retirées des titres affichés
            ti = re.sub(r"(?i)\s*\((?:[^()]*?(?:z-?lib|1lib|libgen|library\.sk)[^()]*)\)|\s*-?\s*libgen\.\w+|[_\s]*medibos\.blogspot\.com"
                        r"|\s*(?:by\s+)?coll\s+z\s+lib", "", pdf.stem)
            livres.append((ti.replace("_", " ").strip(" -"), e or "", an or "", n[k]))
        else:
            cours.setdefault(str(pdf.parent.relative_to(ROOT / "cours")), []).append(pdf.stem.replace("_", " "))
    t = "".join(f"<tr><td>{escape(ti)}</td><td>{escape(str(e))}</td><td>{an}</td><td>{c}</td></tr>" for ti, e, an, c in sorted(livres))
    html = (f"<h2>Les ouvrages</h2><details><summary><b>{len(livres)} livres</b> (preuve) — titre, édition, année, "
            f"passages indexés</summary><div style=overflow-x:auto><table><tr><th>titre</th><th>éd.</th><th>année</th>"
            f"<th>passages</th></tr>{t}</table></div></details>")
    html += (f"<details><summary><b>{sum(map(len, cours.values()))} cours</b> (preuve inférieure), par dossier"
             f"{' + Libre Pathology (wiki, CC BY-NC-SA 4.0, ' + str(n['librepathology']) + ' passages)' if 'librepathology' in n else ''}"
             f"</summary>" + "".join(f"<p><b>{escape(d)}</b> ({len(v)}) : <span class=m>{escape(' · '.join(sorted(v)))}</span></p>"
                                     for d, v in sorted(cours.items())) + "</details>")
    return html


@app.get("/methode")
def methode():
    return page("Méthode", markdown.markdown((ROOT / "METHODE.md").read_text(encoding="utf-8")) + ouvrages())


def titre_fiche(f):
    """« 5.DIG.1 — Tube digestif, biopsies muqueuses » : 1re ligne du .md (assemble_fiche), sans la mention du modèle."""
    t = f.open(encoding="utf-8").readline().removeprefix("# FICHE ").strip()
    return re.sub(r"\s*\([^)]*\)$", "", t) or f.stem


def slug_fiche(f):
    """fiche_5DIG1_digestif.md -> « digestif », fiche_5F1_foie_biopsie.md -> « foie-biopsie » (adresse lisible)."""
    return f.stem.split("_", 2)[2].replace("_", "-")


def liens_fiches():
    return "".join(f"<li><a href='/fiche/{slug_fiche(f)}'>{escape(titre_fiche(f))}</a></li>"
                   for f in sorted((ROOT / "fiches").glob("fiche_*.md")))


@app.get("/fiches")
def fiches():
    liste = liens_fiches()
    return page("Fiches", f"<h3>Fiches</h3><ul>{liste or '<li>aucune pour l’instant</li>'}</ul>")


@app.get("/recherche")         # ponytail: plus dans le menu (demande du 22/10), gardée pour l'usage direct
def accueil():
    q = request.args.get("q", "").strip()
    fiches = liens_fiches()
    corps = (f"<form><input type=search name=q value='{escape(q)}' placeholder='ex. hyalinose segmentaire et focale' autofocus></form>"
             f"<p class=m>Recherche FR ou EN, élargie par MeSH. Bord rouge = livre, bord jaune = cours (preuve inférieure).</p>")
    if q:
        with verrou:
            hit, var, out = recherche.search(q, k=10)
        corps += f"<p class=m>MeSH : {escape(', '.join(hit) or '—')} · variantes : {escape(' | '.join(var))}</p>"
        for st, s, src, ch, pl, txt in out:
            lic = (f" · <a href='https://librepathology.org/wiki/{escape(ch.replace(' ', '_'))}'>Libre Pathology</a>, CC BY-NC-SA 4.0"
                   if src == "librepathology" else "")
            corps += (f"<div class='r {st}'><div class=m><b>{escape(src)}</b> · {escape(ch or '')} · p. {escape(pl or '?')}"
                      f" · {st}{lic}</div><p>{escape(' '.join(txt.split()))}</p></div>")
    else:
        corps += f"<h3>Fiches</h3><ul>{fiches or '<li>aucune pour l’instant</li>'}</ul>"
    return page(q or "Anapath ACP", corps)


@app.get("/fiche/<nom>")
def fiche(nom):
    if nom.startswith("fiche_"):                 # anciennes adresses (fiche_5DIG1_digestif) : redirigées
        f = ROOT / "fiches" / f"{nom}.md"
        if f.is_file() and f.parent == ROOT / "fiches":
            return redirect(f"/fiche/{slug_fiche(f)}", 301)
        abort(404)
    f = next((f for f in (ROOT / "fiches").glob("fiche_*.md") if slug_fiche(f) == nom), None)
    if not f:
        abort(404)
    return page(titre_fiche(f), markdown.markdown(f.read_text(encoding="utf-8"), extensions=["tables"]))


ASKS = {}       # ponytail: en mémoire, perdu au redémarrage


@app.get("/demander")
def demander():
    qui = (f"{ask_anapath.MODEL} (Albert, DINUM) — relu par {ask_anapath.JUGE_MODEL or ask_anapath.MODEL}"
           if ask_anapath.LLM == "albert" else f"{ask_anapath.MODEL} (local)")
    return page("Demander", f"""<p class=m>{qui} cherche lui-même dans la base (4 tours max) et répond en citant les passages.
Chaque citation est vérifiée mot pour mot puis relue par un second modèle : <mark class=cit-ok>verte</mark> retrouvée et
soutenant la phrase, <mark class=cit-warn>orange</mark> recalée, autre passage ou soutien partiel ; les éléments introuvables ou
qui ne soutiennent pas la phrase sont retirés (liste repliée). La synthèse n'est affichée que si le relecteur la juge soutenue.
Une question à la fois : 10 à 60 s.</p>""" + """
<textarea id=q rows=3 placeholder="ex. Quels sont les critères de la classification ISN/RPS de la néphrite lupique ?"></textarea>
<label class=m><input type=checkbox id=dd> discussion des diagnostics différentiels (entité → DD, arguments pour / contre / distingue)</label>
<p><button id=go onclick=ask()>Demander</button> <span id=status class=m></span></p>
<div id=fil></div>
<div id=steps class=m></div><div id=answer class=r style=display:none></div><div id=passages></div>
<script>
const $=id=>document.getElementById(id), esc=s=>s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
let timer=null, cur=null, prof=0, parent=null;
function precis(){parent=cur;$('q').value='';$('q').placeholder='Précision sur la réponse ci-dessus…';$('q').focus();
 $('go').textContent='Préciser ('+(prof+1)+'/2)'}
async function ask(){const q=$('q').value.trim(); if(q.length<8)return;
 if(!parent){prof=0;$('fil').innerHTML=''} else {prof++;
  $('fil').innerHTML+=`<div class="r m"><b>${esc($('fil').dataset.q||'')}</b>${$('answer').innerHTML}</div>`}
 $('fil').dataset.q=q;
 $('go').disabled=true;$('answer').style.display='none';$('passages').innerHTML='';$('steps').innerHTML='';
 const r=await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({q,parent,mode:$('dd').checked?'dd':''})});
 parent=null;$('go').textContent='Demander';$('q').placeholder='Nouvelle question…';
 const d=await r.json(); if(!r.ok){$('status').textContent=d.error;$('go').disabled=false;return}
 cur=d.id;timer=setInterval(()=>poll(d.id),3000);poll(d.id)}
async function poll(id){const a=await (await fetch('/api/ask/'+id)).json();
 $('steps').innerHTML=a.steps.map(s=>s.type==='llm'?`<div>⏳ tour ${s.round} — le modèle réfléchit…</div>`
   :`<div>🔎 <code>${esc(s.q)}</code> → ${s.n} passage(s)</div>`).join('');
 $('status').textContent=a.status==='running'?`en cours… ${a.elapsed} s`:`${a.elapsed} s`;
 if(a.status==='running')return; clearInterval(timer);$('go').disabled=false;
 if(a.status==='error'){$('status').textContent='Erreur : '+a.error;return}
 $('answer').innerHTML=a.answer_html+(prof<2?'<p><button onclick=precis()>Préciser</button> <span class=m>'+(2-prof)+' précision(s) possible(s) — sinon pose une nouvelle question</span></p>':'<p class=m>Deux précisions faites : pose une nouvelle question.</p>');$('answer').style.display='';
 $('passages').innerHTML=a.passages.map(p=>`<div class=r id=p${p.n}><div class=m><b>[#${p.n}]</b> ${esc(p.title)}</div><div class=passage>${p.html}</div></div>`).join('')}
</script>""")


@app.get("/tests")
def tests():
    """Séries de test (rejouer_tests.py) : une ligne par série, une colonne par question, lien vers la réponse."""
    con = sqlite3.connect(ROOT / "ask_log.db")
    rows = con.execute("SELECT id, origine, modele, n_ok, n_warn, n_bad, duree_s FROM asks WHERE origine LIKE 'test:%' "
                       "AND origine NOT LIKE 'test:bench%' ORDER BY id").fetchall()
    series, qs = {}, []
    for i, o, m, ok, wa, ba, du in rows:
        _, tag, qid = o.split(":", 2)
        series.setdefault(tag, {"modele": m})[qid] = (i, ok, wa, ba, du)
        qs += [qid] if qid not in qs else []
    t = "".join(f"<th>{escape(q)}</th>" for q in qs)
    corps = ("<h3>Séries de test (6 questions, rejouer_tests.py)</h3><p class=m>Par case : vert / orange / rouge "
             "(rouge = retiré) · durée. Cliquer pour la réponse. Les séries antérieures au relecteur n'ont que la réponse brute.</p>"
             f"<div style=overflow-x:auto><table><tr><th>série</th><th>modèle</th>{t}</tr>")
    for tag, d in reversed(series.items()):
        corps += f"<tr><td>{escape(tag)}</td><td class=m>{escape(d['modele'] or '')}</td>" + "".join(
            f"<td><a href='/tests/{d[q][0]}'>{d[q][1]}/{d[q][2]}/{d[q][3]}</a> <span class=m>{round(d[q][4] or 0)} s</span></td>"
            if q in d else "<td></td>" for q in qs) + "</tr>"
    corps += "</table></div>"
    if (ev := ROOT / "pplx" / "eval.txt").exists():
        corps += f"<h3>Recherche seule : pplx-embed contre BioLORD</h3><pre>{escape(ev.read_text())}</pre>"
    return page("Tests", corps)


@app.get("/tests/<int:i>")
def test_detail(i):
    r = sqlite3.connect(ROOT / "ask_log.db").execute(
        "SELECT origine, modele, q, requetes, html, reponse, duree_s, cot, note, commentaire FROM asks "
        "WHERE id=? AND origine LIKE 'test:%'", (i,)).fetchone() or abort(404)
    o, m, q, req, h, rep, du, cot, note, com = r
    retour = "/bench" if ":bench" in o else "/tests"
    reqs = ", ".join(x.get("q") or ("ADICAP " + str(x.get("adicap"))) for x in json.loads(req or "[]"))
    cot = "".join(f"<details><summary><b>{escape(c['etape'])}</b> <span class=m>{escape(c['modele'])}</span></summary>"
                  + (f"<p class=m>Raisonnement</p><pre style=white-space:pre-wrap>{escape(c['raisonnement'])}</pre>" if c['raisonnement'] else "")
                  + f"<p class=m>Sortie</p><pre style=white-space:pre-wrap>{escape(c['sortie'])}</pre></details>"
                  for c in json.loads(cot or "[]"))
    return page(o, f"<p class=m><a href='{retour}'>← retour</a> · {escape(o)} · {escape(m or '')} · {du} s</p><h3>{escape(q)}</h3>"
                   f"<p class=m>Recherches : {escape(reqs)}</p>"
                   + (noter(i, o.rsplit(":", 1)[1], note, com) if ":bench" in o else "")
                   + (f"<div class=r>{h}</div>" if h else f"<pre style=white-space:pre-wrap>{escape(rep or '')}</pre>")
                   + (f"<h3>Chaîne de raisonnement (COT)</h3><p class=m>Chaque appel au modèle : tours de recherche, "
                      f"réponse, relecteur. Les passages lus sont ceux listés dans « Recherches ».</p>{cot}" if cot else ""))


NOTES = {"2": "2 · juste et complet", "1": "1 · partiel", "0": "0 · faux ou absent", "D": "D · dangereux"}


def attendus():
    return {r["id"]: r for r in csv.DictReader(open(ROOT / "bench_acp.tsv"), delimiter="\t")}


def noter(i, qid, note, com):
    att = attendus().get(qid, {}).get("attendu", "")
    opts = "".join(f"<label><input type=radio name=note value={k}{' checked' if note == k else ''}> {escape(v)}</label> "
                   for k, v in NOTES.items())
    return (f"<div class='r'><b>Réponse attendue</b> <span class=m>(rédigée par Claude, à valider)</span><p>{escape(att)}</p>"
            f"<form method=post action='/tests/{i}/note'>{opts}<br><textarea name=commentaire rows=2 "
            f"placeholder='commentaire (erreur, manque, piège raté…)'>{escape(com or '')}</textarea>"
            f"<button>Enregistrer la note</button></form></div>")


@app.post("/tests/<int:i>/note")
def enregistrer_note(i):
    note = request.form.get("note")
    if note not in NOTES:
        abort(400)
    con = sqlite3.connect(ROOT / "ask_log.db")
    con.execute("UPDATE asks SET note=?, commentaire=? WHERE id=? AND origine LIKE 'test:bench%'",
                (note, request.form.get("commentaire", "")[:2000], i))
    con.commit()
    return redirect(f"/tests/{i}")


@app.get("/bench")
def bench():
    """Benchmark ACP : 20 questions (bench_acp.tsv), une ligne par question, une colonne par série bench_*."""
    con = sqlite3.connect(ROOT / "ask_log.db")
    rows = con.execute("SELECT id, origine, modele, n_ok, n_warn, n_bad, duree_s, note FROM asks "
                       "WHERE origine LIKE 'test:bench%' ORDER BY id").fetchall()
    att = attendus()
    qtext = {k: r["question"] for k, r in att.items()}
    cases, series = {}, {}
    for i, o, m, ok, wa, ba, du, no in rows:
        _, tag, qid = o.split(":", 2)
        if qid not in att:                          # lignes parasites (fichier réécrit pendant bench_acp_1)
            continue
        series[tag] = m
        cases.setdefault(qid, {})[tag] = (i, ok, wa, ba, du, no)
    th = "".join(f"<th>{escape(t)}<br><span class=m>{escape(m or '')}</span></th>" for t, m in series.items())
    score = {t: [c[t][5] for c in cases.values() if t in c and c[t][5]] for t in series}
    pied = "".join(f"<td><b>{sum(int(n) for n in v if n.isdigit())}/{2 * len(v)}</b> <span class=m>{len(v)} notées"
                   f"{', ' + str(v.count('D')) + ' D' if 'D' in v else ''}</span></td>" for v in score.values())
    corps = ("<h3>Benchmark ACP — 20 questions</h3><p class=m>DeepSeek V4 Flash (Albert, mode réflexion) cherche dans les "
             "livres et répond ; gpt-oss-120b relit chaque élément. Par case : vert / orange / retirés · durée. "
             "Cliquer pour la réponse, les passages, la chaîne de raisonnement (COT) et la noter contre la réponse attendue "
             "(2 juste · 1 partiel · 0 faux · D dangereux).</p>"
             f"<div style=overflow-x:auto><table><tr><th>#</th><th>question</th><th>réponse attendue</th>{th}</tr>")
    for k, (qid, q) in enumerate(qtext.items(), 1):
        d = cases.get(qid, {})
        corps += f"<tr><td>{k}</td><td>{escape(q)}</td><td class=m>{escape(att[qid]['attendu'])}</td>" + "".join(
            f"<td><a href='/tests/{d[t][0]}'>{d[t][1]}/{d[t][2]}/{d[t][3]}</a> <span class=m>{round(d[t][4] or 0)} s</span>"
            f"{' · <b>note ' + escape(d[t][5]) + '</b>' if d[t][5] else ''}</td>"
            if t in d else "<td class=m>…</td>" for t in series) + "</tr>"
    corps += f"<tr><td></td><td><b>Score</b></td><td></td>{pied}</tr>"
    return page("Benchmark ACP", corps + "</table></div>")


@app.post("/api/ask")
def api_ask():
    j = request.get_json(silent=True) or {}
    q, pid = (j.get("q") or "").strip(), j.get("parent")
    if len(q) < 8:
        return jsonify({"error": "Question trop courte."}), 400
    if motif := ask_anapath.filtre_question(q):         # avant tout envoi à Albert ; la question n'est pas journalisée
        print("question refusée :", motif.split(" :")[0], flush=True)
        return jsonify({"error": "Question refusée — " + motif}), 400
    aid = uuid.uuid4().hex[:10]
    ASKS[aid] = {"q": q, "status": "running", "steps": [], "t0": time.time()}
    if j.get("mode") == "dd":
        ASKS[aid] |= {"mode": "dd", "origine": "web:dd"}
    if pid:                                            # « Préciser » : 1 question + 2 précisions au plus
        par = ASKS.get(pid)
        if not par or par["status"] != "done" or "_shown" not in par:
            return jsonify({"error": "Échange précédent introuvable (serveur redémarré ?) : repose la question."}), 400
        if par.get("profondeur", 0) >= 2:
            return jsonify({"error": "Deux précisions maximum : pose une nouvelle question."}), 400
        ASKS[aid] |= {"parent": par, "profondeur": par.get("profondeur", 0) + 1, "_hist": par["_hist"],
                      "origine": f"web:precision{par.get('profondeur', 0) + 1}:{par.get('racine', pid)}",
                      "racine": par.get("racine", pid)}
    threading.Thread(target=ask_anapath.run, args=(ASKS[aid], verrou), daemon=True).start()
    return jsonify({"id": aid})


@app.get("/api/ask/<aid>")
def api_ask_get(aid):
    a = ASKS.get(aid) or abort(404)
    return jsonify({k: v for k, v in a.items() if k != "answer_raw" and k != "parent" and not k.startswith("_")}
                   | {"elapsed": round(time.time() - a["t0"])})


if __name__ == "__main__":
    recherche.model()                                  # chargé au démarrage, pas à la 1re requête
    if recherche.DENSE == "pplx":
        recherche._pplx()                              # ~18 Go de RAM, CPU (ACP_PPLX_DEVICE) : chargé une fois
        threading.Thread(target=recherche.servir_encodeur, daemon=True,   # requêtes du Demander fœto (data.pazuzu)
                         args=(int(os.environ.get("ACP_PPLX_PORT", "5089")), verrou)).start()
    app.run("127.0.0.1", 5087, threaded=True)
