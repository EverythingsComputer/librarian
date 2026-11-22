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

_sync_lock = threading.Lock()
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
    dups = db.execute("SELECT COUNT(*) c FROM docs WHERE dup_of IS NOT NULL").fetchone()["c"]
    db.close()
    total = report.get("docs", 0)
    searchable = max(0, total - dups - hidden)
    parts = []
    for key, word in (("added", "new"), ("changed", "changed"), ("removed", "removed")):
        n = report.get(key, 0)
        if n:
            parts.append(f"{n} {word}")
    what = ", ".join(parts) if parts else "nothing new"
    off = f" — {len(off_names(cfg))} folder(s) switched off, {hidden} docs hidden" if excl else ""
    return (f"Checked: {what}. Library: {total} documents, {searchable} searchable{off}. "
            f"Took {report.get('seconds', 0)}s.")


def off_names(cfg: Config) -> list[str]:
    return [r.name for r in cfg.roots if not r.enabled]


# ------------------------------------------------------------------ API logic

def _sh(cmd: list[str], timeout: float = 2.0) -> str:
    import subprocess
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
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
        "serve_cmd": (f"librarian mcp-serve --transport http "
                      f"--host {ts_ip or '0.0.0.0'} --port {port}"
                      + ("" if tok else " --token 'pick-a-long-random-secret'")),
    }


def api_state(cfg: Config) -> dict:
    from .db import connect
    out = {"libraries": [], "indexed": cfg.db_path.exists()}
    if not out["indexed"]:
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
        searchable = db.execute(
            f"SELECT COUNT(*) n FROM docs WHERE dup_of IS NULL AND root IN ({ph})",
            searchable_roots).fetchone()["n"]
    else:
        searchable = 0
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
    out.update(last_sync=time.strftime("%Y-%m-%d %H:%M", time.localtime(float(row["v"] or 0))) if row else "",
               total_docs=total, duplicates=dups, searchable=searchable,
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


def api_sync(cfg: Config) -> dict:
    if not _sync_lock.acquire(blocking=False):
        return {"busy": True, "message": "A sync is already running."}
    try:
        from .ingest import build_or_update
        report: dict = {}
        build_or_update(cfg, report=report)
        return {"busy": False, "report": report, "summary": human_summary(cfg, report)}
    finally:
        _sync_lock.release()


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


# ------------------------------------------------------------------ HTTP

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Librarian</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#111418;--card:#1b2026;--txt:#e6e9ed;--mut:#8a94a0;--acc:#f5a623;--ok:#3fb96a;--line:#2a313a}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--txt)}
main{max-width:860px;margin:0 auto;padding:24px 16px 80px}
h1{font-size:22px;margin:0 0 2px}h1 b{color:var(--acc)}
.stats{color:var(--mut);font-size:13px;margin-bottom:20px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:0 0 12px}
input[type=text]{width:100%;padding:10px 12px;border-radius:8px;border:1px solid var(--line);
background:#0d1013;color:var(--txt);font-size:15px}
button{background:var(--acc);color:#141414;border:0;border-radius:8px;padding:9px 16px;
font-weight:600;cursor:pointer;font-size:14px}
button.sec{background:#2a313a;color:var(--txt)}
button:disabled{opacity:.5;cursor:wait}
.row{display:flex;gap:8px;margin-top:10px}.row input{flex:1}
.lib{display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid var(--line)}
.lib:last-child{border-bottom:0}
.lib .nm{font-weight:600}.lib .pth{color:var(--mut);font-size:12px;word-break:break-all}
.lib .cnt{margin-left:auto;color:var(--mut);font-size:13px;white-space:nowrap}
.sw{position:relative;width:40px;height:22px;flex:none}
.sw input{opacity:0;width:0;height:0}
.sl{position:absolute;inset:0;background:#3a424c;border-radius:22px;transition:.15s;cursor:pointer}
.sl:before{content:"";position:absolute;width:16px;height:16px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.15s}
input:checked+.sl{background:var(--ok)}input:checked+.sl:before{transform:translateX(18px)}
.hit{padding:10px 0;border-bottom:1px solid var(--line)}.hit:last-child{border-bottom:0}
.hit b{color:var(--acc);font-size:14px}.hit .lbl{color:var(--mut);font-size:12px}
.hit p{margin:4px 0 0;font-size:14px;color:#c7cdd4}
#msg{font-size:13px;color:var(--ok);margin-left:10px;white-space:pre-line}
.err{color:#e5645f!important}.del{background:none;color:#e5645f;padding:4px 8px;font-size:13px}
.miss{color:#e5645f;font-size:12px}
.crow{display:flex;align-items:center;gap:8px;margin:6px 0}
.crow .cl{color:var(--mut);font-size:12px;min-width:170px}
.crow code{flex:1;background:#0d1013;border:1px solid var(--line);border-radius:6px;
 padding:6px 9px;font-size:12.5px;word-break:break-all}
.cp{background:#2a313a;color:var(--txt);padding:5px 10px;font-size:12px}
</style></head><body><main>
<h1><b>Librarian</b> — your document library</h1>
<div class="stats" id="stats">loading…</div>

<section><h2>Search your library</h2>
<input type="text" id="q" placeholder="Type a question or words, press Enter">
<div id="results"></div></section>

<section><h2>Keep it up to date</h2>
<button id="sync" onclick="sync()">Check for new files</button><span id="msg"></span></section>

<section><h2>Library folders</h2>
<div id="libs"></div>
<div class="row"><input type="text" id="newpath" placeholder="Path of a new folder, e.g. /home/you/Documents/Reports">
<button class="sec" onclick="addFolder()">Add folder</button></div>
</section>

<section><h2>Connect an agent (MCP over the network)</h2>
<div id="conn">loading…</div></section>
</main><script>
const $=id=>document.getElementById(id);
async function j(url,body){const o={headers:{'Content-Type':'application/json'}};
 if(body){o.method='POST';o.body=JSON.stringify(body)}
 const r=await fetch(url,o);const d=await r.json();if(d.error)throw new Error(d.error);return d}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function refresh(){
 const s=await j('/api/state');
 $('stats').textContent=s.indexed?
  `${s.searchable} of ${s.total_docs} documents searchable · last checked ${s.last_sync} · index ${s.db_mb} MB`:
  'not indexed yet — add a folder and press “Check for new files”';
 $('libs').innerHTML=(s.libraries||[]).map(l=>`<div class="lib">
  <label class="sw"><input type="checkbox" ${l.enabled?'checked':''} onchange="toggle('${esc(l.name)}',this.checked)"><span class="sl"></span></label>
  <div><div class="nm">${esc(l.name)} ${l.missing?'<span class="miss">(folder missing!)</span>':''}</div>
  <div class="pth">${esc(l.path)}</div></div>
  <div class="cnt">${l.docs} docs</div>
  <button class="del" onclick="del('${esc(l.name)}')">Remove</button></div>`).join('')||'<i>No folders yet</i>';
}
async function sync(){ $('sync').disabled=true;$('msg').className='';$('msg').textContent='checking…';
 try{const d=await j('/api/sync',{});$('msg').textContent=d.summary||(d.busy?d.message:'done')}
 catch(e){$('msg').className='err';$('msg').textContent=''+e}
 $('sync').disabled=false;refresh()}
async function toggle(n,on){try{await j('/api/toggle',{name:n,enabled:on});refresh()}catch(e){alert(e)}}
async function addFolder(){const p=$('newpath').value.trim();if(!p)return;
 $('msg').textContent='adding + indexing…';
 try{const d=await j('/api/add',{path:p});$('newpath').value='';$('msg').textContent=d.summary||'added'}
 catch(e){$('msg').className='err';$('msg').textContent=''+e}refresh()}
async function del(n){if(!confirm(`Remove "${n}" from the library? (files on disk are NOT deleted)`))return;
 try{await j('/api/remove',{name:n})}catch(e){alert(e)}refresh()}
function copyrow(lbl,val,id){return `<div class="crow"><span class="cl">${lbl}</span>
 <code id="${id}">${esc(val)}</code>
 <button class="cp" onclick="cp('${id}')">copy</button></div>`}
function cp(id){navigator.clipboard.writeText(document.getElementById(id).textContent)
 .then(()=>{const b=event.target;b.textContent='copied';setTimeout(()=>b.textContent='copy',1200)})}
async function regen(){if(!confirm('Generate a NEW access token? The running MCP server keeps the old one until restarted, and every connected client must be updated.'))return;
 try{const d=await j('/api/token',{action:'generate'});
  alert('New token: '+d.token+'. '+d.note);conn();}catch(e){alert(e)}}
async function conn(){
 try{const c=await j('/api/connection');  let h='';
  if(c.mcp_running){
   h+=`<div class="stats" style="margin:0 0 8px"><span style="color:var(--ok)">● MCP server running</span> on port ${c.mcp_port}</div>`;
   h+=copyrow('Client URL (any MCP app)',c.connect_url,'curl');
   if(c.tailscale_ip)h+=copyrow('This machine on Tailscale',c.tailscale_ip,'tsip');
   if(c.lan_ips.length)h+=copyrow('LAN address(es)',c.lan_ips.join(', '),'lanips');
   if(c.token_set)h+=`<div class="crow"><span class="cl">Access token</span>
     <code id="tval">${esc(c.token)}</code>
     <button class="cp" onclick="cp('tval')">copy</button>
     <button class="cp" onclick="regen()">regenerate</button></div>
     <p class="lbl">This page is reachable only from this machine (127.0.0.1); clients elsewhere use the URL above with this token.</p>`;
   else h+='<p class="err">no token set — add <code>mcp_token</code> under <code>[server]</code> in config.toml or restart with --token; it is required off-localhost</p>';
  }else{
   h+=`<div class="stats" style="margin:0 0 8px"><span style="color:var(--mut)">○ MCP server not running</span> — start it with:</div>`;
   h+=copyrow('Start command',c.serve_cmd,'scmd');
   if(c.tailscale_ip)h+=copyrow('This machine on Tailscale',c.tailscale_ip,'tsip');
   else h+='<p class="err">Tailscale not detected on this machine (install/connect tailscale, or use a LAN address)</p>';
   if(c.lan_ips.length)h+=copyrow('LAN address(es)',c.lan_ips.join(', '),'lanips');
  }
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
refresh();conn();setInterval(conn,15000);
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
                self._json(api_sync(cfg))
            elif u.path == "/api/add":
                self._json(api_add(cfg, body.get("path", ""), body.get("name")))
            elif u.path == "/api/toggle":
                self._json(api_toggle(cfg, body.get("name", ""), bool(body.get("enabled"))))
            elif u.path == "/api/token":
                self._json(api_token(cfg, body.get("action", ""), body.get("value", "")))
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
        print("\nstopped")
