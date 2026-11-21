"""SQLite schema + connection helpers.

Single file DB: docs + chunks + FTS5 (BM25) + sqlite-vec (cosine KNN) + meta.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS docs (
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  root TEXT,                      -- library root name
  folder TEXT,                    -- folder path relative to root
  title TEXT,
  ext TEXT,
  kind TEXT,                      -- pdf|pptx|docx|msg|text|video_transcript|dup|empty
  size INTEGER,
  mtime REAL,
  text_sha TEXT,                  -- sha256 of normalized full text (dedupe)
  dup_of INTEGER,                 -- doc id of canonical copy if duplicate
  pages INTEGER,                  -- page/slide count
  chars INTEGER,
  year TEXT,
  doc_date TEXT,                  -- best-effort created/sent date
  summary TEXT,
  summary_src TEXT,               -- 'llm' | 'extractive'
  key_terms TEXT,
  note TEXT,                      -- e.g. linked video path, extraction warning
  indexed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_docs_folder ON docs(folder);
CREATE INDEX IF NOT EXISTS idx_docs_sha ON docs(text_sha);

CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  doc_id INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
  ordinal INTEGER,
  label TEXT,                     -- 'p.3', 'slide 5', ''
  text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

-- standalone FTS (rowid = chunks.id); title column is boosted in bm25()
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  title, text,
  tokenize = 'porter unicode61 remove_diacritics 2'
);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
  chunk_id INTEGER PRIMARY KEY,
  embedding float[{dims}] distance_metric=cosine
);
"""


def _load_vec(db: sqlite3.Connection):
    try:
        import sqlite_vec
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
    except Exception as e:  # pragma: no cover
        raise SystemExit(f"could not load sqlite-vec extension: {e}\n"
                         "pip install sqlite-vec (or .venv/bin/pip install -e .)")


def connect(db_path: Path, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        uri = f"file:{db_path}?mode=ro"
        db = sqlite3.connect(uri, uri=True)
    else:
        db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    _load_vec(db)
    return db


def init_db(db_path: Path, dims: int) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = connect(db_path)
    has = db.execute("SELECT name FROM sqlite_master WHERE name='chunks'").fetchone()
    vec = db.execute("SELECT name FROM sqlite_master WHERE name='vec_chunks'").fetchone()
    if has and not vec:
        raise SystemExit(
            f"{db_path} has a partial schema (earlier crashed run). Delete the file and rebuild."
        )
    if has:
        stored = db.execute("SELECT value FROM meta WHERE key='dims'").fetchone()
        if stored and int(stored["value"]) != dims:
            raise SystemExit(
                f"Index at {db_path} uses {stored['value']}-dim embeddings but config wants {dims}. "
                "Change embed model back, or delete the DB and rebuild."
            )
    else:
        db.executescript(SCHEMA.replace("{dims}", str(dims)))
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('dims',?)", (str(dims),))
        db.commit()
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version','1')")
    db.commit()
    return db


def get_meta(db: sqlite3.Connection, key: str, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default
