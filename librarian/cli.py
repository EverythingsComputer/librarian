"""librarian CLI.

librarian init [--data DIR]        create data dir + config template
librarian build [--llm] [--force]  full incremental index build
librarian update [--llm]           incremental refresh (same as build)
librarian status                   index stats as JSON
librarian ls [--folder F]          list folders/docs
librarian card DOC_ID              one doc: metadata, summary, outline
librarian read DOC_ID [--at LABEL] [--max N]
librarian search "query" [-k 10] [--mode hybrid|lexical|semantic] [--folder F]
librarian multi "q1" "q2" "q3" [-k 12]
librarian coverage "query" [-k 15]
librarian find "exact phrase"
librarian related DOC_ID [-k 8]
librarian cards-refresh [--llm] [--missing-only]
librarian sync            incremental refresh, human summary (agents: --json)
librarian folders         list library folders (+ on/off state)
librarian add-folder PATH [--name N] [--sync]
librarian remove-folder NAME
librarian enable NAME | librarian disable NAME   (instant; index is kept)
librarian ui [--port 8765] [--no-browser]        local maintenance web page
librarian mcp-serve [--transport stdio|http] [--host H] [--port P] [--token T]
                    serve the MCP tools; http mode is for remote/Tailscale clients
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Config, DEFAULT_DATA_DIR, ensure_data_dir


def _cfg(args) -> Config:
    return Config.load(Path(args.data) if getattr(args, "data", None) else None)


def _embedder(cfg: Config):
    from .embed import Embedder
    e = Embedder(cfg.embed_backend, cfg.embed_model, cfg.embed_base_url, cfg.embed_api_key)
    if cfg.embed_backend == "fastembed":
        e._fast()
    else:
        e.dims = len(e.embed_one("probe"))
    return e


def _j(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _excl(cfg: Config, args=None) -> list[str] | None:
    """Roots to hide from query results (switched-off folders), unless --include-off."""
    if getattr(args, "include_off", False):
        return None
    return cfg.disabled_roots or None


def cmd_sync(args):
    cfg = _cfg(args)
    from .ingest import build_or_update
    report: dict = {}
    build_or_update(cfg, report=report, retry_empty=getattr(args, "retry_empty", False))
    if args.json:
        _j(report)
        return
    from .ui import human_summary
    print(human_summary(cfg, report))


def cmd_init(args):
    ddir = Path(args.data or DEFAULT_DATA_DIR).expanduser()
    ensure_data_dir(ddir)
    cfg_path = ddir / "config.toml"
    if cfg_path.exists() and not args.force:
        print(f"already exists: {cfg_path} (use --force to overwrite)")
        return
    cfg_path.write_text(Config.template())
    print(f"wrote {cfg_path}\nEdit it (set your library paths), then run: librarian build")


def cmd_folders(args):
    from .db import connect
    from .ui import api_state
    cfg = _cfg(args)
    if args.json:
        _j(api_state(cfg))
        return
    st = api_state(cfg)
    print("Library folders (on = included in search):")
    for l in st["libraries"]:
        mark = "ON " if l["enabled"] else "OFF"
        extra = "  [folder missing!]" if l.get("missing") else ""
        print(f"  [{mark}] {l['name']:<28} {l['docs']:>5} docs   {l['path']}{extra}")
    if st.get("indexed"):
        print(f"\n{st['searchable']} of {st['total_docs']} documents searchable · last checked {st['last_sync']}")


def cmd_add_folder(args):
    from .config import add_root
    cfg = _cfg(args)
    try:
        root = add_root(cfg, args.path, args.name)
    except ValueError as e:
        sys.exit(f"error: {e}")
    print(f"added library '{root.name}' -> {root.path}")
    if args.sync:
        cmd_sync(argparse.Namespace(data=cfg.data_dir, json=False))
    else:
        print("run: librarian sync   (or use the web UI)")


def cmd_remove_folder(args):
    from .config import remove_root
    cfg = _cfg(args)
    try:
        root = remove_root(cfg, args.name)
    except ValueError as e:
        sys.exit(f"error: {e}")
    print(f"removed library '{root.name}' from config — your files were NOT deleted.")
    if args.sync:
        cmd_sync(argparse.Namespace(data=cfg.data_dir, json=False))
    else:
        print("run 'librarian sync' to drop its documents from the index.")


def cmd_toggle(args, enable: bool):
    from .config import set_root_enabled
    cfg = _cfg(args)
    try:
        root = set_root_enabled(cfg, args.name, enable)
    except ValueError as e:
        sys.exit(f"error: {e}")
    state = "ON — documents are searchable again" if enable else \
        "OFF — documents are hidden from search (still stored, instantly restorable)"
    print(f"{root.name}: {state}")


def cmd_ui(args):
    from .ui import DEFAULT_PORT, serve
    cfg = _cfg(args)
    # explicit = the user typed --port themselves (even if 8765); a bare
    # launch of the default port is allowed to slide if it's busy
    serve(cfg.data_dir, port=args.port or DEFAULT_PORT,
          open_browser=not args.no_browser, explicit=args.port is not None)


def cmd_mcp_serve(args):
    cfg = _cfg(args)
    if args.transport == "stdio":
        from .mcp_server import mcp
        mcp.run(transport="stdio")
    else:
        from .mcp_http import serve as http_serve
        http_serve(cfg.data_dir, host=args.host, port=args.port or cfg.mcp_port,
                   token=args.token or cfg.mcp_token or None, stateless=args.stateless)


def cmd_build(args):
    cfg = _cfg(args)
    from .ingest import build_or_update
    build_or_update(cfg, force=args.force, use_llm_summaries=args.llm, limit=args.limit,
                    retry_empty=getattr(args, "retry_empty", False))


def cmd_status(args):
    from .ingest import status
    _j(status(_cfg(args)))


def cmd_ls(args):
    from .db import connect
    from .search import library
    cfg = _cfg(args)
    db = connect(cfg.db_path, read_only=True)
    _j(library(db, folder=args.folder, limit=args.limit, exclude_roots=_excl(cfg, args)))


def cmd_card(args):
    from .db import connect
    from .search import doc_card
    db = connect(_cfg(args).db_path, read_only=True)
    _j(doc_card(db, args.doc_id))


def cmd_read(args):
    from .db import connect
    from .search import read_doc
    db = connect(_cfg(args).db_path, read_only=True)
    r = read_doc(db, args.doc_id, label_contains=args.at, max_chars=args.max)
    if args.meta_only:
        r.pop("text", None)
        _j(r)
    else:
        print(f"# {r.get('title')}  [{r.get('path')}]\n")
        print(r.get("text", r.get("error", "")))


def cmd_search(args):
    cfg = _cfg(args)
    from .db import connect
    from .search import search
    db = connect(cfg.db_path, read_only=True)
    _j(search(db, _embedder(cfg), args.query, k=args.k, mode=args.mode, folder=args.folder,
              exclude_roots=_excl(cfg, args)))


def cmd_multi(args):
    cfg = _cfg(args)
    from .db import connect
    from .search import multi_search
    db = connect(cfg.db_path, read_only=True)
    _j(multi_search(db, _embedder(cfg), args.queries, k=args.k, folder=args.folder,
                    exclude_roots=_excl(cfg, args)))


def cmd_coverage(args):
    cfg = _cfg(args)
    from .db import connect
    from .search import coverage
    db = connect(cfg.db_path, read_only=True)
    _j(coverage(db, _embedder(cfg), args.query, k_docs=args.k, exclude_roots=_excl(cfg, args)))


def cmd_find(args):
    from .db import connect
    from .search import find_term
    cfg = _cfg(args)
    db = connect(cfg.db_path, read_only=True)
    _j(find_term(db, args.term, exclude_roots=_excl(cfg, args)))


def cmd_related(args):
    cfg = _cfg(args)
    from .db import connect
    from .search import related
    db = connect(cfg.db_path, read_only=True)
    _j(related(db, _embedder(cfg), args.doc_id, k=args.k, exclude_roots=_excl(cfg, args)))


def cmd_cards_refresh(args):
    cfg = _cfg(args)
    from .db import connect
    from .llm import ChatLLM, extractive_summary
    db = connect(cfg.db_path)
    llm = ChatLLM(cfg.llm_base_url, cfg.llm_model, cfg.llm_api_key) if (args.llm and cfg.llm_base_url) else None
    if not llm:
        print("no [llm] configured in config.toml — doing extractive refresh only")
    where = "WHERE summary IS NULL OR summary=''" if args.missing_only else ""
    rows = db.execute(f"SELECT id,title FROM docs WHERE dup_of IS NULL AND chars>0 {where}").fetchall()
    n = 0
    for r in rows:
        text = db.execute(
            "SELECT GROUP_CONCAT(text, char(10)) t FROM (SELECT text FROM chunks WHERE doc_id=? ORDER BY id LIMIT 8)",
            (r["id"],)).fetchone()["t"] or ""
        summ = None
        if llm:
            try:
                summ = llm.summarize_doc(r["title"], text)
            except Exception as e:
                print(f"llm fail {r['id']}: {e}")
        if not summ:
            summ = extractive_summary(text)
            src = "extractive"
            if not args.all and not args.missing_only and r is None:
                pass
        else:
            src = "llm"
        db.execute("UPDATE docs SET summary=?, summary_src=? WHERE id=?", (summ, src, r["id"]))
        n += 1
        if n % 20 == 0:
            db.commit()
            print(f"  {n}/{len(rows)}")
    db.commit()
    print(f"refreshed {n} summaries")


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", help="data dir (default ~/librarian or $LIBRARIAN_DATA)")
    common.add_argument("--include-off", action="store_true",
                        help="also return documents from library folders that are switched off")
    ap = argparse.ArgumentParser(prog="librarian", description="Search your own document folders; connects AI assistants to them")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", parents=[common]); p.add_argument("--force", action="store_true"); p.set_defaults(fn=cmd_init)
    p = sub.add_parser("build", parents=[common])
    p.add_argument("--llm", action="store_true", help="generate LLM doc summaries")
    p.add_argument("--force", action="store_true", help="re-extract everything")
    p.add_argument("--retry-empty", action="store_true",
                   help="also re-extract every empty PDF, including true scans")
    p.add_argument("--limit", type=int, default=0, help="debug: cap files")
    p.set_defaults(fn=cmd_build)
    p = sub.add_parser("update", parents=[common])
    p.add_argument("--llm", action="store_true")
    p.add_argument("--retry-empty", action="store_true")
    p.set_defaults(fn=lambda a: cmd_build(argparse.Namespace(
        llm=a.llm, force=False, limit=0, data=a.data, retry_empty=a.retry_empty)))
    p = sub.add_parser("status", parents=[common]); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("ls", parents=[common]); p.add_argument("--folder"); p.add_argument("--limit", type=int, default=400); p.set_defaults(fn=cmd_ls)
    p = sub.add_parser("card", parents=[common]); p.add_argument("doc_id", type=int); p.set_defaults(fn=cmd_card)
    p = sub.add_parser("read", parents=[common])
    p.add_argument("doc_id", type=int)
    p.add_argument("--at", help="filter sections by label, e.g. 'p.3' or 'slide 5'")
    p.add_argument("--max", type=int, default=24000)
    p.add_argument("--meta-only", action="store_true")
    p.set_defaults(fn=cmd_read)
    p = sub.add_parser("search", parents=[common])
    p.add_argument("query")
    p.add_argument("-k", type=int, default=10)
    p.add_argument("--mode", choices=["hybrid", "lexical", "semantic"], default="hybrid")
    p.add_argument("--folder")
    p.set_defaults(fn=cmd_search)
    p = sub.add_parser("multi", parents=[common])
    p.add_argument("queries", nargs="+")
    p.add_argument("-k", type=int, default=12)
    p.add_argument("--folder")
    p.set_defaults(fn=cmd_multi)
    p = sub.add_parser("coverage", parents=[common])
    p.add_argument("query")
    p.add_argument("-k", type=int, default=15)
    p.set_defaults(fn=cmd_coverage)
    p = sub.add_parser("find", parents=[common])
    p.add_argument("term")
    p.set_defaults(fn=cmd_find)
    p = sub.add_parser("related", parents=[common])
    p.add_argument("doc_id", type=int)
    p.add_argument("-k", type=int, default=8)
    p.set_defaults(fn=cmd_related)
    p = sub.add_parser("cards-refresh", parents=[common])
    p.add_argument("--llm", action="store_true")
    p.add_argument("--all", action="store_true")
    p.add_argument("--missing-only", action="store_true")
    p.set_defaults(fn=cmd_cards_refresh)

    # --- maintenance / library-folder management
    p = sub.add_parser("sync", parents=[common],
                       help="check for new/changed/removed files and update the index")
    p.add_argument("--json", action="store_true", help="machine-readable report instead of prose")
    p.add_argument("--retry-empty", action="store_true",
                   help="also re-extract every empty PDF, including true scans")
    p.set_defaults(fn=cmd_sync)
    p = sub.add_parser("folders", parents=[common], help="list library folders and on/off state")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_folders)
    p = sub.add_parser("add-folder", parents=[common])
    p.add_argument("path")
    p.add_argument("--name")
    p.add_argument("--sync", action="store_true", help="index the new folder right away")
    p.set_defaults(fn=cmd_add_folder)
    p = sub.add_parser("remove-folder", parents=[common])
    p.add_argument("name")
    p.add_argument("--sync", action="store_true")
    p.set_defaults(fn=cmd_remove_folder)
    p = sub.add_parser("enable", parents=[common], help="include a library folder in search")
    p.add_argument("name")
    p.set_defaults(fn=lambda a: cmd_toggle(a, True))
    p = sub.add_parser("disable", parents=[common], help="hide a library folder from search (keeps index)")
    p.add_argument("name")
    p.set_defaults(fn=lambda a: cmd_toggle(a, False))
    p = sub.add_parser("ui", parents=[common], help="local web page for maintenance + search")
    p.add_argument("--port", type=int, default=None,
                   help="default: 8765; if busy the default launch uses the next free port")
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(fn=cmd_ui)
    p = sub.add_parser("mcp-serve", parents=[common],
                       help="serve MCP tools (stdio for local agents; http for remote clients)")
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=0, help="default: [server] mcp_port (8770)")
    p.add_argument("--token", default="", help="default: [server] mcp_token (required for non-loopback binds)")
    p.add_argument("--stateless", action="store_true",
                   help="stateless HTTP mode (friendlier behind proxies, no session resume)")
    p.set_defaults(fn=cmd_mcp_serve)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
