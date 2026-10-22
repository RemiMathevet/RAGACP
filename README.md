# RAGACP — RAG documentaire d'anatomie pathologique générale

Recherche dans des livres d'anatomie et cytologie pathologiques, réponses **citées mot pour mot** et vérifiées.
Travail académique personnel, outil de test : ce n'est pas un dispositif médical ni un avis diagnostique.

Le fonctionnement détaillé est dans [METHODE.md](METHODE.md). En bref :

1. **Corpus** — PDF de livres (preuve) et de cours (preuve inférieure), découpés en chapitres par leurs signets
   (`split_anapath.py`), OCR des scans (`ocr_scans.py`), passages de ≤ 512 mots, page imprimée retrouvée
   (`pages_anapath.py`), bibliographies écartées (`biblio_anapath.py`), édition et année de chaque source
   (`editions_anapath.py`), Libre Pathology en couche « cours » (`fetch_librepathology.py`, CC BY-NC-SA 4.0).
2. **Index** — vecteurs BioLORD-2023-M (FR et EN dans un même espace) + FTS5/BM25 dans SQLite (sqlite-vec)
   (`embed_anapath.py`) ; terminologie ADICAP 2024 + MeSH FR/EN (`build_terms.py`).
3. **Recherche** — dense + mots-clés, élargie par le MeSH, fusion par rang réciproque (`recherche.py`).
4. **Réponse** — un LLM mène la recherche en plusieurs tours et rend des éléments *phrase + verbatim + passage* ;
   chaque verbatim est contrôlé dans le texte du livre, puis un second modèle (relecteur) vérifie qu'il soutient
   la phrase ; ce qui ne passe pas est retiré (`ask_anapath.py`). Mode diagnostics différentiels (`dd_anapath.py`).
5. **Fiches** — par entité, rubriques remplies avec le même contrôle (`extract_fiche.py`, `assemble_fiche.py`,
   `comparer_fiches.py`).
6. **Évaluation** — jeux de questions rejouables (`rejouer_tests.py`, `tests_rag.tsv`, `bench_acp.tsv`),
   comparaison d'index (`pplx_anapath.py`, `pplx/gold.tsv`).
7. **Site** — `web_acp.py` (Flask) : connexion, « Demander », benchmark noté, fiches, méthode ; filtre d'entrée
   contre les données identifiantes et l'injection de consignes.

## Ce que le dépôt ne contient pas

Ni livres, ni cours, ni bases, ni fiches : ce sont des extraits d'ouvrages sous droits. Les bases construites
(passages, vecteurs, terminologie, fiches) se téléchargent sur invitation, par un lien personnel à usage unique
(`acp_tokens.py`, `acp_paquet.py`), pour un usage personnel non redistribuable.

## Lancer

```bash
pip install -r requirements.txt
python split_anapath.py && python embed_anapath.py && python pages_anapath.py   # depuis vos PDF (livres/, cours/)
python build_terms.py                                                            # ADICAP + MeSH (refs/)
export ACP_USER=... ACP_PASS=...                                                 # aucun défaut
ACP_LLM=albert python web_acp.py                                                 # ou Qwen local via un broker
```

LLM : `ACP_LLM=albert` (API Albert : clé dans `~/.config/albert.key`), ou un serveur local compatible.
`vendor/` : deux modules d'un projet voisin (découpage, stockage, pages), copiés tels quels.

## Licence

Code : MIT. Données : non incluses ; Libre Pathology reste sous CC BY-NC-SA 4.0.
