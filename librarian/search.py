"""Hybrid retrieval: FTS5 BM25 (title-boosted) + sqlite-vec cosine KNN, fused
with Reciprocal Rank Fusion, plus agent-oriented views:

  search()    - top chunks, hybrid/lexical/semantic
  coverage()  - CENSUS view: every doc touching a topic, with hit counts
  find_term() - exact phrase occurrences per doc
  related()   - docs nearest a doc in embedding space
  multi_search() - several phrasings in one call, results fused
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

from .chunk import embed_text

STOP = {"a","an","the","is","are","was","were","be","been","being","do","does","did",
        "of","to","in","on","for","with","as","by","at","from","and","or","not","no",
        "it","its","this","that","these","those","i","you","we","they","he","she",
        "what","which","how","why","when","where","who","me","my","our","your"}


@dataclass
class Hit:
    doc_id: int
    chunk_id: int
    title: str
    folder: str
    path: str
    kind: str
    label: str
    score: float
    snippet: str
    bm25_rank: int | None = None
    vec_rank: int | None = None

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items()}


# ---------------------------------------------------------------- query prep

def fts_query(query: str) -> str:
    """Keep user-quoted phrases intact; OR the rest for recall (bm25 ranks)."""
    phrases = re.findall(r'"([^"]+)"', query)
    rest = re.sub(r'"[^"]+"', " ", query)
    toks = [t for t in re.split(r"[^\w\-]+", rest.lower()) if t and t not in STOP and len(t) > 1]
    parts = [f'"{p.strip()}"' for p in phrases if p.strip()] + toks
    return " OR ".join(parts) if parts else '"{}"'.format(query.strip())


def fts_and_query(query: str) -> str:
    """Same terms as fts_query but AND-ed: matches chunks carrying the whole concept."""
    phrases = re.findall(r'"([^"]+)"', query)
    rest = re.sub(r'"[^"]+"', " ", query)
    toks = [t for t in re.split(r"[^\w\-]+", rest.lower()) if t and t not in STOP and len(t) > 1]
    parts = [f'"{p.strip()}"' for p in phrases if p.strip()] + toks
    return " AND ".join(parts) if parts else '"{}"'.format(query.strip())


def query_terms(query: str) -> list[str]:
    return [t for t in re.split(r"[^\w\-]+", query.lower()) if t and len(t) > 2]


# ---------------------------------------------------------------- primitives

def _rx(alias: str, exclude_roots: list[str] | None) -> tuple[str, list]:
    """SQL clause hiding docs from switched-off library roots (instant: index is kept)."""
    if not exclude_roots:
        return "", []
    ph = ",".join("?" * len(exclude_roots))
    return f"AND ({alias}.root IS NULL OR {alias}.root NOT IN ({ph}))", list(exclude_roots)


def _lexical(db, q: str, k: int, folder: str | None, exclude_roots: list[str] | None = None) -> list:
    fk = "AND (d.folder LIKE ? OR d.path LIKE ? OR d.root LIKE ?)" if folder else ""
    rk, rargs = _rx("d", exclude_roots)
    sql = f"""
      SELECT c.id AS chunk_id, c.doc_id, c.label, d.title, d.folder, d.path, d.kind,
             bm25(chunks_fts, 3.0, 1.0) AS r,
             snippet(chunks_fts, 1, '»', '«', ' … ', 14) AS snip
      FROM chunks_fts
      JOIN chunks c ON c.id = chunks_fts.rowid
      JOIN docs d ON d.id = c.doc_id
      WHERE chunks_fts MATCH ?
        AND d.dup_of IS NULL
        {fk}
        {rk}
      ORDER BY r
      LIMIT ?
    """
    args = [q] + ([f"%{folder}%"] * 3 if folder else []) + rargs + [k]
    return db.execute(sql, args).fetchall()


def _semantic(db, embedder, query: str, k: int, folder: str | None,
              exclude_roots: list[str] | None = None) -> list:
    qv = embedder.embed_one(query)
    pack = struct.pack(f"{len(qv)}f", *qv)
    fk = "AND (d.folder LIKE ? OR d.path LIKE ? OR d.root LIKE ?)" if folder else ""
    rk, rargs = _rx("d", exclude_roots)
    sql = f"""
      SELECT v.chunk_id, v.distance, c.doc_id, c.label, d.title, d.folder, d.path, d.kind
      FROM vec_chunks v
      JOIN chunks c ON c.id = v.chunk_id
      JOIN docs d ON d.id = c.doc_id
      WHERE v.embedding MATCH ? AND k = ? {fk} AND d.dup_of IS NULL {rk}
      ORDER BY v.distance
      LIMIT ?
    """
    args = [pack, k * 4] + ([f"%{folder}%"] * 3 if folder else []) + rargs + [k * 4]
    return db.execute(sql, args).fetchall()


def _text_snippet(db, chunk_id: int, terms: list[str], width: int = 380) -> str:
    row = db.execute("SELECT text FROM chunks WHERE id=?", (chunk_id,)).fetchone()
    if not row:
        return ""
    text = row["text"]
    pos = -1
    for t in terms:
        m = re.search(re.escape(t), text, re.I)
        if m:
            pos = m.start()
            break
    if pos < 0:
        pos = 0
    start = max(0, pos - width // 2)
    end = min(len(text), start + width)
    snip = text[start:end].replace("\n", " ")
    if start > 0:
        snip = "… " + snip
    if end < len(text):
        snip = snip + " …"
    return snip


# ---------------------------------------------------------------- fusion

def search(db, embedder, query: str, k: int = 10, mode: str = "hybrid",
           folder: str | None = None, include_dups: bool = False,
           exclude_roots: list[str] | None = None) -> list[dict]:
    cand = max(40, k * 4)
    lex_rows, vec_rows = [], []
    if mode in ("hybrid", "lexical"):
        lex_rows = _lexical(db, fts_query(query), cand, folder, exclude_roots)
    if mode in ("hybrid", "semantic"):
        vec_rows = _semantic(db, embedder, query, cand, folder, exclude_roots)

    by_id: dict[int, Hit] = {}
    for rank, r in enumerate(lex_rows, 1):
        h = Hit(r["doc_id"], r["chunk_id"], r["title"], r["folder"], r["path"], r["kind"],
                r["label"], 0.0, r["snip"], bm25_rank=rank)
        h.score += 1 / (60 + rank)
        by_id[r["chunk_id"]] = h
    terms = query_terms(query)
    for rank, r in enumerate(vec_rows, 1):
        cid = r["chunk_id"]
        if cid in by_id:
            by_id[cid].score += 1 / (60 + rank)
            by_id[cid].vec_rank = rank
        else:
            h = Hit(r["doc_id"], cid, r["title"], r["folder"], r["path"], r["kind"],
                    r["label"], 1 / (60 + rank), _text_snippet(db, cid, terms), vec_rank=rank)
            by_id[cid] = h
    hits = sorted(by_id.values(), key=lambda h: h.score, reverse=True)[:k]
    return [h.to_dict() for h in hits]


def multi_search(db, embedder, queries: list[str], k: int = 12, folder: str | None = None,
                 exclude_roots: list[str] | None = None) -> list[dict]:
    """Run several phrasings of the same need in one call; fuse all results."""
    merged: dict[int, dict] = {}
    for q in queries[:8]:
        for h in search(db, embedder, q, k=k, folder=folder, exclude_roots=exclude_roots):
            key = h["chunk_id"]
            if key in merged:
                merged[key]["score"] += h["score"]
                merged[key]["matched_queries"] += 1
            else:
                h["matched_queries"] = 1
                merged[key] = h
    out = sorted(merged.values(), key=lambda h: (-h["matched_queries"], -h["score"]))[:k]
    return out


# ---------------------------------------------------------------- census views

def coverage(db, embedder, query: str, k_docs: int = 15, chunk_pool: int = 120,
             exclude_roots: list[str] | None = None) -> dict:
    """'All the needles': every document that touches this topic, ranked, with
    exact hit census (OR-occurrences + chunks containing ALL key terms),
    best snippets, and doc summary."""
    q = fts_query(query)
    q_and = fts_and_query(query)
    hits = search(db, embedder, query, k=chunk_pool, mode="hybrid", exclude_roots=exclude_roots)
    rk, rargs = _rx("dd", exclude_roots)

    def census(expr):
        out = {}
        try:
            for r in db.execute(
                f"""SELECT c.doc_id AS d, COUNT(*) n FROM chunks_fts
                   JOIN chunks c ON c.id = chunks_fts.rowid
                   JOIN docs dd ON dd.id = c.doc_id
                   WHERE chunks_fts MATCH ? AND dd.dup_of IS NULL {rk}
                   GROUP BY c.doc_id""", (expr, *rargs)
            ).fetchall():
                out[r["d"]] = r["n"]
        except Exception:
            pass
        return out

    occ = census(q)          # term-occurrence volume (broad)
    concept = census(q_and)  # chunks containing ALL key terms (precise)

    docs: dict[int, dict] = {}
    for h in hits:
        d = docs.setdefault(h["doc_id"], {
            "doc_id": h["doc_id"], "title": h["title"], "folder": h["folder"],
            "path": h["path"], "kind": h["kind"], "fused_score": 0.0,
            "chunks_matched": 0, "snippets": [],
        })
        d["fused_score"] += h["score"]
        d["chunks_matched"] += 1
        if len(d["snippets"]) < 2:
            d["snippets"].append({"where": h["label"], "text": h["snippet"]})

    # include docs that only the lexical census saw (beyond the chunk pool top hits)
    for doc_id in set(occ) | set(concept):
        if doc_id not in docs:
            row = db.execute(
                """SELECT title, folder, path, kind FROM docs WHERE id=? AND dup_of IS NULL""",
                (doc_id,)).fetchone()
            if not row:
                continue
            snippet_row = db.execute(
                """SELECT c.label, snippet(chunks_fts,1,'»','«',' … ',12) s
                   FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid
                   WHERE chunks_fts MATCH ? AND c.doc_id=?
                   ORDER BY chunks_fts.rank LIMIT 1""", (q, doc_id)).fetchone()
            docs[doc_id] = {
                "doc_id": doc_id, "title": row["title"], "folder": row["folder"],
                "path": row["path"], "kind": row["kind"], "fused_score": 0.0,
                "chunks_matched": 0,
                "snippets": [{"where": snippet_row["label"], "text": snippet_row["s"]}] if snippet_row else [],
            }

    out = []
    for d in docs.values():
        meta = db.execute(
            "SELECT pages, year, summary FROM docs WHERE id=?", (d["doc_id"],)).fetchone()
        d["term_occurrences"] = occ.get(d["doc_id"], 0)
        d["concept_chunks"] = concept.get(d["doc_id"], 0)
        pages = meta["pages"] or 1
        d["concept_density"] = round(d["concept_chunks"] / pages, 3)
        d["pages"] = meta["pages"]
        d["year"] = meta["year"]
        d["summary"] = (meta["summary"] or "")[:280]
        fused = d.pop("fused_score")
        # rank: docs where the WHOLE concept appears in chunks, weighted by how
        # central it is to the doc (density) + fused retrieval score
        d["rank_score"] = round(fused * 8 + d["concept_chunks"] / 10 + d["concept_density"] * 6, 4)
        d.pop("chunks_matched", None)
        out.append(d)

    out.sort(key=lambda d: -d["rank_score"])
    total_docs = len(out)
    total_hits = sum(d["term_occurrences"] for d in out)
    return {
        "query": query,
        "fts_expression": q,
        "docs_matching": total_docs,
        "total_term_occurrences": total_hits,
        "documents": out[:k_docs],
        "note": "rank_score blends hybrid retrieval with concept_chunks (chunks containing ALL "
                "key terms) and concept_density (concept chunks / pages). term_occurrences is "
                "raw OR-volume and favors huge docs — trust concept chunks/density. "
                "read_doc(doc_id) to read, find_term for exact phrases.",
    }


def find_term(db, term: str, k_docs: int = 50, exclude_roots: list[str] | None = None) -> dict:
    """Exact phrase census across the whole library."""
    q = f'"{term}"'
    rk, rargs = _rx("dd", exclude_roots)
    rows = db.execute(
        f"""SELECT c.doc_id AS d, COUNT(*) n FROM chunks_fts
           JOIN chunks c ON c.id=chunks_fts.rowid JOIN docs dd ON dd.id=c.doc_id
           WHERE chunks_fts MATCH ? AND dd.dup_of IS NULL {rk}
           GROUP BY c.doc_id ORDER BY n DESC LIMIT ?""", (q, *rargs, k_docs)).fetchall()
    out = []
    for r in rows:
        meta = db.execute("SELECT title, folder, path FROM docs WHERE id=?", (r["d"],)).fetchone()
        sn = db.execute(
            """SELECT c.label, snippet(chunks_fts,1,'»','«',' … ',12) s
               FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid
               WHERE chunks_fts MATCH ? AND c.doc_id=? ORDER BY chunks_fts.rank LIMIT 2""",
            (q, r["d"])).fetchall()
        out.append({"doc_id": r["d"], "title": meta["title"], "folder": meta["folder"],
                    "occurrences": r["n"],
                    "snippets": [{"where": s["label"], "text": s["s"]} for s in sn]})
    return {"term": term, "docs_matching": len(out),
            "total_occurrences": sum(r["n"] for r in db.execute(
                f"""SELECT COUNT(*) n FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid
                   JOIN docs dd ON dd.id=c.doc_id WHERE chunks_fts MATCH ? AND dd.dup_of IS NULL {rk}""",
                (q, *rargs)).fetchall()),
            "documents": out}


# ---------------------------------------------------------------- reading

def read_doc(db, doc_id: int, label_contains: str | None = None,
             max_chars: int = 24000) -> dict:
    meta = db.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()
    if not meta:
        return {"error": f"no doc {doc_id}"}
    q = "SELECT ordinal, label, text FROM chunks WHERE doc_id=? ORDER BY id"
    params: list = [doc_id]
    if label_contains:
        q += " AND label LIKE ?"
        params.append(f"%{label_contains}%")
    rows = db.execute(q, params).fetchall()
    parts, total = [], 0
    for r in rows:
        block = f"\n--- [{r['label']}] ---\n{r['text']}"
        if total + len(block) > max_chars:
            parts.append(f"\n… truncated at {max_chars} chars — call again with label_contains "
                         f"(e.g. 'p.12' or 'slide 3') to page through. {len(rows)-len(parts)} sections remain.")
            break
        parts.append(block)
        total += len(block)
    return {
        "doc_id": doc_id, "title": meta["title"], "path": meta["path"],
        "folder": meta["folder"], "kind": meta["kind"], "pages": meta["pages"],
        "summary": meta["summary"], "note": meta["note"],
        "text": "".join(parts).strip(),
    }


def doc_card(db, doc_id: int) -> dict:
    meta = db.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()
    if not meta:
        return {"error": f"no doc {doc_id}"}
    outline = db.execute(
        "SELECT label, substr(text,1,90) head FROM chunks WHERE doc_id=? GROUP BY label ORDER BY id",
        (doc_id,)).fetchall()
    dupes = db.execute("SELECT id,title,path FROM docs WHERE dup_of=?", (doc_id,)).fetchall()
    return {
        "doc_id": doc_id, "title": meta["title"], "path": meta["path"], "root": meta["root"],
        "folder": meta["folder"], "kind": meta["kind"], "ext": meta["ext"],
        "size_mb": round((meta["size"] or 0) / 1e6, 2), "pages": meta["pages"],
        "chars": meta["chars"], "year": meta["year"], "date": meta["doc_date"],
        "summary": meta["summary"], "summary_src": meta["summary_src"], "note": meta["note"],
        "duplicate_copies": [dict(d) for d in dupes],
        "outline": [{"label": o["label"], "head": o["head"].replace("\n", " ")} for o in outline[:60]],
    }


def library(db, folder: str | None = None, limit: int = 400,
            exclude_roots: list[str] | None = None) -> dict:
    flt = "AND (folder LIKE ? OR root LIKE ?)" if folder else ""
    params = [f"%{folder}%"] * 2 if folder else []
    rk, rargs = _rx("docs", exclude_roots)
    folders = db.execute(
        f"""SELECT root, folder, COUNT(*) n, SUM(CASE WHEN chars>0 THEN 1 ELSE 0 END) with_text
            FROM docs WHERE dup_of IS NULL {flt} {rk} GROUP BY root, folder ORDER BY root, folder""",
        params + rargs).fetchall()
    docs = db.execute(
        f"""SELECT id, title, folder, kind, pages, year, substr(summary,1,140) summary
            FROM docs WHERE dup_of IS NULL {flt} {rk} ORDER BY root, folder, title LIMIT ?""",
        params + rargs + [limit]).fetchall()
    return {
        "folders": [dict(f) for f in folders],
        "docs": [dict(d) for d in docs],
    }


def related(db, embedder, doc_id: int, k: int = 8, exclude_roots: list[str] | None = None) -> list[dict]:
    """Vector centroid of this doc -> nearest chunks in other docs."""
    vecs = db.execute("SELECT embedding FROM vec_chunks WHERE chunk_id IN (SELECT id FROM chunks WHERE doc_id=?)",
                      (doc_id,)).fetchall()
    if not vecs:
        return [{"error": "doc has no embeddings (not indexed or no text)"}]
    import numpy as np
    m = np.array([np.frombuffer(v["embedding"], dtype=np.float32) for v in vecs])
    c = m.mean(axis=0)
    c = c / (np.linalg.norm(c) + 1e-9)
    pack = struct.pack(f"{len(c)}f", *c.tolist())
    rk, rargs = _rx("d", exclude_roots)
    rows = db.execute(
        f"""SELECT v.chunk_id, v.distance, c.doc_id, c.label, d.title, d.folder, d.kind,
                  d.id AS did
           FROM vec_chunks v JOIN chunks c ON c.id=v.chunk_id JOIN docs d ON d.id=c.doc_id
           WHERE v.embedding MATCH ? AND k = ? AND d.dup_of IS NULL AND d.id != ? {rk}
           ORDER BY v.distance""", (pack, max(64, k * 12), doc_id, *rargs)).fetchall()
    seen, out = set(), []
    for r in rows:
        if r["doc_id"] in seen:
            continue
        seen.add(r["doc_id"])
        snip = _text_snippet(db, r["chunk_id"], [t for t in query_terms(doc_title(db, doc_id))][:4])
        out.append({"doc_id": r["doc_id"], "title": r["title"], "folder": r["folder"],
                    "kind": r["kind"], "distance": round(r["distance"], 3),
                    "where": r["label"], "sample": snip[:300]})
        if len(out) >= k:
            break
    return out


def doc_title(db, doc_id: int) -> str:
    r = db.execute("SELECT title FROM docs WHERE id=?", (doc_id,)).fetchone()
    return r["title"] if r else ""
