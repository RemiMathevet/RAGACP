# Modus operandi

Outil de test, documentaire : il cherche dans des livres d'anatomie pathologique et ne répond qu'avec ce qu'il peut citer. Ce n'est pas un avis diagnostique.

## 1. Le corpus
- **Livres** (89 ouvrages, ~69 000 passages) : traités, atlas, séries *Diagnostic Pathology* et *Biopsy Interpretation*, classifications OMS, Colvin, Robbins, Rosai… Marqués **[livre]** : c'est la preuve.
- **Cours** (959 documents, ~15 000 passages) : EPU, polycopiés, Libre Pathology (CC BY-NC-SA 4.0). Marqués **[cours]** : preuve inférieure, pondérée 0,8 à la recherche.
- Tri à l'entrée : ≥ 200 pages = livre (sauf EPU/polys), doublons et éditions anciennes écartés. Les scans sans texte passent par un OCR (Tesseract français + anglais), dont la couche de texte est inscrite dans le PDF.
- Chaque source porte son **édition et son année** (nom du fichier, pages de titre, ©) : affichées dans le titre de chaque passage, pour qu'une source antérieure à la classification demandée soit repérée (ex. OMS SNC 2016, pas 2021).
- Base **autonome** : rien de commun avec les bases fœto et placenta.

## 2. Le chapitrage et le découpage
- Chaque PDF est découpé en **chapitres** d'après ses **signets** (la table des matières du PDF) ; un cours est pris en entier.
- Chaque chapitre est coupé en **passages** d'au plus ~512 mots, aux limites de phrases, avec 3 phrases de recouvrement entre deux passages voisins.
- Chaque passage retrouve sa **page imprimée** (celle du livre papier, pas l'index du PDF) pour être cité.
- Les **bibliographies** sont sorties de l'index (9 190 passages) : un titre d'article n'est pas une preuve.

## 3. Les embeddings (vecteurs)
- Chaque passage est converti en un vecteur par **BioLORD-2023-M**, un modèle biomédical multilingue : une question en français et un passage en anglais tombent au même endroit (cosinus moyen 0,94 sur des paires de termes FR↔EN, contre 0,60 pour la version anglaise seule).
- En test : **pplx-embed-v2-context** (Perplexity), qui calcule le vecteur d'un passage en lisant tout son chapitre — pour qu'une ligne de tableau sache de quelle entité elle parle. Gardé seulement s'il fait mieux sur le jeu de 20 questions.

## 4. La recherche (retrieval)
Pour chaque requête, deux canaux fusionnés :
1. **Sens** : les 40 passages dont le vecteur est le plus proche de celui de la requête.
2. **Mots** : les 40 meilleurs passages par mots-clés (BM25) — indispensable pour les gènes, fusions et anticorps (CIC-DUX4, NKX2-2, R132H).
3. La requête est **élargie par le MeSH** (synonymes français et anglais).
4. Fusion par rang réciproque ; les cours pèsent 0,8.

Terminologie : **ADICAP 2024** (ANS) et MeSH FR/EN reliés ; un code ADICAP n'est donné que s'il sort de la table, jamais composé par le modèle.

## 5. La réponse (« Demander »)
1. Le modèle (**DeepSeek V4 Flash**, via **Albert**, l'API de la DINUM) mène lui-même la recherche : jusqu'à 4 tours, 2 requêtes par tour, en anglais, éventuellement dans un livre précis. Une requête trop proche d'une précédente est refusée ; les passages déjà vus ne sont pas renvoyés.
2. Il répond par une liste d'éléments : *phrase en français + citation exacte du livre + numéro du passage*.
3. **Contrôle mot pour mot** : chaque citation est cherchée dans le passage. Introuvable → l'élément est **retiré**. Presque exacte (ligne de tableau recomposée) → remplacée par le vrai texte du livre (**orange**).
4. **Relecteur** : un second modèle (**Claude Sonnet 5.5**, API Anthropic ; auparavant gpt-oss-120b, plafonné à 1 000 appels par jour) vérifie que la citation *soutient* la phrase — même entité, même édition. Contredit ou hors sujet → **retiré** ; soutien partiel → **orange**.
5. La **synthèse** (1–2 phrases) vient du savoir du modèle : elle n'est affichée en clair que si le relecteur la juge soutenue.
6. **Préciser** : une question puis deux précisions au plus ; seuls les éléments gardés et leurs passages sont transmis, jamais la synthèse ni les éléments retirés.
7. **Diagnostics différentiels** : même chaîne, les arguments rangés par diagnostic (pour / contre / distingue).

Couleurs : **vert** = citation exacte et soutenante · **orange** = recalée, autre passage ou soutien partiel · **retiré** = liste repliée, à titre d'information.

## 6. Les fiches
Par entité : le chapitre de référence du livre pivot (Colvin pour le rein, *Biopsy Interpretation of the Liver* pour le foie) + les passages des autres livres. Le modèle remplit des rubriques (définition, critères, mesure, gradation, colorations / IF, différentiel, pièges…), chaque élément avec sa citation ; même contrôle mot pour mot et même relecteur. Une rubrique tenue par les seuls cours est signalée.

## 7. Évaluation
- **Benchmark** : 20 questions, réponse attendue rédigée à part, note humaine (2 juste · 1 partiel · 0 faux · D dangereux). Chaque réponse garde sa chaîne de raisonnement (COT) et ses recherches.
- Tout est journalisé (question, requêtes, passages, réponse, alertes, durée) pour rejouer après chaque changement.

## 8. Données
- **Filtre d'entrée** : avant tout envoi au modèle, la question est refusée si elle contient un élément identifiant (nom précédé de M./Mme/patient, date de naissance ou date complète, n° de sécurité sociale, d'examen ou de dossier, courriel, téléphone) ou une consigne adressée au modèle (« ignore les instructions », « révèle ton prompt »…). Une question refusée n'est pas enregistrée. Le modèle a en plus pour règle de traiter la question et les passages comme des données, jamais comme des consignes.
- **Aucune donnée patient** : ni nom, ni date de naissance, ni n° d'examen, ni date ou lieu de prélèvement. Les questions partent vers Albert (serveurs de l'État) et sont enregistrées pour évaluer l'outil.
- Les passages de livres envoyés au modèle restent des extraits courts, pour un usage interne de test.
