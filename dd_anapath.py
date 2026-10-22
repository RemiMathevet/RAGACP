#!/usr/bin/env python3
"""Discussion des diagnostics différentiels d'une entité, ancrée sur les livres (même moteur que « Demander »).

    ACP_LLM=albert python dd_anapath.py "sarcome avec réarrangement CIC"  [-o dd_cic.md]

Chaque argument (pour / contre / distingue) porte son verbatim, vérifié mot pour mot puis relu par un second modèle ;
les arguments non prouvés sont retirés. Sortie texte (ou Markdown avec -o) ; journalisé dans ask_log.db (origine cli:dd).
"""
import argparse, html, re, threading, time
import ask_anapath


def texte(h):
    h = re.sub(r"<li>", "\n- ", h)
    h = re.sub(r"<summary[^>]*>", "\n\n### ", re.sub(r"<br>", "\n  > ", h))
    h = re.sub(r"<p>|</ul>|<details>", "\n", h)
    return html.unescape(re.sub(r"<[^>]+>", "", h)).strip()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("entite")
    ap.add_argument("-o", "--out")
    a = ap.parse_args()
    ask = {"q": a.entite, "mode": "dd", "origine": "cli:dd", "status": "running", "steps": [], "t0": time.time()}
    ask_anapath.run(ask, threading.Lock())
    if ask["status"] != "done":
        raise SystemExit("erreur : " + ask.get("error", "?"))
    md = f"# Diagnostics différentiels : {a.entite}\n\n{texte(ask['answer_html'])}\n\n## Passages\n" + "".join(
        f"\n[#{p['n']}] {p['title']}\n" for p in ask["passages"])
    md += f"\n_{ask_anapath.MODEL}, {round(time.time() - ask['t0'])} s_\n"
    open(a.out, "w").write(md) if a.out else print(md)
