"""Library walker + incremental ingest: extract -> chunk -> embed -> store.

Incremental: compares (size, mtime) against the docs table; only changed/new
files are re-extracted. Deleted files are dropped from the index.
Duplicates (identical normalized text) are marked dup_of and skipped.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from . import extract as X
from .chunk import chunk_units, embed_text
from .config import Config, DOC_EXTS, TEXT_EXTS, SKIP_EXTS, MEDIA_EXTS
from .db import connect, init_db

KIND_BY_EXT = {
    "pdf": "pdf", "pptx": "pptx", "docx": "docx", "msg": "msg",
}


def walk_files(cfg: Config):
    for root in cfg.roots:
        if not root.path.exists():
            print(f"!! missing root: {root.path}")
            continue
        for dirpath, dirnames, filenames in os.walk(root.path, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not _excluded(root, dirpath, d, dirnames))
            for fn in sorted(filenames):
                p = Path(dirpath) / fn
                yield root, p


def _excluded(root, dirpath: str, d: str, siblings) -> bool:
    if d.startswith(".") or d.lower() in ("__pycache__", "node_modules"):
        return True
    low = d.lower()
    return any(x.lower() in low for x in root.exclude)


def extract_doc(path: Path, cfg: Config) -> X.Extracted:
    ext = path.suffix.lower().lstrip(".")
    if ext == "pdf":
        return X.extract_pdf(path, ocr=cfg.ocr)
    if ext == "pptx":
        return X.extract_pptx(path)
    if ext == "docx":
        return X.extract_docx(path)
    if ext == "msg":
        return X.extract_msg(path)
    if ext == "zip" and cfg.index_archives:
        return X.extract_zip(path)
    if ext in TEXT_EXTS:
        return X.extract_text(path)
    return X.Extracted(note=f"unsupported extension .{ext}")


def build_or_update(cfg: Config, force: bool = False, use_llm_summaries: bool = False, limit: int = 0,
                    report: dict | None = None):
    """Scan all configured roots and index new/changed files. When `report` is
    passed it receives machine-readable counts (added/changed/removed/seconds/
    docs/chunks) so callers like `sync` and the web UI can summarize."""
    from .embed import Embedder

    probe = Embedder(cfg.embed_backend, cfg.embed_model, cfg.embed_base_url, cfg.embed_api_key)
    if cfg.embed_backend == "fastembed":
        probe._fast()  # loads model, sets dims
    elif not cfg.embed_base_url:
        raise SystemExit("embed.backend='openai' needs [embed] base_url")
    else:
        probe.dims = len(probe.embed_one("dims probe"))
    print(f"embeddings: {cfg.embed_backend} {cfg.embed_model} ({probe.dims}d)")

    db = init_db(cfg.db_path, probe.dims)
    existing = {r["path"]: r for r in db.execute("SELECT * FROM docs")}

    plans: list[tuple[str, Path]] = []   # ('new'|'update', path)
    seen: set[str] = set()
    for root, p in walk_files(cfg):
        sp = str(p)
        seen.add(sp)
        ext = p.suffix.lower().lstrip(".")
        if ext in SKIP_EXTS or (ext not in DOC_EXTS and ext not in TEXT_EXTS and not (ext == "zip" and cfg.index_archives)):
            if ext not in MEDIA_EXTS:
                continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size > cfg.max_file_mb * 1_000_000:
            continue
        row = existing.get(sp)
        if row is None:
            plans.append(("new", p))
        elif force or abs(st.st_mtime - row["mtime"]) > 1 or st.st_size != row["size"]:
            plans.append(("update", p))

    for sp in existing:
        if sp not in seen:
            _delete_doc(db, existing[sp]["id"])
            print(f"- removed {sp}")
    removed_n = sum(1 for sp in existing if sp not in seen)

    if limit:
        plans = plans[:limit]
    total = len(plans)
    print(f"{total} files to index ({sum(1 for k,_ in plans if k=='new')} new, "
          f"{sum(1 for k,_ in plans if k=='update')} changed)")

    import json
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('roots',?)",
               (json.dumps([str(r.path) for r in cfg.roots]),))
    db.commit()

    llm = None
    if use_llm_summaries and cfg.llm_base_url:
        from .llm import ChatLLM
        llm = ChatLLM(cfg.llm_base_url, cfg.llm_model, cfg.llm_api_key)

    t0 = time.time()
    done = 0
    for kind, p in plans:
        done += 1
        root_name = next((r.name for r in cfg.roots if str(p).startswith(str(r.path))), "")
        rel = p.relative_to(next(r.path for r in cfg.roots if str(p).startswith(str(r.path))))
        folder = str(rel.parent) if str(rel.parent) != "." else "/"
        if kind == "update":
            _delete_doc(db, existing[str(p)]["id"])
        try:
            _ingest_one(db, cfg, probe, llm, p, root_name, folder)
        except Exception as e:
            print(f"!! failed {p}: {type(e).__name__}: {e}")
            _insert_broken(db, p, root_name, folder, f"extraction error: {e}")
        if done % 25 == 0 or done == total:
            el = time.time() - t0
            print(f"  [{done}/{total}] {el:.0f}s elapsed, {p.name[:60]}")
            db.commit()

    _mark_duplicates(db)
    _link_transcripts(db)
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_build',?)", (str(time.time()),))
    db.commit()
    _vacuum_if_needed(db, cfg)
    n_docs = db.execute("SELECT COUNT(*) c FROM docs").fetchone()["c"]
    n_chunks = db.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]
    elapsed = time.time() - t0
    print(f"done: {n_docs} docs, {n_chunks} chunks, {elapsed:.0f}s, db={cfg.db_path} "
          f"({cfg.db_path.stat().st_size/1e6:.0f} MB)")
    if report is not None:
        report.update(
            added=sum(1 for k, _ in plans if k == "new"),
            changed=sum(1 for k, _ in plans if k == "update"),
            removed=removed_n,
            seconds=round(elapsed, 1),
            docs=n_docs,
            chunks=n_chunks,
        )
    return db


def _ingest_one(db, cfg, probe, llm, p: Path, root_name: str, folder: str):
    st = p.stat()
    ext = p.suffix.lower().lstrip(".")
    ex = extract_doc(p, cfg)
    title = ex.title or _guess_title(p)
    if not ex.units:
        kind = "media" if ext in MEDIA_EXTS else "empty"
        _insert_broken(db, p, root_name, folder, ex.note or "no text extracted", title=title, kind=kind)
        return

    chunks = chunk_units(ex.units)
    emb_texts = [embed_text(title, folder, lab, t) for lab, t in chunks]
    vectors = probe.embed_batch(emb_texts)

    sha = hashlib.sha256(ex.full_text.encode("utf-8", "ignore")).hexdigest()
    cur = db.execute(
        """INSERT INTO docs(path,root,folder,title,ext,kind,size,mtime,text_sha,pages,chars,
             year,doc_date,note,indexed_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (str(p), root_name, folder, title, ext, KIND_BY_EXT.get(ext, "text"),
         st.st_size, st.st_mtime, sha, ex.pages, len(ex.full_text),
         ex.year, ex.doc_date, ex.note, time.time()),
    )
    doc_id = cur.lastrowid

    # summaries (cheap extractive now; LLM pass optional)
    from .llm import extractive_summary
    summ = extractive_summary(ex.full_text)
    db.execute("UPDATE docs SET summary=?, summary_src='extractive' WHERE id=?", (summ, doc_id))
    if llm is not None:
        try:
            s = llm.summarize_doc(title, ex.full_text)
            if s:
                db.execute("UPDATE docs SET summary=?, summary_src='llm' WHERE id=?", (s, doc_id))
        except Exception:
            pass  # keep extractive

    for (lab, text), vec in zip(chunks, vectors):
        c = db.execute("INSERT INTO chunks(doc_id,ordinal,label,text) VALUES(?,?,?,?)",
                       (doc_id, len(chunks), lab, text))
        cid = c.lastrowid
        db.execute("INSERT INTO chunks_fts(rowid,title,text) VALUES(?,?,?)", (cid, title, text))
        db.execute("INSERT INTO vec_chunks(chunk_id,embedding) VALUES(?,?)",
                   (cid, _pack_f32(vec)))


def _pack_f32(vec):
    import struct
    return struct.pack(f"{len(vec)}f", *vec)


def _guess_title(p: Path) -> str:
    stem = p.stem
    stem = stem.replace("_", " ")
    return stem[:200]


def _insert_broken(db, p: Path, root_name: str, folder: str, note: str, title: str = "", kind: str = "empty"):
    st = None
    try:
        st = p.stat()
    except OSError:
        pass
    ext = p.suffix.lower().lstrip(".")
    note = note or ""
    if kind == "media":
        note = "video/audio file — searchable via its transcript if one exists" + (f"; {note}" if note else "")
    db.execute(
        """INSERT OR REPLACE INTO docs(path,root,folder,title,ext,kind,size,mtime,note,indexed_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (str(p), root_name, folder, title or _guess_title(p), ext, kind,
         st.st_size if st else 0, st.st_mtime if st else 0, note, time.time()),
    )


def _delete_doc(db, doc_id: int):
    rows = db.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,)).fetchall()
    ids = [r["id"] for r in rows]
    if ids:
        # chunks_fts is a contentful FTS5 table: delete by rowid directly.
        # The special 'delete' VALUES command requires the EXACT original column
        # values; passing an empty title there raises 'SQL logic error' whenever
        # the stored title is non-empty.
        db.executemany("DELETE FROM chunks_fts WHERE rowid = ?", [(i,) for i in ids])
        db.executemany("DELETE FROM vec_chunks WHERE chunk_id=?", [(i,) for i in ids])
    db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    db.execute("DELETE FROM docs WHERE id=?", (doc_id,))


def _mark_duplicates(db):
    """Same normalized text -> earliest/cleanest copy is canonical."""
    groups = db.execute(
        """SELECT text_sha, GROUP_CONCAT(id) ids FROM docs
           WHERE text_sha IS NOT NULL AND text_sha != '' AND dup_of IS NULL
           GROUP BY text_sha HAVING COUNT(*) > 1"""
    ).fetchall()
    n = 0
    for g in groups:
        ids = [int(x) for x in g["ids"].split(",")]
        # canonical: prefer title without 'copy/compressed/reduced/draft', then shortest path
        docs = [dict(db.execute("SELECT * FROM docs WHERE id=?", (i,)).fetchone()) for i in ids]
        def score(d):
            t = d["title"].lower()
            bad = any(w in t for w in ("copy", "compressed", "reduced", "final final", "draft"))
            return (1 if bad else 0, len(d["path"]), d["id"])
        docs.sort(key=score)
        canon = docs[0]
        for d in docs[1:]:
            db.execute("UPDATE docs SET dup_of=?, note=COALESCE(note||'; ','')||? WHERE id=?",
                       (canon["id"], f"duplicate of #{canon['id']}", d["id"]))
            n += 1
    if n:
        print(f"marked {n} duplicate copies")


def _link_transcripts(db):
    """txt files whose stem matches a nearby video file -> kind=video_transcript, note the video."""
    import json
    import re
    roots = db.execute("SELECT value FROM meta WHERE key='roots'").fetchone()
    if not roots:
        return
    media: dict[str, dict[str, list[str]]] = {}  # dir -> {stem: [paths]}
    for r in json.loads(roots["value"]):
        rp = Path(r)
        if not rp.exists():
            continue
        for dp, _, fns in os.walk(rp):
            for fn in fns:
                p = Path(dp) / fn
                if p.suffix.lower().lstrip(".") in MEDIA_EXTS:
                    media.setdefault(dp, {})[p.stem.lower()] = [str(p)]
    n = 0
    for r in db.execute("SELECT id,path,kind FROM docs WHERE kind='text'").fetchall():
        p = Path(r["path"])
        stem = p.stem.lower()
        dirmedia = media.get(str(p.parent), {})
        if not dirmedia:
            continue
        hit = dirmedia.get(stem)          # 1. exact stem match
        note = f"transcript of {hit[0]}" if hit else None
        if not hit and stem.endswith("-transcript"):
            # 2. token overlap: 'Product Review Q3-transcript' ~ 'Review_Q3.mp4'
            words = [w for w in re.split(r"[^a-z0-9]+", stem[: -len("-transcript")]) if len(w) > 2]
            best, best_score = None, 0
            for vid_stem, paths in dirmedia.items():
                vnorm = vid_stem.replace("_", "").replace("-", "")
                score = sum(1 for w in words if w in vnorm or (len(w) > 4 and w[:5] in vnorm))
                if score > best_score:
                    best, best_score = paths, score
            if best and best_score >= 1:
                hit = best
                note = f"transcript of {hit[0]} (name-similar)"
        if not hit:
            # 3. same folder has media: at least tell the agent
            note = f"transcript; {len(set(sum(dirmedia.values(), [])))} video file(s) in same folder"
        db.execute("UPDATE docs SET kind='video_transcript', note=COALESCE(note||'; ','')||? WHERE id=?",
                   (note, r["id"]))
        n += 1
    if n:
        print(f"linked {n} video transcripts to their media files")


def _vacuum_if_needed(db, cfg):
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def status(cfg: Config) -> dict:
    import json
    if not cfg.db_path.exists():
        return {"indexed": False, "db": str(cfg.db_path)}
    db = connect(cfg.db_path, read_only=True)

    def meta_val(key, default=None):
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    out = {
        "indexed": True,
        "db": str(cfg.db_path),
        "db_mb": round(cfg.db_path.stat().st_size / 1e6, 1),
        "docs": db.execute("SELECT COUNT(*) c FROM docs").fetchone()["c"],
        "with_text": db.execute("SELECT COUNT(*) c FROM docs WHERE chars>0").fetchone()["c"],
        "duplicates": db.execute("SELECT COUNT(*) c FROM docs WHERE dup_of IS NOT NULL").fetchone()["c"],
        "chunks": db.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"],
        "dims": int(meta_val("dims", 0)),
        "last_build": float(meta_val("last_build", 0) or 0),
        "roots": json.loads(meta_val("roots", "[]")),
        "folders": [dict(r) for r in db.execute(
            "SELECT folder, COUNT(*) c FROM docs WHERE dup_of IS NULL GROUP BY folder ORDER BY c DESC LIMIT 40")],
    }
    db.close()
    return out
