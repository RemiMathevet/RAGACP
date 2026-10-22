#!/usr/bin/env python3
"""RAG v2 — Chunking sémantique + BioLORD embeddings.

Trois corpus :
  - chapitres : .txt/.pdf dans chapitres/ (ouvrages référence fœtopathologie)
  - abstracts : case_reports depuis syndromes_foetaux.db
  - vignettes : synthetic_vignettes depuis syndromes_foetaux.db

Usage:
    python embed_biolord.py                     # embed tout
    python embed_biolord.py --source chapitres  # un seul corpus
    python embed_biolord.py --source abstracts
    python embed_biolord.py --source vignettes
    python embed_biolord.py --reembed           # force re-embed
    python embed_biolord.py --stats             # stats sans embed
    python embed_biolord.py --dry-run           # chunk sans embed
"""

import argparse
import json
import os
import re
import sqlite3
import struct
import sys
import time
from pathlib import Path

import numpy as np
import pysbd

FOETO_DB = os.environ.get(
    "ORACULUM_SYNDROME_DB",
    str(Path(__file__).resolve().parent.parent / "foeto_base" / "syndromes_foetaux.db"),
)
RAG_DB = str(Path(__file__).resolve().parent / "rag_v2.db")
CHAPITRES_DIR = str(Path(__file__).resolve().parent / "chapitres")

MODEL_NAME = "FremyCompany/BioLORD-2023"
EMBEDDING_DIM = 768
BATCH_SIZE = 64
MAX_WORDS = 512
OVERLAP_SENTENCES = 3
MIN_CHUNK_WORDS = 50

_seg_en = pysbd.Segmenter(language="en", clean=False)
_seg_fr = pysbd.Segmenter(language="fr", clean=False)

# ponytail: detect language by counting French-specific chars/words, upgrade to langdetect if needed
_FR_MARKERS = re.compile(r"[àâéèêëïîôùûüÿçœæ]|(?:^|\s)(?:le|la|les|des|une?|du|est|dans|avec|pour|qui|que|sont|par|cette|ces)\s", re.I)

def detect_lang(text):
    sample = text[:2000]
    return "fr" if len(_FR_MARKERS.findall(sample)) > 10 else "en"


def sentence_split(text, lang=None):
    if lang is None:
        lang = detect_lang(text)
    seg = _seg_fr if lang == "fr" else _seg_en
    return seg.segment(text)


def chunk_semantic(text, lang=None, max_words=MAX_WORDS, overlap=OVERLAP_SENTENCES):
    """Chunking sémantique hiérarchique : paragraphe → phrase."""
    if not text or len(text.strip()) < 30:
        return []

    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    chunks = []
    para_idx = 0

    for para in paragraphs:
        words = para.split()
        if len(words) < MIN_CHUNK_WORDS:
            continue

        if len(words) <= max_words:
            chunks.append({
                "text": para,
                "paragraph_id": para_idx,
                "chunk_index": 0,
                "total_chunks": 1,
            })
        else:
            sentences = sentence_split(para, lang)
            current_sents = []
            current_words = 0
            chunk_idx = 0
            sub_chunks = []

            for sent in sentences:
                sw = len(sent.split())
                if current_words + sw > max_words and current_sents:
                    sub_chunks.append(current_sents[:])
                    # overlap: keep last N sentences
                    keep = current_sents[-overlap:]
                    current_sents = keep
                    current_words = sum(len(s.split()) for s in current_sents)
                current_sents.append(sent)
                current_words += sw

            if current_sents:
                sub_chunks.append(current_sents)

            total = len(sub_chunks)
            for i, sents in enumerate(sub_chunks):
                chunk_text = " ".join(s.strip() for s in sents)
                if len(chunk_text.split()) < MIN_CHUNK_WORDS:
                    continue
                chunks.append({
                    "text": chunk_text,
                    "paragraph_id": para_idx,
                    "chunk_index": i,
                    "total_chunks": total,
                })

        para_idx += 1

    return chunks


# -- Schema ------------------------------------------------------------------

def init_db(db_path=RAG_DB):
    import sqlite_vec
    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS chunk_meta (
            rowid         INTEGER PRIMARY KEY,
            source_type   TEXT NOT NULL,
            source_short  TEXT,
            isbn          TEXT,
            pmid          TEXT,
            year          INTEGER,
            language      TEXT,
            chapter_num   INTEGER,
            chapter_title TEXT,
            section       TEXT,
            paragraph_id  TEXT,
            chunk_index   INTEGER DEFAULT 0,
            total_chunks  INTEGER DEFAULT 1,
            page_start    INTEGER,
            organ_system  TEXT,
            foeto_terms   TEXT,
            chunk_text    TEXT NOT NULL,
            embedded_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_cm_source ON chunk_meta(source_type, source_short);
        CREATE INDEX IF NOT EXISTS idx_cm_organ ON chunk_meta(organ_system);
        CREATE INDEX IF NOT EXISTS idx_cm_para ON chunk_meta(paragraph_id);
    """)
    conn.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks
        USING vec0(embedding float[{EMBEDDING_DIM}])
    """)
    conn.commit()
    return conn


def serialize_vec(vec):
    return struct.pack(f"{len(vec)}f", *vec)


# -- Source: chapitres --------------------------------------------------------

# ponytail: map dirname → metadata, extend when adding new books
BOOK_META = {
    "soffoet":    {"source_short": "soffoet",     "year": 2008, "language": "fr", "isbn": None},
    "keeling":    {"source_short": "keeling",     "year": 2015, "language": "en", "isbn": "978-3-319-19206-7"},
    "ernst":      {"source_short": "ernst",       "year": 2016, "language": "en", "isbn": None},
    "devneuro":   {"source_short": "devneuro",    "year": 2018, "language": "en", "isbn": None},
    "benirschke": {"source_short": "benirschke",  "year": 2012, "language": "en", "isbn": "978-3-642-23940-3"},
    "ashworth":   {"source_short": "ashworth",    "year": 2012, "language": "en", "isbn": None},
    "saudubray":  {"source_short": "saudubray",   "year": 2016, "language": "en", "isbn": "978-3-662-49770-8"},
    "perineuro":  {"source_short": "perineuro",   "year": 2021, "language": "en", "isbn": None},
    "verdijk":    {"source_short": "verdijk",     "year": 2020, "language": "en", "isbn": "978-3-030-36079-5"},
    "spranger":   {"source_short": "spranger",    "year": 2018, "language": "en", "isbn": "978-0-19-062665-5"},
    # syndromologie (2026-09-11) : decoupes au grain du SYNDROME par split_syndromes.py
    "smith":      {"source_short": "smith",       "year": 2013, "language": "en", "isbn": "978-1-4557-3811-3"},  # Jones, Recognizable Patterns, 7e
    "limb":       {"source_short": "limb",        "year": None, "language": "en", "isbn": None},                 # atlas malformations des membres
    "eye":        {"source_short": "eye",         "year": None, "language": "en", "isbn": None},                 # pathologie oculaire foetale
    # corpus placentaire (chapitres_placenta/ -> placenta_rag.db)
    "khong2019":  {"source_short": "khong2019",   "year": 2019, "language": "en", "isbn": "978-3-319-97214-5"},
    "vogel":      {"source_short": "vogel",       "year": 2019, "language": "en", "isbn": "978-3-11-044997-6"},
    "amsterdam":  {"source_short": "amsterdam",   "year": 2016, "language": "en", "isbn": None},
    "horii2018":  {"source_short": "horii2018",   "year": 2018, "language": "en", "isbn": None},
    "recurrent":  {"source_short": "recurrent",   "year": 2017, "language": "en", "isbn": None},
    "lherminecoulomb": {"source_short": "lherminecoulomb", "year": 2019, "language": "fr", "isbn": None},
}

_CH_RE = re.compile(r"ch(\d+)_(.+)")

def parse_chapter_filename(fname):
    m = _CH_RE.match(Path(fname).stem)
    if m:
        return int(m.group(1)), m.group(2).replace("_", " ")
    return None, Path(fname).stem


def prepare_chapitres(conn, existing, chapitres_dir=CHAPITRES_DIR):
    items = []
    cdir = Path(chapitres_dir)
    if not cdir.is_dir():
        print(f"  chapitres dir not found: {cdir}")
        return items

    for book_dir in sorted(cdir.iterdir()):
        if not book_dir.is_dir():
            # ponytail: skip PDFs at top level, already split into .txt chapters
            continue

        book_name = book_dir.name
        meta = BOOK_META.get(book_name, {"source_short": book_name, "year": None, "language": "en", "isbn": None})

        for fpath in sorted(book_dir.iterdir()):
            if fpath.suffix.lower() not in (".txt", ".md"):
                continue

            ch_num, ch_title = parse_chapter_filename(fpath.name)
            source_id = f"{meta['source_short']}/ch{ch_num}"
            if source_id in existing:
                continue

            text = fpath.read_text(encoding="utf-8", errors="replace")
            lang = meta["language"]
            chunks = chunk_semantic(text, lang=lang)

            for c in chunks:
                items.append({
                    "source_type": "book",
                    "source_short": meta["source_short"],
                    "isbn": meta["isbn"],
                    "pmid": None,
                    "year": meta["year"],
                    "language": lang,
                    "chapter_num": ch_num,
                    "chapter_title": ch_title,
                    "section": None,
                    "paragraph_id": f"{book_name}_ch{ch_num or 0}_p{c['paragraph_id']}",
                    "chunk_index": c["chunk_index"],
                    "total_chunks": c["total_chunks"],
                    "page_start": None,
                    "organ_system": None,
                    "foeto_terms": None,
                    "chunk_text": c["text"],
                    "_source_id": source_id,
                })

    return items


# -- Source: abstracts --------------------------------------------------------

def prepare_abstracts(conn_rag, existing, foeto_db=FOETO_DB):
    items = []
    if not os.path.exists(foeto_db):
        print(f"  foeto_base DB not found: {foeto_db}")
        return items

    fconn = sqlite3.connect(foeto_db)
    rows = fconn.execute(
        "SELECT id, pmid, gold_diagnosis, clinical_text FROM case_reports "
        "WHERE clinical_text IS NOT NULL AND length(clinical_text) > 100"
    ).fetchall()
    fconn.close()

    for cid, pmid, gold, text in rows:
        source_id = f"pubmed_{cid}"
        if source_id in existing:
            continue

        lang = detect_lang(text)
        chunks = chunk_semantic(text, lang=lang)

        for c in chunks:
            items.append({
                "source_type": "article",
                "source_short": "pubmed",
                "isbn": None,
                "pmid": pmid or str(cid),
                "year": None,
                "language": lang,
                "chapter_num": None,
                "chapter_title": gold,
                "section": None,
                "paragraph_id": f"pubmed_{cid}_p{c['paragraph_id']}",
                "chunk_index": c["chunk_index"],
                "total_chunks": c["total_chunks"],
                "page_start": None,
                "organ_system": None,
                "foeto_terms": None,
                "chunk_text": c["text"],
                "_source_id": source_id,
            })

    return items


# -- Source: vignettes --------------------------------------------------------

def prepare_vignettes(conn_rag, existing, foeto_db=FOETO_DB):
    items = []
    if not os.path.exists(foeto_db):
        print(f"  foeto_base DB not found: {foeto_db}")
        return items

    fconn = sqlite3.connect(foeto_db)
    rows = fconn.execute(
        "SELECT id, syndrome_id, vignette_index, clinical_text FROM synthetic_vignettes "
        "WHERE clinical_text IS NOT NULL AND length(clinical_text) > 100"
    ).fetchall()
    fconn.close()

    for vid, syndrome_id, vig_idx, text in rows:
        source_id = f"vignette_{vid}"
        if source_id in existing:
            continue

        lang = detect_lang(text)
        chunks = chunk_semantic(text, lang=lang)

        for c in chunks:
            items.append({
                "source_type": "vignette",
                "source_short": syndrome_id,
                "isbn": None,
                "pmid": None,
                "year": None,
                "language": lang,
                "chapter_num": None,
                "chapter_title": f"{syndrome_id}_v{vig_idx}",
                "section": None,
                "paragraph_id": f"vignette_{vid}_p{c['paragraph_id']}",
                "chunk_index": c["chunk_index"],
                "total_chunks": c["total_chunks"],
                "page_start": None,
                "organ_system": None,
                "foeto_terms": None,
                "chunk_text": c["text"],
                "_source_id": source_id,
            })

    return items


# -- Embed & store ------------------------------------------------------------

COLUMNS = [
    "source_type", "source_short", "isbn", "pmid", "year", "language",
    "chapter_num", "chapter_title", "section", "paragraph_id",
    "chunk_index", "total_chunks", "page_start", "organ_system",
    "foeto_terms", "chunk_text",
]

def embed_and_store(items, conn, batch_size=BATCH_SIZE):
    if not items:
        print("Nothing to embed.")
        return

    from sentence_transformers import SentenceTransformer
    print(f"Loading {MODEL_NAME}...")
    model = SentenceTransformer(MODEL_NAME)

    texts = [it["chunk_text"] for it in items]
    print(f"Encoding {len(texts)} chunks...")
    t0 = time.time()
    embeddings = model.encode(texts, batch_size=batch_size, normalize_embeddings=True,
                              show_progress_bar=True)
    elapsed = time.time() - t0
    print(f"Encoded in {elapsed:.1f}s ({len(texts) / max(elapsed, 0.1):.0f} chunks/s)")

    placeholders = ", ".join(["?"] * len(COLUMNS))
    col_names = ", ".join(COLUMNS)
    insert_sql = f"INSERT INTO chunk_meta ({col_names}) VALUES ({placeholders})"

    print("Storing...")
    for i, item in enumerate(items):
        vals = [item.get(c) for c in COLUMNS]
        cur = conn.execute(insert_sql, vals)
        rowid = cur.lastrowid
        conn.execute(
            "INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)",
            (rowid, serialize_vec(embeddings[i])),
        )
        if (i + 1) % 1000 == 0:
            conn.commit()
            print(f"  {i + 1}/{len(items)}")

    conn.commit()
    print(f"Stored {len(items)} chunks.")


def get_existing(conn, source_type):
    try:
        rows = conn.execute(
            "SELECT DISTINCT paragraph_id FROM chunk_meta WHERE source_type = ?",
            [source_type],
        ).fetchall()
        # build source_id set from paragraph_ids
        # For dedup we track _source_id via a separate approach
        return set()
    except sqlite3.OperationalError:
        return set()


def get_existing_source_ids(conn):
    """Extract unique source identifiers already embedded."""
    try:
        rows = conn.execute(
            "SELECT source_type, source_short, chapter_num, paragraph_id FROM chunk_meta"
        ).fetchall()
        ids = set()
        for stype, sshort, cnum, pid in rows:
            if stype == "book" and sshort:
                # Le nom du fichier n'est pas en base : dédupliquer sur (livre, n° de chapitre),
                # la seule clé reconstructible. Un chapitre renommé ne sera pas réingéré.
                ids.add(f"{sshort}/ch{cnum}")
            elif stype == "article" and pid:
                m = re.match(r"pubmed_(\d+)", pid)
                if m:
                    ids.add(f"pubmed_{m.group(1)}")
            elif stype == "vignette" and pid:
                m = re.match(r"vignette_(\d+)", pid)
                if m:
                    ids.add(f"vignette_{m.group(1)}")
        return ids
    except sqlite3.OperationalError:
        return set()


def print_stats(conn):
    try:
        stats = conn.execute("""
            SELECT source_type, COUNT(*) as n, COUNT(DISTINCT source_short) as n_sources,
                   COUNT(DISTINCT paragraph_id) as n_paras
            FROM chunk_meta GROUP BY source_type
        """).fetchall()
        total_vec = conn.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
    except sqlite3.OperationalError:
        print("DB empty or not initialized.")
        return

    print(f"\n{'Source':<12} {'Chunks':>8} {'Sources':>8} {'Paragraphs':>10}")
    print("-" * 42)
    for stype, n, ns, np in stats:
        print(f"{stype:<12} {n:>8} {ns:>8} {np:>10}")
    print(f"\nTotal vectors: {total_vec}")


def main():
    parser = argparse.ArgumentParser(description="RAG v2 — Semantic chunking + BioLORD")
    parser.add_argument("--source", choices=["chapitres", "abstracts", "vignettes", "all"], default="all")
    parser.add_argument("--reembed", action="store_true", help="Drop existing and re-embed")
    parser.add_argument("--stats", action="store_true", help="Print stats only")
    parser.add_argument("--dry-run", action="store_true", help="Chunk only, no embedding")
    parser.add_argument("--db", default=RAG_DB)
    parser.add_argument("--foeto-db", default=FOETO_DB)
    parser.add_argument("--chapitres", default=CHAPITRES_DIR)
    args = parser.parse_args()

    conn = init_db(args.db)

    if args.stats:
        print_stats(conn)
        conn.close()
        return

    if args.reembed:
        print("Dropping existing data...")
        conn.execute("DELETE FROM chunk_meta")
        conn.execute("DELETE FROM vec_chunks")
        conn.commit()

    existing = set() if args.reembed else get_existing_source_ids(conn)
    all_items = []

    sources = {
        "chapitres": lambda: prepare_chapitres(conn, existing, args.chapitres),
        "abstracts": lambda: prepare_abstracts(conn, existing, args.foeto_db),
        "vignettes": lambda: prepare_vignettes(conn, existing, args.foeto_db),
    }

    targets = [args.source] if args.source != "all" else ["chapitres", "abstracts", "vignettes"]

    for src in targets:
        print(f"\nPreparing {src}...")
        items = sources[src]()
        print(f"  {src}: {len(items)} chunks")
        all_items.extend(items)

    if not all_items:
        print("\nNothing to embed.")
        print_stats(conn)
        conn.close()
        return

    print(f"\nTotal: {len(all_items)} chunks")

    if args.dry_run:
        print("Dry run — no embedding.")
        for src in targets:
            subset = [it for it in all_items if it["source_type"] == {"chapitres": "book", "abstracts": "article", "vignettes": "vignette"}[src]]
            if subset:
                print(f"\n  Sample {src}:")
                for it in subset[:3]:
                    print(f"    [{it['paragraph_id']}] {it['chunk_text'][:120]}...")
        conn.close()
        return

    embed_and_store(all_items, conn)
    print_stats(conn)
    conn.close()


if __name__ == "__main__":
    main()
