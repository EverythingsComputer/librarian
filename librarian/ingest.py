"""Library walker + incremental ingest: extract -> chunk -> embed -> store.

Incremental: compares (size, mtime) against the docs table; only changed/new
files are re-extracted. Deleted files are dropped from the index.
Duplicates (identical normalized text) are marked dup_of and skipped.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

from . import extract as X
from .chunk import chunk_units, embed_text
from .config import Config, DOC_EXTS, TEXT_EXTS, SKIP_EXTS, MEDIA_EXTS
from .db import connect, init_db

# single lock for every sync entry point (UI, MCP tool, CLI) — v0.2.9:
# ui._sync_lock and mcp._build_gate were separate, so an agent-driven
# update_index and a UI sync could run against the same SQLite file at once.
SYNC_LOCK = threading.Lock()

# set by server shutdown hooks and Ctrl-C; the ingest loop checks it between
# batches so a long build — especially its prune phase — actually stops
# (v0.2.9 incident: an agent cancelled update_index, the client gave up, and
# the server kept deleting rows through 'Finished server process').
GLOBAL_CANCEL = threading.Event()

# live view of the current build, readable by the UI /api/state and MCP
# status while the sync request itself is still blocking (v0.2.13: long OCR
# runs went silent for minutes per file with no way to see the current one)
PROGRESS = {"active": False, "done": 0, "total": 0, "file": "", "detail": "",
            "started": 0.0, "file_started": 0.0}


def _progress(**kw):
    global PROGRESS
    PROGRESS = {**PROGRESS, **kw}


def progress_snapshot() -> dict:
    p = dict(PROGRESS)
    if p.get("active"):
        p["file_seconds"] = round(time.time() - (p["file_started"] or p["started"] or time.time()))
    return p


def install_shutdown_hook(app):
    """Starlette/uvicorn apps call this so server shutdown stops an in-flight
    build instead of letting its prune outrun 'Finished server process' (A2)."""
    try:
        app.router.on_shutdown.append(GLOBAL_CANCEL.set)
    except AttributeError:
        pass

KIND_BY_EXT = {
    "pdf": "pdf", "pptx": "pptx", "docx": "docx", "msg": "msg",
}

_OOXML_DIRS = {"_rels", "docprops", "word", "xl", "ppt", "customxml"}


def _ooxml_internals(p: Path) -> bool:
    """True for the unzipped innards of an Office package (someone unpacked a
    .docx in place): [Content_Types].xml, word/document.xml, docProps/*, ...
    Those are XML schema boilerplate, not documents (v0.2.9 B1)."""
    if p.name.lower() == "[content_types].xml":
        return True
    if p.suffix.lower() != ".xml":
        return False
    return bool({x.lower() for x in p.parts[:-1]} & _OOXML_DIRS)


def walk_files(cfg: Config):
    for root in cfg.roots:
        if not root.path.exists():
            print(f"!! missing root: {root.path}")
            continue
        for dirpath, dirnames, filenames in os.walk(root.path, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not _excluded(root, dirpath, d, dirnames))
            for fn in sorted(filenames):
                p = Path(dirpath) / fn
                if _ooxml_internals(p):
                    continue
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


# Notes that prove a file failed for TOOL reasons (missing/broken extractor),
# not because the document itself lacks text. v0.2.5 retries these
# automatically once the tool is available, so upgrading the app repairs an
# index that was built with a broken one. Real scans carry the note
# "no text layer (N pages) — scanned?" and are never auto-retried.
TOOL_FAILURE_NOTES = ("pdftotext not installed", "pdftotext not found",
                      "pdftotext failed", "no text extracted",
                      "extraction error")


def is_tool_failure(note: "str | None") -> bool:
    n = note or ""
    return any(m in n for m in TOOL_FAILURE_NOTES)


def build_or_update(cfg: Config, force: bool = False, use_llm_summaries: bool = False, limit: int = 0,
                    report: dict | None = None, retry_empty: bool = False,
                    cancel: threading.Event | None = None):
    """Scan all configured roots and index new/changed files. When `report` is
    passed it receives machine-readable counts (added/changed/removed/seconds/
    docs/chunks) so callers like `sync` and the web UI can summarize.

    Prune safety (v0.2.9 — an agent's update_index wiped 2,501 rows when the
    USB root went briefly unreadable): a root that yields ZERO files this scan
    is treated as unreadable, and nothing under it is pruned; a prune that
    would remove a large fraction of the library is blocked outright; the
    prune runs at the END of the build and checks `cancel` between batches."""
    from .embed import Embedder

    cancel = cancel or GLOBAL_CANCEL
    GLOBAL_CANCEL.clear()   # single-flight (SYNC_LOCK) makes this safe

    probe = Embedder(cfg.embed_backend, cfg.embed_model, cfg.embed_base_url, cfg.embed_api_key)
    if cfg.embed_backend == "fastembed":
        probe._fast()  # loads model, sets dims
    elif not cfg.embed_base_url:
        raise SystemExit("embed.backend='openai' needs [embed] base_url")
    else:
        probe.dims = len(probe.embed_one("dims probe"))
    print(f"embeddings: {cfg.embed_backend} {cfg.embed_model} ({probe.dims}d)")

    db = init_db(cfg.db_path, probe.dims)
    existing = {r["path"]: dict(r) for r in db.execute("SELECT * FROM docs")}

    # B1 cleanup: previously-indexed OOXML package internals go now, whatever
    # the prune settings say.
    junk = [r for sp, r in existing.items() if _ooxml_internals(Path(sp))]
    for r in junk:
        _delete_doc(db, r["id"])
        print(f"- removed (office package internals) {r['path']}")
    if junk:
        existing = {sp: r for sp, r in existing.items()
                    if r["id"] not in {j['id'] for j in junk}}

    # B2 repair: titles double-encoded by the old PDF metadata path (Arabic
    # showed as 'Ø§Ù—...'). Fix in place — no re-extraction needed, and it
    # converges (repaired titles pass through fix_mojibake unchanged).
    from .extract import fix_mojibake as _fixt
    for r in list(existing.values()):
        t0 = r["title"] or ""
        t1 = _fixt(t0)
        if t1 != t0:
            db.execute("UPDATE docs SET title=? WHERE id=?", (t1, r["id"]))
            db.execute("""UPDATE chunks_fts SET title=?
                          WHERE rowid IN (SELECT id FROM chunks WHERE doc_id=?)""",
                       (t1, r["id"]))
            r["title"] = t1
            print(f"~ title repaired: {t0[:40]!r} -> {t1[:40]!r}")

    plans: list[tuple[str, Path]] = []   # ('new'|'update', path)
    seen: set[str] = set()
    scanned_per_root: dict[str, int] = {str(r.path): 0 for r in cfg.roots}
    for root, p in walk_files(cfg):
        sp = str(p)
        seen.add(sp)
        scanned_per_root[str(root.path)] = scanned_per_root.get(str(root.path), 0) + 1
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
    print("scanned: " + ", ".join(f"{k} -> {v} files" for k, v in scanned_per_root.items()))

    # self-heal (v0.2.5): re-extract empty stubs whose stored note says the
    # EXTRACTOR failed, now that a working one exists. Converges in one run:
    # success removes the stub; a real scan gets the "no text layer" note,
    # which is not in TOOL_FAILURE_NOTES.
    planned = {str(p) for _, p in plans}
    from .extract import has_poppler as _pop
    if retry_empty:
        for r in existing.values():
            sp = str(r["path"])
            if (r["kind"] == "empty" and (r["ext"] or "") == "pdf"
                    and sp not in planned and sp in seen and Path(sp).is_file()):
                plans.append(("update", Path(sp)))
                planned.add(sp)
    elif _pop():
        for r in existing.values():
            sp = str(r["path"])
            if (r["kind"] != "empty" or sp in planned or sp not in seen
                    or (r["ext"] or "") != "pdf"
                    or not is_tool_failure(r["note"] or "")):
                continue
            if Path(sp).is_file():
                plans.append(("update", Path(sp)))
                planned.add(sp)
    # OCR (v0.2.7): when OCR is enabled and available, scanned stubs get one
    # attempt. A failed attempt rewrites the note to "...OCR found no readable
    # text", which is excluded here, so it never re-OCRs on every sync.
    if cfg.ocr and not force:
        from .extract import ocr_available
        if ocr_available():
            for r in existing.values():
                sp = str(r["path"])
                note = r["note"] or ""
                if (r["kind"] != "empty" or (r["ext"] or "") != "pdf"
                        or sp in planned or sp not in seen
                        or not note.startswith("no text layer")
                        or "OCR found no readable text" in note):
                    continue
                if Path(sp).is_file():
                    plans.append(("update", Path(sp)))
                    planned.add(sp)

    # (prune moved to the END of the build, guarded — see v0.2.9 note above)

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
    cancelled = False
    _progress(active=True, done=0, total=total, file="", detail="", started=t0)
    for kind, p in plans:
        if cancel.is_set():
            cancelled = True
            print(f"!! cancelled after {done}/{total} files — nothing pruned; re-run sync to continue")
            break
        done += 1
        # announce BEFORE the work: while a 400-page scan OCRs for minutes,
        # the terminal/last log line always names the file being worked on
        sz = p.stat().st_size / 1e6 if p.exists() else 0
        _progress(done=done, file=p.name,
                  detail=f"{kind} {sz:.1f} MB" + (" +OCR" if cfg.ocr and p.suffix.lower() == ".pdf" else ""))
        print(f"> [{done}/{total}] {p.name} ({kind}, {sz:.1f} MB)", flush=True)
        file_t0 = time.time()
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
        fel = time.time() - file_t0
        if fel > 15:   # call out the slow ones so the pace is obvious
            print(f"   ^ finished in {fel:.0f}s", flush=True)
        if done % 25 == 0 or done == total:
            el = time.time() - t0
            print(f"  [{done}/{total}] {el:.0f}s elapsed, {p.name[:60]}")
            db.commit()

    # ---- guarded prune (v0.2.9) ------------------------------------------------
    _progress(active=True, file="", detail="pruning")
    if cancel.is_set():
        cancelled = True          # may have been set before/after the loop too
    # Rules learned from the incident where a transiently-unreadable USB root
    # looked like 2,501 deleted files and the index obeyed:
    #   * runs AFTER indexing, so a killed build never prunes;
    #   * a root that scanned 0 files this run is 'unreadable' — its docs are
    #     left alone, with a loud note;
    #   * if the prune would still remove >25% of the library (>100 docs),
    #     it is BLOCKED and only reported;
    #   * the loop is cancellable and commits in batches, so a cancel leaves
    #     a consistent index of whatever it already did.
    removed_n = 0
    prune_blocked = 0
    unreadable = {k for k, n in scanned_per_root.items() if n == 0}
    if unreadable:
        print("!! prune skipped for unreadable root(s) (0 files scanned): "
              + ", ".join(unreadable)
              + " — if that mount should be online, remount it and sync again")
    if cancelled:
        print("!! prune skipped (build was cancelled)")
    else:
        gone = []
        for sp in existing:
            if sp in seen:
                continue
            base = next((k for k in scanned_per_root if sp.startswith(k + os.sep)), None)
            if base in unreadable:
                continue
            gone.append(sp)
        under_ok = sum(1 for sp in existing
                       if next((k for k in scanned_per_root if sp.startswith(k + os.sep)), None)
                       not in unreadable)
        mass_ok = os.environ.get("LIBRARIAN_ALLOW_MASS_PRUNE") == "1"
        if not mass_ok and len(gone) > max(100, under_ok // 4):
            prune_blocked = len(gone)
            print(f"!! prune BLOCKED: {len(gone)} files vanished from disk at once "
                  f"(out of {under_ok}). That smells like a mount/network problem, "
                  f"not a mass delete — NOTHING removed. If the files really are "
                  f"gone on purpose, re-run with LIBRARIAN_ALLOW_MASS_PRUNE=1.")
        for i, sp in enumerate(gone):
            if prune_blocked or cancel.is_set():
                break
            _delete_doc(db, existing[sp]["id"])
            removed_n += 1
            print(f"- removed {sp}")
            if (i + 1) % 200 == 0:
                db.commit()
        if removed_n:
            print(f"prune: {removed_n} index row(s) removed for files gone from disk")
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
    _progress(active=False, done=total if not cancelled else done, file="", detail="")
    if report is not None:
        report.update(
            added=sum(1 for k, _ in plans if k == "new"),
            changed=sum(1 for k, _ in plans if k == "update"),
            removed=removed_n,
            prune_blocked=prune_blocked,
            cancelled=cancelled,
            junk=len(junk),
            seconds=round(elapsed, 1),
            docs=n_docs,
            chunks=n_chunks,
            textless=db.execute(
                "SELECT COUNT(*) c FROM docs WHERE kind='empty' AND dup_of IS NULL"
            ).fetchone()["c"],
            ocr=bool(cfg.ocr),
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


def status(cfg: Config, exclude_roots: list[str] | None = None) -> dict:
    """Index stats. exclude_roots (v0.3.0): when answering a shared-access
    PROFILE, hide the folders the profile cannot see — counts and folder map
    must not leak the existence of other libraries."""
    import json
    from .extract import tool_path
    excl = [e for e in (exclude_roots or [])]
    if not cfg.db_path.exists():
        out = {"indexed": False}
        if not excl:
            out["db"] = str(cfg.db_path)
        return out
    db = connect(cfg.db_path, read_only=True)
    rcond = ""
    rargs: list = []
    if excl:
        rcond = " AND (root IS NULL OR root NOT IN (" + ",".join("?" * len(excl)) + "))"
        rargs = list(excl)

    def meta_val(key, default=None):
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    out = {
        "indexed": True,
        "db_mb": round(cfg.db_path.stat().st_size / 1e6, 1),
        "docs": db.execute(f"SELECT COUNT(*) c FROM docs WHERE 1=1{rcond}", rargs).fetchone()["c"],
        "with_text": db.execute(
            f"SELECT COUNT(*) c FROM docs WHERE chars>0{rcond}", rargs).fetchone()["c"],
        "textless": db.execute(
            f"SELECT COUNT(*) c FROM docs WHERE kind='empty' AND dup_of IS NULL{rcond}",
            rargs).fetchone()["c"],
        "tools": {n: tool_path(n) for n in ("pdftotext", "pdfinfo", "pdftoppm", "tesseract")},
        "sync": progress_snapshot(),
        "ocr": {"enabled": bool(cfg.ocr), "available": X.ocr_available(),
                "tesseract": tool_path("tesseract"),
                "tessdata": X.tessdata_dir()},
        "duplicates": db.execute(
            f"SELECT COUNT(*) c FROM docs WHERE dup_of IS NOT NULL{rcond}", rargs).fetchone()["c"],
        "chunks": db.execute(
            "SELECT COUNT(*) c FROM chunks WHERE doc_id IN (SELECT id FROM docs WHERE 1=1"
            + rcond + ")", rargs).fetchone()["c"] if excl
            else db.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"],
        "dims": int(meta_val("dims", 0)),
        "last_build": float(meta_val("last_build", 0) or 0),
        "roots": [r for r in json.loads(meta_val("roots", "[]"))
                  if not excl or _root_name(r) not in excl],
        "folders": [dict(r) for r in db.execute(
            "SELECT folder, COUNT(*) c FROM docs WHERE dup_of IS NULL"
            + rcond + " GROUP BY folder ORDER BY c DESC LIMIT 40", rargs)],
    }
    if not excl:
        out["db"] = str(cfg.db_path)   # admin only: path reveals the account name
    else:
        out.pop("db_mb", None)         # global index size hints at hidden libraries
    db.close()
    return out


def _root_name(r) -> str:
    """meta['roots'] entries are dicts ({'name':...}) or plain strings."""
    return r.get("name", "") if isinstance(r, dict) else str(r)
