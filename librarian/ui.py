"""librarian ui — a tiny local web page for non-terminal maintenance.

Stdlib only (http.server), binds 127.0.0.1 by default.
Features: library status, search box, one-click sync (check for new files),
add/remove library folders, and per-folder on/off switches (instant, the
index is kept so re-enabling needs no re-index).
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .config import (Config, add_root, find_root, remove_root,
                     set_root_enabled)

_sync_lock = threading.Lock()   # superseded by ingest.SYNC_LOCK (shared with MCP)
_embedder = None
_embedder_lock = threading.Lock()

DEFAULT_PORT = 8765


def _get_embedder(cfg: Config):
    global _embedder
    with _embedder_lock:
        if _embedder is None:
            from .embed import Embedder
            e = Embedder(cfg.embed_backend, cfg.embed_model, cfg.embed_base_url, cfg.embed_api_key)
            if cfg.embed_backend == "fastembed":
                e._fast()
            else:
                e.dims = len(e.embed_one("probe"))
            _embedder = e
    return _embedder


def human_summary(cfg: Config, report: dict) -> str:
    from .db import connect
    db = connect(cfg.db_path, read_only=True)
    excl = cfg.disabled_roots
    hidden = 0
    if excl:
        ph = ",".join("?" * len(excl))
        hidden = db.execute(
            f"SELECT COUNT(*) c FROM docs WHERE root IN ({ph}) AND dup_of IS NULL", excl).fetchone()["c"]
    # "searchable" must mean it can actually be found: count only docs with
    # text behind them (v0.2.5 — stub-heavy indexes used to advertise every
    # failed file as searchable, which is how Optiplex shipped "303 of 303")
    cond = "d.dup_of IS NULL AND EXISTS(SELECT 1 FROM chunks c WHERE c.doc_id=d.id)"
    args: list = []
    if excl:
        cond += f" AND d.root NOT IN ({','.join('?' * len(excl))})"
        args = list(excl)
    searchable = db.execute(f"SELECT COUNT(*) c FROM docs d WHERE {cond}", args).fetchone()["c"]
    textless = db.execute(
        "SELECT COUNT(*) c FROM docs WHERE kind='empty' AND dup_of IS NULL").fetchone()["c"]
    db.close()
    total = report.get("docs", 0)
    parts = []
    for key, word in (("added", "new"), ("changed", "changed"), ("removed", "removed")):
        n = report.get(key, 0)
        if n:
            parts.append(f"{n} {word}")
    what = ", ".join(parts) if parts else "nothing new"
    off = f" — {len(off_names(cfg))} folder(s) switched off, {hidden} docs hidden" if excl else ""
    warn = ""
    from .extract import tool_path as _tp
    if report.get("ocr") and not _tp("tesseract"):
        warn += (" OCR was requested but tesseract is unavailable here —"
                 " nothing was OCR'd.")
    if report.get("prune_blocked"):
        warn += (f" PRUNE BLOCKED: {report['prune_blocked']} files appear to have"
                 " vanished at once — looks like an offline mount, nothing was"
                 " removed. Check the folder is online; if the files really are"
                 " gone, re-run with LIBRARIAN_ALLOW_MASS_PRUNE=1.")
    if report.get("cancelled"):
        warn += " (sync was cancelled mid-run — nothing pruned; run again to continue)"
    if textless and searchable == 0:
        warn = (f" WARNING: none of the {total} files produced any text — the PDF"
                f" extractor cannot run here (install poppler-utils, or use the"
                f" AppImage v0.2.5+ which bundles and uses its own).")
    elif textless:
        warn = f" Note: {textless} file(s) still have no searchable text (scanned PDFs?)."
    return (f"Checked: {what}. Library: {total} documents, {searchable} searchable{off}. "
            f"Took {report.get('seconds', 0)}s.{warn}")


def off_names(cfg: Config) -> list[str]:
    return [r.name for r in cfg.roots if not r.enabled]


# ------------------------------------------------------------------ API logic

def _sh(cmd: list[str], timeout: float = 2.0) -> str:
    import subprocess
    # host helpers (tailscale) must not inherit the bundle's loader env —
    # same leak as the v0.2.1 browser-open bug (v0.2.5)
    env = {k: v for k, v in os.environ.items()
           if k not in ("LD_LIBRARY_PATH", "LD_PRELOAD")}
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=env).stdout.strip()
    except Exception:
        return ""


def _ip_addrs_v4() -> list[tuple[str, str]]:
    """[(interface, ipv4)] from `ip -4 -o addr` (no deps beyond iproute2)."""
    import re as _re
    out = []
    for line in _sh(["ip", "-4", "-o", "addr", "show"]).splitlines():
        m = _re.match(r"\d+:\s+(\S+)\s+inet\s+([\d.]+)", line)
        if m and not m.group(2).startswith("127."):
            out.append((m.group(1), m.group(2)))
    return out


def api_token(cfg: Config, action: str, value: str = "") -> dict:
    """Token management for the UI. generate = new random secret;
    set = explicit value. The running mcp-serve keeps its old token until
    restarted (it loads the config at startup)."""
    import secrets
    if action == "generate":
        new = secrets.token_urlsafe(24)
    elif action == "set" and value.strip():
        new = value.strip()
    else:
        raise ValueError("action must be 'generate' or 'set' with a value")
    from .config import set_server_value
    set_server_value(cfg, "mcp_token", new)
    return {"ok": True, "token": new,
            "note": "restart 'librarian mcp-serve' to apply; update your clients' URL/header too"}


def api_connection(cfg: Config) -> dict:
    """Everything needed to point a remote MCP client at this machine."""
    import platform
    import socket
    addrs = _ip_addrs_v4()
    ts_ip = ""
    ts_state = _sh(["tailscale", "status", "--self"])
    for line in ts_state.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0][0].isdigit() and parts[0].count(".") == 3:
            ts_ip = parts[0]
            break
    if not ts_ip:  # no tailscale CLI — fall back to an address on a ts*/tun* iface
        for iface, ip in addrs:
            if iface.startswith(("tailscale", "ts", "tun")):
                ts_ip = ip
                break
    lan = [ip for _, ip in addrs if ip != ts_ip]
    port = cfg.mcp_port
    url_base = f"http://{ts_ip or (lan[0] if lan else '127.0.0.1')}"
    # is the MCP HTTP server listening on this box? (it may be bound to loopback,
    # the Tailscale address, or all interfaces — probe whichever we could reach)
    import socket as _s
    running = False
    for target in dict.fromkeys(["127.0.0.1", ts_ip] if ts_ip else ["127.0.0.1"]):
        s = _s.socket()
        s.settimeout(0.4)
        if s.connect_ex((target, port)) == 0:
            running = True
        s.close()
        if running:
            break
    tok = cfg.mcp_token
    return {
        "hostname": socket.gethostname(),
        "os": platform.system(),
        "tailscale_ip": ts_ip or None,
        "lan_ips": lan,
        "mcp_port": port,
        "mcp_running": running,
        "token_set": bool(tok),
        "token": tok,               # UI is loopback-only; the local user owns this secret
        "mcp_path": "/mcp",
        "connect_url": f"{url_base}:{port}/mcp" + (f"?token={tok}" if tok else ""),
        "serve_cmd": (f"{_serve_invocation()} mcp-serve --transport http "
                      f"--host {ts_ip or '0.0.0.0'} --port {port}"
                      + ("" if tok else " --token 'pick-a-long-random-secret'")),
    }


def _serve_invocation() -> str:
    """The invocation that ACTUALLY starts mcp-serve in a fresh shell on this
    machine, whatever shape librarian was launched in (v0.2.4). A literal
    `librarian` breaks for AppImage users (nothing installs on PATH) and for
    venv installs whose bin dir isn't exported. Resolution order:
      1. a `librarian` console script already on PATH,
      2. one sitting next to our own interpreter (venv: .venv/bin/librarian),
      3. no console script at all (AppImage / source run): module form,
         re-exporting the PYTHONPATH (package dir) and LD_LIBRARY_PATH
         (bundled loader libs) this live process runs with, so the pasted
         command reproduces our own environment in the user's shell.
    Everything is shlex.quoted; paths inside an AppImage mount stay valid
    because the UI process (which the user copies from) holds the mount open.
    """
    import os
    import shlex
    import shutil
    import sys
    from pathlib import Path as _P
    w = shutil.which("librarian")
    if w:
        return shlex.quote(w)
    try:
        side = _P(sys.executable).resolve().parent / "librarian"
        if side.exists() and os.access(side, os.X_OK):
            return shlex.quote(str(side))
    except OSError:
        pass
    from . import __file__ as _init
    pkg_root = str(_P(_init).resolve().parent.parent)  # dir containing the package
    env = f"PYTHONPATH={shlex.quote(pkg_root)} "
    ldlp = os.environ.get("LD_LIBRARY_PATH", "")
    if ldlp:
        env += f"LD_LIBRARY_PATH={shlex.quote(ldlp)} "
    return f"{env}{shlex.quote(sys.executable)} -m librarian.cli"


def api_state(cfg: Config) -> dict:
    from .db import connect
    from .ingest import progress_snapshot
    out = {"libraries": [], "indexed": cfg.db_path.exists(),
           "sync": progress_snapshot()}
    if not out["indexed"]:
        # no db yet — but a FIRST build may be running right now; progress
        # matters most then, so sync is already in `out`
        out["libraries"] = [{"name": r.name, "path": str(r.path), "enabled": r.enabled,
                             "docs": 0} for r in cfg.roots]
        return out
    db = connect(cfg.db_path, read_only=True)
    row = db.execute("SELECT value v FROM meta WHERE key='last_build'").fetchone()
    counts = {r["root"]: r for r in db.execute(
        "SELECT root, COUNT(*) n, SUM(CASE WHEN dup_of IS NOT NULL THEN 1 ELSE 0 END) d "
        "FROM docs GROUP BY root")}
    total = db.execute("SELECT COUNT(*) n FROM docs").fetchone()["n"]
    dups = db.execute("SELECT COUNT(*) n FROM docs WHERE dup_of IS NOT NULL").fetchone()["n"]
    searchable_roots = [r.name for r in cfg.roots if r.enabled]
    if searchable_roots:
        ph = ",".join("?" * len(searchable_roots))
        # searchable = has real text behind it (v0.2.5); empty stubs are counted
        # separately and shown as a warning instead of being advertised
        searchable = db.execute(
            f"SELECT COUNT(*) n FROM docs d WHERE d.dup_of IS NULL AND d.root IN ({ph})"
            " AND EXISTS(SELECT 1 FROM chunks c WHERE c.doc_id=d.id)",
            searchable_roots).fetchone()["n"]
    else:
        searchable = 0
    textless = db.execute(
        "SELECT COUNT(*) n FROM docs WHERE kind='empty' AND dup_of IS NULL").fetchone()["n"]
    known = {r.name for r in cfg.roots}
    for r in cfg.roots:
        c = counts.get(r.name, {"n": 0, "d": 0})
        missing = not r.path.is_dir()
        out["libraries"].append({"name": r.name, "path": str(r.path), "enabled": r.enabled,
                                 "docs": (c["n"] or 0) - (c["d"] or 0), "missing": missing})
    for root_name, c in counts.items():  # indexed but no longer configured
        if root_name and root_name not in known:
            out["libraries"].append({"name": root_name, "path": "(removed from config — run sync to drop)",
                                     "enabled": False, "docs": (c["n"] or 0) - (c["d"] or 0),
                                     "missing": True})
    db.close()
    from .extract import tool_path, tessdata_dir
    from .ingest import progress_snapshot
    out.update(last_sync=time.strftime("%Y-%m-%d %H:%M", time.localtime(float(row["v"] or 0))) if row else "",
               total_docs=total, duplicates=dups, searchable=searchable,
               textless=textless,
               tools={n: bool(tool_path(n)) for n in ("pdftotext", "pdfinfo")},
               ocr={"enabled": bool(cfg.ocr),
                    "available": bool(tool_path("tesseract") and tool_path("pdftoppm"))},
               sync=progress_snapshot(),
               db_mb=round(cfg.db_path.stat().st_size / 1e6, 1))
    return out


def api_search(cfg: Config, q: str, k: int = 10) -> list[dict]:
    from .db import connect
    from .search import search
    db = connect(cfg.db_path, read_only=True)
    hits = search(db, _get_embedder(cfg), q, k=k, exclude_roots=cfg.disabled_roots)
    db.close()
    return [{"doc_id": h["doc_id"], "title": h["title"],
             "folder": h["folder"], "label": h["label"], "snippet": h["snippet"],
             "kind": h["kind"], "score": round(h["score"], 4)} for h in hits]


def api_sync(cfg: Config, retry_empty: bool = False, ocr=None) -> dict:
    if ocr is not None and bool(ocr) != cfg.ocr:
        from .config import set_ocr
        set_ocr(cfg, bool(ocr))    # persisted: survives refresh, visible to status()
    from .ingest import SYNC_LOCK, build_or_update
    if not SYNC_LOCK.acquire(blocking=False):
        return {"busy": True, "message": "A sync is already running."}
    try:
        report: dict = {}
        build_or_update(cfg, report=report, retry_empty=retry_empty)
        return {"busy": False, "report": report, "summary": human_summary(cfg, report)}
    finally:
        SYNC_LOCK.release()


def api_add(cfg: Config, path: str, name: str | None) -> dict:
    root = add_root(cfg, path, name)
    return {"added": root.name, **api_sync(Config.load(cfg.data_dir))}


def api_toggle(cfg: Config, name: str, enabled: bool) -> dict:
    root = set_root_enabled(cfg, name, enabled)
    return {"name": root.name, "enabled": root.enabled}


def api_remove(cfg: Config, name: str) -> dict:
    root = remove_root(cfg, name)
    res = api_sync(Config.load(cfg.data_dir))
    return {"removed": root.name, "note": "only removed from the index — your files were NOT deleted",
            **res}


def api_profiles(cfg: Config, body: dict | None = None) -> dict:
    """Shared-access profile manager (v0.3.0). GET (body=None) returns the
    list; POST performs one action. Token plaintext is returned EXACTLY ONCE
    (add/reset) — config stores only its hash. A running mcp-serve picks
    every change up within one request (mtime-reloaded resolver), no restart."""
    from .config import (add_profile, delete_profile, rename_profile,
                         reset_profile_token, set_profile_enabled,
                         set_profile_roots)
    folders = [r.name for r in cfg.roots]
    if body is None:
        return {"profiles": [{"name": p.name, "roots": p.roots, "enabled": p.enabled}
                             for p in cfg.profiles],
                "folders": folders}
    act = body.get("action", "")
    name = str(body.get("name", "")).strip()
    roots = [str(x) for x in (body.get("roots") or [])]
    out: dict = {"ok": True}
    if act == "add":
        prof, plain = add_profile(cfg, name, roots)
        out.update(name=prof.name, token=plain)
    elif act == "rename":
        prof = rename_profile(cfg, name, str(body.get("new", "")))
        out["name"] = prof.name
    elif act == "delete":
        prof = delete_profile(cfg, name)
        out["name"] = prof.name
    elif act == "reset":
        out["token"] = reset_profile_token(cfg, name)
        out["name"] = name
    elif act == "enable":
        prof = set_profile_enabled(cfg, name, bool(body.get("enabled")))
        out.update(name=prof.name, enabled=prof.enabled)
    elif act == "roots":
        prof = set_profile_roots(cfg, name, roots)
        out.update(name=prof.name, roots=prof.roots)
    else:
        raise ValueError("unknown profiles action")
    return out


# ------------------------------------------------------------------ HTTP

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Librarian</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0d1015;--card:#151a21;--txt:#e9edf2;--mut:#93a0af;--acc:#f0a531;--ok:#43c975;--no:#e5645f;--line:#252d38;--rad:12px}
html{color-scheme:dark}
*{box-sizing:border-box}
::-webkit-scrollbar{width:11px;height:11px}
::-webkit-scrollbar-track{background:#0a0d11}
::-webkit-scrollbar-thumb{background:#2c3642;border-radius:6px;border:2px solid #0a0d11}
::-webkit-scrollbar-thumb:hover{background:#3a4757}
input[type=checkbox]{accent-color:var(--acc);width:15px;height:15px}
body{margin:0;font:15px/1.55 -apple-system,"Segoe UI",system-ui,sans-serif;
 background:radial-gradient(1200px 520px at 72% -10%,#1c2532 0%,var(--bg) 62%) fixed,var(--bg);color:var(--txt)}
main{max-width:880px;margin:0 auto;padding:30px 18px 110px}
h1{font-size:24px;font-weight:750;margin:0 0 2px;letter-spacing:-.01em}
h1 b{background:linear-gradient(90deg,var(--acc),#ffd08a);-webkit-background-clip:text;background-clip:text;color:transparent}
.stats{color:var(--mut);font-size:13px;margin-bottom:22px}
section{background:linear-gradient(180deg,var(--card),#141920);border:1px solid var(--line);border-radius:var(--rad);
 padding:18px;margin-bottom:16px;box-shadow:0 8px 24px #00000038}
h2{font-size:12.5px;font-weight:700;text-transform:uppercase;letter-spacing:.09em;color:var(--mut);margin:0 0 14px}
h2 em{color:var(--acc);font-style:normal;font-weight:600;text-transform:none;letter-spacing:0;margin-left:8px}
input[type=text]{width:100%;padding:10px 12px;border-radius:9px;border:1px solid var(--line);
 background:#0b0e12;color:var(--txt);font-size:15px;outline:none;transition:border-color .15s}
input[type=text]:focus{border-color:#3a4a5e}
button{background:linear-gradient(180deg,#f7b23f,#df9925);color:#20160a;border:0;border-radius:9px;
 padding:9px 16px;font-weight:700;cursor:pointer;font-size:13.5px;transition:filter .15s}
button:hover{filter:brightness(1.08)}
button.sec{background:#242c38;color:var(--txt);font-weight:600}
button.tiny{padding:4px 10px;font-size:12px;border-radius:7px}
button.danger{background:none;color:var(--no);border:1px solid #43302f;font-weight:600}
button:disabled{opacity:.5;cursor:wait}
.row{display:flex;gap:8px;margin-top:10px}.row input{flex:1}
.lbl{color:var(--mut);font-size:12px}
.lib{display:flex;align-items:center;gap:12px;padding:11px 2px;border-bottom:1px solid var(--line)}
.lib:last-child{border-bottom:0}
.lib .nm{font-weight:600}.lib .pth{color:var(--mut);font-size:12px;word-break:break-all}
.lib .cnt{margin-left:auto;color:var(--mut);font-size:13px;white-space:nowrap}
.sw{position:relative;width:40px;height:22px;flex:none}
.sw input{opacity:0;width:0;height:0}
.sl{position:absolute;inset:0;background:#39424f;border-radius:22px;transition:.15s;cursor:pointer}
.sl:before{content:"";position:absolute;width:16px;height:16px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.15s}
input:checked+.sl{background:var(--ok)}input:checked+.sl:before{transform:translateX(18px)}
.hit{padding:11px 2px;border-bottom:1px solid var(--line)}.hit:last-child{border-bottom:0}
.hit b{color:var(--acc);font-size:14px}
.hit p{margin:4px 0 0;font-size:14px;color:#c8d0d9}
#msg{font-size:13px;color:var(--ok);margin-left:10px;white-space:pre-line}
.err{color:var(--no)!important}
.miss{color:var(--no);font-size:12px}
.warn{display:none;margin:12px 0;padding:11px 13px;border:1px solid #6b5116;border-radius:10px;
 background:#231c0d;color:#e8c46f;font-size:13.5px}
.crow{display:flex;align-items:center;gap:8px;margin:6px 0}
.crow .cl{color:var(--mut);font-size:12px;min-width:170px}
.crow code{flex:1;min-width:0;background:#0b0e12;border:1px solid var(--line);border-radius:7px;
 padding:6px 9px;font-size:12.5px;word-break:break-all}
.cbtns{display:flex;gap:6px;justify-content:flex-end;width:150px;flex:none}
.cp{background:#242c38;color:var(--txt);padding:5px 10px;font-size:12px;font-weight:600}
.prof{display:flex;gap:12px;align-items:flex-start;padding:13px 2px;border-bottom:1px solid var(--line)}
.prof:last-child{border-bottom:0}
.pmain{flex:1;min-width:0}
.pchips{display:flex;flex-wrap:wrap;gap:6px;margin-top:7px}
.pchip{display:inline-flex;position:relative}
.pchip input{position:absolute;opacity:0}
.pchip span{border:1px solid var(--line);background:#10151c;border-radius:99px;padding:3px 12px;
 font-size:12.5px;color:var(--mut);cursor:pointer;user-select:none;transition:all .12s}
.pchip input:checked+span{border-color:#6b5116;background:#2a2110;color:var(--acc)}
.pbtns{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;max-width:200px}
#tokbox{display:none;margin:12px 0;padding:12px 13px;border:1px solid #6b5116;border-radius:10px;background:#241d0e}
#toasts{position:fixed;right:16px;bottom:16px;display:flex;flex-direction:column;gap:8px;z-index:50}
.toast{background:#202934;border:1px solid var(--line);padding:10px 14px;border-radius:10px;
 font-size:13.5px;max-width:360px;box-shadow:0 6px 18px #00000055}
.toast.err{border-color:#5c2b2b;background:#2a1717;color:#f0b1ad}
.grid2{display:grid;grid-template-columns:1fr auto;gap:8px}
@media (max-width:640px){.pbtns{max-width:none}}
</style></head><body><main>
<h1><b>Librarian</b> — your document library</h1>
<div class="stats" id="stats">loading…</div>
<div class="warn" id="warn"></div>

<section><h2>Search your library</h2>
<input type="text" id="q" placeholder="Type a question or words, press Enter">
<div id="results"></div></section>

<section><h2>Keep it up to date</h2>
<button id="sync" onclick="sync()">Check for new files</button>
<label class="crow" style="display:inline-flex;min-width:0;margin-left:14px"><input type="checkbox" id="ocr" style="width:auto"><span class="cl" style="min-width:0">OCR scanned PDFs (slower — only files with no text)</span></label><span id="msg"></span></section>

<section><h2>Library folders</h2>
<div id="libs"></div>
<div class="row"><input type="text" id="newpath" placeholder="Path of a new folder, e.g. /home/you/Documents/Reports">
<button class="sec" onclick="addFolder()">Add folder</button></div>
</section>

<section><h2>Shared access <em>profiles — each friend gets their own token and sees only the folders you pick</em></h2>
<div id="profs">loading…</div>
<div id="tokbox"></div>
<div class="row" style="border-top:1px solid var(--line);padding-top:14px">
 <input type="text" id="pname" placeholder="New profile name, e.g. friend-1">
 <button class="sec" onclick="addProf()">Create profile</button></div>
<div class="pchips" id="newRoots" style="margin-top:10px"></div>
<p class="lbl">A profile's token can be replaced ("new token") or switched off at any time — instantly, no server restart. Profiles are read-only; syncing stays yours.</p>
</section>

<section><h2>Connect an agent (MCP over the network)</h2>
<div id="conn">loading…</div></section>
</main><div id="toasts"></div><script>
const $=id=>document.getElementById(id);
async function j(url,body){const o={headers:{'Content-Type':'application/json'}};
 if(body){o.method='POST';o.body=JSON.stringify(body)}
 const r=await fetch(url,o);const d=await r.json();if(d.error)throw new Error(d.error);return d}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function toast(m,err){const t=document.createElement('div');t.className='toast'+(err?' err':'');t.textContent=m;
 $('toasts').appendChild(t);setTimeout(()=>{t.style.opacity='0';t.style.transition='opacity .4s'},4200);
 setTimeout(()=>t.remove(),4800)}
async function refresh(){
 const s=await j('/api/state');
 $('stats').textContent=s.indexed?
  `${s.searchable} of ${s.total_docs} documents searchable · last checked ${s.last_sync} · index ${s.db_mb} MB`:
  'not indexed yet — add a folder and press “Check for new files”';
 if(s.sync&&s.sync.active){ // sync running (started here or before a reload)
  $('sync').disabled=true;_extinct=true;prog(true);
  $('msg').textContent=`syncing ${s.sync.done}/${s.sync.total}: ${s.sync.file||s.sync.detail||''} (${s.sync.file_seconds||0}s on this file)`;}
 const oc=$('ocr');
 if(!oc.dataset.init&&s.ocr){oc.checked=!!s.ocr.enabled;oc.dataset.init='1';
  if(!s.ocr.available){oc.disabled=true;oc.parentElement.title='tesseract is not available on this machine';oc.parentElement.style.opacity=.55;}}
 const w=$('warn');
 if(s.indexed&&s.textless>0){w.style.display='block';
  w.innerHTML=`${s.textless} file(s) have no searchable text. `
   +(s.tools&&!s.tools.pdftotext?`<b>No PDF extractor is available on this machine</b> — install poppler-utils or run the AppImage v0.2.5+ (it bundles its own). `:'')
   +`<button class="sec tiny" onclick="retryEmpty()">Retry textless files</button>`;}
 else w.style.display='none';
 $('libs').innerHTML=(s.libraries||[]).map(l=>`<div class="lib">
  <label class="sw"><input type="checkbox" ${l.enabled?'checked':''} onchange="toggle('${esc(l.name)}',this.checked)"><span class="sl"></span></label>
  <div><div class="nm">${esc(l.name)} ${l.missing?'<span class="miss">(folder missing!)</span>':''}</div>
  <div class="pth">${esc(l.path)}</div></div>
  <div class="cnt">${l.docs} docs</div>
  <button class="danger tiny" onclick="del('${esc(l.name)}')">Remove</button></div>`).join('')||'<i>No folders yet</i>';
}
let _pp=null,_extinct=false;
function prog(on){ if(on){ if(_pp)return; _pp=setInterval(async()=>{try{const s=await j('/api/state');
  if(s.sync&&s.sync.active){$('msg').textContent=`syncing ${s.sync.done}/${s.sync.total}: ${s.sync.file||s.sync.detail||''} (${s.sync.file_seconds||0}s on this file)`;}
  else if(_extinct){prog(false);$('sync').disabled=false;refresh();}
 }catch(e){}},2500);} else {clearInterval(_pp);_pp=null;} }
async function sync(){ $('sync').disabled=true;$('msg').className='';$('msg').textContent='checking…';_extinct=false;prog(true);
 try{const d=await j('/api/sync',{ocr:$('ocr').checked});$('msg').textContent=d.summary||(d.busy?d.message:'done')}
 catch(e){$('msg').className='err';$('msg').textContent=''+e}
 prog(false);$('sync').disabled=false;refresh()}
async function retryEmpty(){ $('sync').disabled=true;$('msg').textContent='retrying files with no text…';_extinct=false;prog(true);
 try{const d=await j('/api/sync',{retry:true,ocr:$('ocr').checked});$('msg').textContent=d.summary||'done'}
 catch(e){$('msg').className='err';$('msg').textContent=''+e}
 prog(false);$('sync').disabled=false;refresh()}
async function toggle(n,on){try{await j('/api/toggle',{name:n,enabled:on});refresh()}catch(e){toast(e,true)}}
async function addFolder(){const p=$('newpath').value.trim();if(!p)return;
 $('msg').textContent='adding + indexing…';
 try{const d=await j('/api/add',{path:p});$('newpath').value='';$('msg').textContent=d.summary||'added'}
 catch(e){$('msg').className='err';$('msg').textContent=''+e}refresh();profs()}
async function del(n){if(!confirm(`Remove "${n}" from the library? (files on disk are NOT deleted)`))return;
 try{await j('/api/remove',{name:n})}catch(e){toast(e,true)}refresh();profs()}
function copyrow(lbl,val,id){return `<div class="crow"><span class="cl">${lbl}</span>
 <code id="${id}">${esc(val)}</code>
 <span class="cbtns"><button class="cp" onclick="cp('${id}')">copy</button></span></div>`}
function cp(id){navigator.clipboard.writeText(document.getElementById(id).textContent)
 .then(()=>{const b=event.target;b.textContent='copied';setTimeout(()=>b.textContent='copy',1200)})}
async function regen(){if(document.getElementById('tval')&&!confirm('Generate a NEW admin token? The running MCP server keeps the old one until restarted, and every connected client must be updated.'))return;
 try{const d=await j('/api/token',{action:'generate'});
  toast('New admin token saved. '+d.note);conn();}catch(e){toast(e,true)}}
let _folders=[];
function chips(folders,chosen){return folders.map(f=>`<label class="pchip"><input type="checkbox" value="${esc(f)}" ${chosen.includes(f)?'checked':''}><span>${esc(f)}</span></label>`).join('')||'<span class="lbl">no folders yet</span>'}
async function profs(){
 try{const d=await j('/api/profiles');_folders=d.folders||[];
  $('newRoots').innerHTML=chips(_folders,[]);
  $('profs').innerHTML=d.profiles.map(p=>`<div class="prof" data-name="${esc(p.name)}">
   <label class="sw"><input type="checkbox" ${p.enabled?'checked':''} onchange="enProf('${esc(p.name)}',this.checked)"><span class="sl"></span></label>
   <div class="pmain"><div class="nm">${esc(p.name)}
    <button class="sec tiny" onclick="renProf('${esc(p.name)}')">rename</button></div>
    <div class="pchips">${chips(_folders,p.roots)}</div></div>
   <div class="pbtns"><button class="sec tiny" onclick="resetProf('${esc(p.name)}')">new token</button>
    <button class="danger tiny" onclick="delProf('${esc(p.name)}')">delete</button></div></div>`).join('')
   ||'<i class="lbl">No profiles yet — create one to share part of your library.</i>';
  document.querySelectorAll('#profs .pchips input').forEach(i=>i.addEventListener('change',ev=>{
   const row=ev.target.closest('.prof');setRoots(row.dataset.name,
    [...row.querySelectorAll('.pchips input:checked')].map(x=>x.value));}));
 }catch(e){$('profs').innerHTML='<span class="err">'+e+'</span>'}}
function picked(sel){return [...document.querySelectorAll(sel+' input:checked')].map(i=>i.value)}
function showTok(n,tok){$('tokbox').style.display='block';
 $('tokbox').innerHTML=copyrow('Token for '+n+' — hand it out NOW',tok,'ptok')
  +'<p class="lbl">Shown ONCE (only a hash is stored). Your friend uses the same client URL from “Connect an agent”, with this as ?token=… . Lost? “new token” makes a fresh one and this one dies.</p>'}
async function addProf(){const n=$('pname').value.trim();if(!n){toast('Give the profile a name first',true);return}
 try{const d=await j('/api/profiles',{action:'add',name:n,roots:picked('#newRoots')});
  $('pname').value='';$('newRoots').querySelectorAll('input').forEach(i=>i.checked=false);
  showTok(d.name,d.token);profs();}catch(e){toast(e,true)}}
async function setRoots(n,roots){try{await j('/api/profiles',{action:'roots',name:n,roots})}catch(e){toast(e,true);profs()}}
async function enProf(n,on){try{await j('/api/profiles',{action:'enable',name:n,enabled:on});
 toast(on?'Profile enabled — token works again':'Profile disabled — token rejected')}catch(e){toast(e,true)}profs()}
async function renProf(n){const nn=prompt('New name for profile "'+n+'":',n);if(!nn||nn===n)return;
 try{await j('/api/profiles',{action:'rename',name:n,new:nn});profs()}catch(e){toast(e,true)}}
async function resetProf(n){if(!confirm('Replace the token for "'+n+'"? The old token stops working IMMEDIATELY — give this person the new one.'))return;
 try{const d=await j('/api/profiles',{action:'reset',name:n});showTok(n,d.token)}catch(e){toast(e,true)}}
async function delProf(n){if(!confirm('Delete profile "'+n+'"? Its token stops working immediately.'))return;
 try{await j('/api/profiles',{action:'delete',name:n});profs();toast('Profile deleted')}catch(e){toast(e,true)}}
async function conn(){
 try{const c=await j('/api/connection');  let h='';
  if(c.mcp_running){
   h+=`<div class="stats" style="margin:0 0 8px"><span style="color:var(--ok)">● MCP server running</span> on port ${c.mcp_port}</div>`;
   h+=copyrow('Client URL (any MCP app)',c.connect_url,'curl');
   if(c.tailscale_ip)h+=copyrow('This machine on Tailscale',c.tailscale_ip,'tsip');
   if(c.lan_ips.length)h+=copyrow('LAN address(es)',c.lan_ips.join(', '),'lanips');
  }else{
   h+=`<div class="stats" style="margin:0 0 8px"><span style="color:var(--mut)">○ MCP server not running</span> — start it with:</div>`;
   h+=copyrow('Start command',c.serve_cmd,'scmd');
   if(c.tailscale_ip)h+=copyrow('This machine on Tailscale',c.tailscale_ip,'tsip');
   else h+='<p class="err">Tailscale not detected on this machine (install/connect tailscale, or use a LAN address)</p>';
   if(c.lan_ips.length)h+=copyrow('LAN address(es)',c.lan_ips.join(', '),'lanips');
  }
  if(c.token_set)h+=`<div class="crow"><span class="cl">Admin token (full access)</span>
     <code id="tval">${esc(c.token)}</code>
     <span class="cbtns"><button class="cp" onclick="regen()">regenerate</button>
     <button class="cp" onclick="cp('tval')">copy</button></span></div>
     <p class="lbl">This page is reachable only from this machine (127.0.0.1). Use the URL above with this token yourself; friends and colleagues use the SAME URL with their own profile token (see “Shared access”). Changing the admin token takes effect when the MCP server restarts. New profile tokens work immediately.</p>`;
  else h+=`<p class="err">No admin token set — required before the server can start on a network address (profile tokens alone also work).</p>
     <button class="cp" onclick="regen()">generate a token now</button>`;
  $('conn').innerHTML=h;
 }catch(e){$('conn').innerHTML='<span class="err">'+e+'</span>'}}
$('q').addEventListener('keydown',async e=>{if(e.key!=='Enter')return;
 const q=$('q').value.trim();if(!q)return;$('results').innerHTML='<p class="lbl">searching…</p>';
 try{const hits=await j(`/api/search?q=${encodeURIComponent(q)}`);
  $('results').innerHTML=hits.length?hits.map(h=>`<div class="hit"><b>${esc(h.title)}</b>
  <span class="lbl"> · ${esc(h.label||'')} · doc ${h.doc_id}</span>
  <div class="lbl">${esc(h.folder)}</div><p>${esc(h.snippet)}</p></div>`).join('')
  :'<p class="lbl">no matches</p>'}
 catch(e){$('results').innerHTML=`<p class="err">${e}</p>`}});
refresh();profs();conn();setInterval(conn,15000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    cfg_dir: Path  # set on the server class by serve()

    def log_message(self, format, *args):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def do_GET(self):
        u = urlparse(self.path)
        cfg = Config.load(self.cfg_dir)
        try:
            if u.path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif u.path == "/api/state":
                self._json(api_state(cfg))
            elif u.path == "/api/connection":
                self._json(api_connection(cfg))
            elif u.path == "/api/profiles":
                self._json(api_profiles(cfg))
            elif u.path == "/api/search":
                qs = parse_qs(u.query)
                q = (qs.get("q") or [""])[0]
                k = int((qs.get("k") or ["10"])[0])
                if not q:
                    self._json([])
                else:
                    self._json(api_search(cfg, q, k))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        cfg = Config.load(self.cfg_dir)
        try:
            if u.path == "/api/sync":
                self._json(api_sync(cfg, retry_empty=bool(body.get("retry")),
                                    ocr=body.get("ocr") if "ocr" in body else None))
            elif u.path == "/api/add":
                self._json(api_add(cfg, body.get("path", ""), body.get("name")))
            elif u.path == "/api/toggle":
                self._json(api_toggle(cfg, body.get("name", ""), bool(body.get("enabled"))))
            elif u.path == "/api/token":
                self._json(api_token(cfg, body.get("action", ""), body.get("value", "")))
            elif u.path == "/api/profiles":
                self._json(api_profiles(cfg, body))
            elif u.path == "/api/remove":
                self._json(api_remove(cfg, body.get("name", "")))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 400)


def _bind(cfg_dir: Path, port: int, explicit: bool):
    """Bind the loopback server; on a busy DEFAULT port (double-click/AppRun
    launch) slide up to the next free one instead of dying. An explicitly
    requested port fails with a plain-language error instead of a traceback."""
    Handler.cfg_dir = cfg_dir
    try:
        return ThreadingHTTPServer(("127.0.0.1", port), Handler), port
    except OSError as e:
        if e.errno != errno.EADDRINUSE or explicit:
            raise SystemExit(
                f"port {port} is already in use.\n"
                f"  - If the Librarian page is already running here, just open "
                f"http://127.0.0.1:{port}/ and stop this command (Ctrl+C).\n"
                f"  - Otherwise use another port:  librarian ui --port 8790") from None
        for cand in range(port + 1, port + 26):
            try:
                srv = ThreadingHTTPServer(("127.0.0.1", cand), Handler)
                print(f"(port {port} is busy — using {cand} instead)", flush=True)
                return srv, cand
            except OSError:
                continue
        raise SystemExit(
            f"ports {port}-{port + 25} are all in use; pick one: librarian ui --port N") from None


def _open_browser(url: str) -> None:
    """Open the desktop browser WITHOUT the AppImage's loader overrides.
    Inheriting our LD_LIBRARY_PATH makes system helpers (kde-open etc.) load
    the bundled libstdc++ and die, so the page never appeared (v0.2.1)."""
    import subprocess
    env = {k: v for k, v in os.environ.items()
           if k not in ("LD_LIBRARY_PATH", "LD_PRELOAD")}
    for cmd in ([shutil.which("xdg-open") or "xdg-open", url], ["gio", "open", url]):
        try:
            subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return
        except OSError:
            continue
    webbrowser.open(url)  # last resort


def serve(cfg_dir: Path, port: int = DEFAULT_PORT, open_browser: bool = True,
          explicit: bool = False):
    srv, port = _bind(Path(cfg_dir), port, explicit)
    url = f"http://127.0.0.1:{port}/"
    print(f"Librarian UI: {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.7, _open_browser, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        from .ingest import GLOBAL_CANCEL
        GLOBAL_CANCEL.set()      # stop an in-flight sync/prune before we exit
        print("\nstopped")
