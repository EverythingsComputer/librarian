"""MCP stdio server: exposes the librarian tools to any MCP client
(Hermes Agent, LMStudio, Claude Desktop, ...).

Run:  <venv>/bin/python -m librarian.mcp_server
Optional env: LIBRARIAN_DATA=/path/to/data (same as CLI).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from .config import Config
from .db import connect
from .embed import Embedder
from . import search as S

mcp = _Server(
    "librarian",
    instructions="""\
Librarian: search a private document library (PDF/PPTX/DOCX/TXT/MSG).
Workflow for thorough answers:
1. library() once to see what's there, or go straight to coverage(query) — it lists
   EVERY document touching a topic with exact occurrence counts (the 'all needles' view).
2. search()/multi_search() to pull best passages; multi_search takes several rephrasings
   of the same question in one call — use it when the user's wording may not match the
   documents' wording (acronyms, synonyms, foreign terms).
3. read_doc(doc_id) to read whole documents (page-range with 'label_contains') and
   doc_card(doc_id) for a doc's summary/outline before committing to a full read.
4. related(doc_id) to hop to neighboring documents; find_term for exact-phrase census.
Cite documents by title + doc_id + page/slide label.
Note: the user can switch individual library folders off (disable); switched-off
folders are automatically excluded from every result — if the user mentions a
folder that returns nothing, ask whether it is switched off.
""",
)

_cfg: Config | None = None
_embedder: Embedder | None = None
_embedder_lock = threading.Lock()
# mcp >= 2 dispatches sync tools onto anyio worker threads and the pool rotates,
# so a cached sqlite connection is only valid on the thread that created it
# ("SQLite objects created in a thread can only be used in that same thread").
# Give every thread its own read-only connection instead.
_tls = threading.local()


def cfg() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = Config.load()
    return _cfg


def db():
    db = getattr(_tls, "db", None)
    if db is None:
        db = _tls.db = connect(cfg().db_path, read_only=True)
    return db


def embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        with _embedder_lock:
            if _embedder is None:
                c = cfg()
                e = Embedder(c.embed_backend, c.embed_model, c.embed_base_url, c.embed_api_key)
                if c.embed_backend == "fastembed":
                    e._fast()
                else:
                    e.dims = len(e.embed_one("probe"))
                _embedder = e
    return _embedder


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _excl() -> list[str] | None:
    """Library folders the user switched off — hidden from every query.
    Re-reads config.toml each call so UI/CLI toggles apply without a restart."""
    from .config import Config
    return Config.load(cfg().data_dir).disabled_roots or None


import threading

_build_gate = threading.Lock()
_building = {"on": False}

INSTRUCTIONS_NOTE = "see server instructions"


@mcp.tool()
def status() -> str:
    """Index stats: doc/chunk counts, folders, last build time."""
    from .ingest import status as _status
    return _j(_status(cfg()))


@mcp.tool()
def library(folder: str | None = None, limit: int = 300) -> str:
    """Map of the library: folders with doc counts + doc cards (id/title/summary).
    Call once per session before broad questions. folder: filter by name fragment."""
    return _j(S.library(db(), folder=folder, limit=limit, exclude_roots=_excl()))


@mcp.tool()
def search(query: str, k: int = 10, mode: str = "hybrid", folder: str | None = None) -> str:
    """Top passages for a query. mode: hybrid (default), lexical (exact words/BM25),
    semantic (meaning). folder: restrict to name fragment. Returns snippets with
    doc_id + page/slide label — cite those. Any wording is safe, including hyphenated
    model numbers (LT2-F, MMP-15). To read a hit's page: read_doc(doc_id,
    label_contains=<the hit's label>)."""
    return _j(S.search(db(), embedder(), query, k=k, mode=mode, folder=folder, exclude_roots=_excl()))


@mcp.tool()
def multi_search(queries: list[str], k: int = 12, folder: str | None = None) -> str:
    """Several phrasings of the SAME need in one call, results fused (chunks found by
    more phrasings rank first). Best tool when wording may differ from the documents:
    e.g. ['quarterly budget totals', 'Q3 spending limits', 'budget cap per team']."""
    return _j(S.multi_search(db(), embedder(), queries, k=k, folder=folder, exclude_roots=_excl()))


@mcp.tool()
def coverage(query: str, k_docs: int = 15) -> str:
    """THE 'ALL NEEDLES' TOOL. For a topic, returns EVERY document that touches it:
    exact term-occurrence counts across the whole library, best snippets per doc, and
    each doc's summary — not just the top passage. Use before answering questions like
    'what do my documents say about X'. Broad recall: partial word matches inflate
    counts — judge docs by concept_chunks/concept_density, confirm with find_term,
    then read_doc the ones that matter."""
    return _j(S.coverage(db(), embedder(), query, k_docs=k_docs, exclude_roots=_excl()))


@mcp.tool()
def find_term(term: str) -> str:
    """Exact-phrase census: every document containing the phrase and occurrence counts.
    Good for standard numbers, codes, names, e.g. 'ISO 9001', 'error E-4012', 'SN-88213'."""
    return _j(S.find_term(db(), term, exclude_roots=_excl()))


@mcp.tool()
def doc_card(doc_id: int) -> str:
    """One document: metadata, summary, note (duplicates/video link), section outline."""
    return _j(S.doc_card(db(), doc_id))


@mcp.tool()
def read_doc(doc_id: int, label_contains: str | None = None, max_chars: int = 24000) -> str:
    """Read a document's text with [p.N]/[slide N] markers. Truncates at max_chars;
    when truncated, the output's last line tells you the NEXT label to request —
    call again with label_contains set to THAT (never re-read from the start).
    label_contains selects pages whose label matches ('p.24' matches 'p.24 (cont 1)');
    if nothing matches you get an error listing real labels — never put product names
    or body text in it, that is search()'s job."""
    return _j(S.read_doc(db(), doc_id, label_contains=label_contains, max_chars=max_chars))


@mcp.tool()
def related(doc_id: int, k: int = 8) -> str:
    """Documents nearest to this one in meaning — good for following a thread across files."""
    return _j(S.related(db(), embedder(), doc_id, k=k, exclude_roots=_excl()))


@mcp.tool()
def update_index() -> str:
    """Scan library roots and index new/changed files (incremental, safe to call).
    Use when the user says they just added or changed files. Files that failed
    earlier because an extraction tool was missing are retried automatically."""
    # in-process (v0.2.5): the old child launch `sys.executable -m librarian.cli`
    # never worked inside the AppImage — the executable there is a frozen
    # binary with no -m flag, so this tool always returned "update failed".
    with _build_gate:
        if _building["on"]:
            return "a sync is already running; try again in a minute"
        _building["on"] = True
    try:
        from .ingest import build_or_update
        rep: dict = {}
        build_or_update(cfg(), report=rep)
        return ("added=%s changed=%s removed=%s docs=%s chunks=%s seconds=%s textless=%s"
                % tuple(rep.get(k) for k in ("added", "changed", "removed",
                                             "docs", "chunks", "seconds", "textless")))
    except Exception as e:
        return f"update failed: {type(e).__name__}: {e}"
    finally:
        with _build_gate:
            _building["on"] = False


if __name__ == "__main__":
    mcp.run(transport="stdio")
