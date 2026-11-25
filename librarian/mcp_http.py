"""librarian mcp-serve --http: serve the MCP server over Streamable-HTTP.

Makes librarian reachable from any MCP client on any machine (e.g. over a
Tailscale interface): the client points at  http://HOST:PORT/mcp  and gets the
exact same tools as the stdio transport Hermes uses.

Security model (v0.3.0 — shared access profiles):
- Loopback bind (127.0.0.1/localhost) with no credentials configured: the
  machine's own user already owns the box -> full (admin) access.
- The master token ([server] mcp_token / --token) grants full access.
- Any other credential must be a PROFILE token: it authenticates a profile
  that sees only its granted library folders (see librarian/access.py).
  Requests with an unknown credential get 401.
- Any non-loopback bind requires at least one credential (master token or a
  profile); refuse to start otherwise.
Accepted credential forms on every request (either is enough):
    Authorization: Bearer <token>   header  (standard)
    ?token=<token>                  query   (clients whose MCP config is URL-only)
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _extract_token(scope) -> str:
    from urllib.parse import parse_qs
    headers = dict(scope.get("headers") or [])
    auth = headers.get(b"authorization", b"").decode(errors="ignore")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    qs = parse_qs(scope.get("query_string", b"").decode(errors="ignore"))
    return qs.get("token", [""])[0]


async def _send_json(send, code: int, obj) -> None:
    import json
    body = json.dumps(obj).encode()
    await send({"type": "http.response.start", "status": code,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def _gate(app, resolver, data_dir):
    """Root ASGI wrapper.
    1. 401 anything that is not the master token or a valid, enabled profile
       token (when any credential is configured at all).
    2. serves GET /file/<doc_id>: streams the original document file, with
       the v0.4.0 download hierarchy (folder master flag > profile files
       grant; admin always allowed)."""

    async def wrapped(scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/file/"):
            await _serve_file(scope, receive, send, resolver, data_dir)
            return
        if scope["type"] == "http":
            tok = _extract_token(scope)
            if (resolver.master_token or resolver.profile_list()) and not resolver.accepts(tok):
                await _send_json(send, 401, {"error": "unauthorized"})
                return
        await app(scope, receive, send)

    return wrapped


async def _serve_file(scope, receive, send, resolver, data_dir) -> None:
    import mimetypes
    import os
    import re as _re

    if scope.get("method") not in ("GET", "HEAD"):
        await _send_json(send, 405, {"error": "use GET"})
        return
    m = _re.fullmatch(r"/file/(\d+)", scope.get("path", ""))
    if not m:
        await _send_json(send, 404, {"error": "use /file/<doc_id>"})
        return
    doc_id = int(m.group(1))

    from .access import download_block
    from .config import Config
    tok = _extract_token(scope)
    cfg = Config.load(data_dir)
    prof = None
    if tok:
        if resolver.is_admin(tok):
            prof = None                      # admin
        else:
            prof = resolver.profile_for_token(tok)
            if prof is None:
                # unknown/revoked/disabled credential: same 401 as the MCP gate
                await _send_json(send, 401, {"error": "unauthorized"})
                return
    elif resolver.master_token or resolver.profile_list():
        await _send_json(send, 401, {"error": "unauthorized"})
        return

    from .db import connect
    try:
        db = connect(cfg.db_path, read_only=True)
        row = db.execute("SELECT path, root, title FROM docs WHERE id=?", (doc_id,)).fetchone()
        db.close()
    except Exception:
        await _send_json(send, 500, {"error": "index unavailable"})
        return
    if row is None:
        await _send_json(send, 404, {"error": "no such document"})
        return
    path, root = row["path"], row["root"]

    block = download_block(cfg, prof, root)
    if block:
        await _send_json(send, 403, {"error": block})
        return

    # defense against a tampered index: only serve inside configured roots
    try:
        rp = os.path.realpath(path)
        ok = any(rp == os.path.realpath(str(r.path))
                 or rp.startswith(os.path.realpath(str(r.path)) + os.sep)
                 for r in cfg.roots)
    except OSError:
        ok = False
    if not ok or not os.path.isfile(path):
        await _send_json(send, 404, {"error": "file not present on this server"})
        return

    from starlette.responses import FileResponse
    media = mimetypes.guess_type(path)[0] or "application/octet-stream"
    try:
        await FileResponse(path, media_type=media,
                           filename=os.path.basename(path),
                           stat_result=os.stat(path))(scope, receive, send)
    except Exception:
        pass  # client aborted mid-download


def install_profile_scope(mcp_obj, resolver) -> None:
    """Make the MCP dispatch chain profile-aware:
    1. server middleware sets access.CURRENT per message from the credential
       riding on that message (ctx.request — Starlette Request);
    2. tools/list drops admin-only tools while a profile is active, so shared
       clients cannot even discover update_index.
    The tool functions themselves enforce everything too (defense in depth).
    Idempotent: calling serve() twice in one process is safe.
    """
    from .access import ADMIN_ONLY_TOOLS, CURRENT, ProfileMiddleware

    ll = getattr(mcp_obj, "_lowlevel_server", None)
    if ll is None or not hasattr(ll, "middleware"):
        raise RuntimeError("this mcp SDK build has no server middleware chain; "
                           "shared-access profiles need mcp>=2")
    if not any(isinstance(m, ProfileMiddleware) for m in ll.middleware):
        ll.middleware.append(ProfileMiddleware(resolver))
    if getattr(mcp_obj, "_profile_list_tools", None) is not None:
        return                                   # already scoped
    orig = mcp_obj.list_tools

    async def scoped_list_tools():
        tools = await orig()
        if CURRENT.get() is None:
            return tools
        return [t for t in tools if t.name not in ADMIN_ONLY_TOOLS]

    mcp_obj.list_tools = scoped_list_tools       # _handle_list_tools calls self.list_tools()
    mcp_obj._profile_list_tools = scoped_list_tools


def serve(cfg_dir: Path | None, host: str = "127.0.0.1", port: int = 8770,
          token: str | None = None, stateless: bool = False) -> None:
    if cfg_dir:
        os.environ["LIBRARIAN_DATA"] = str(Path(cfg_dir).expanduser())

    import uvicorn
    from .access import Resolver
    from .config import Config
    from .mcp_server import mcp

    data_dir = Path(os.environ.get("LIBRARIAN_DATA") or Config.load().data_dir)
    resolver = Resolver(data_dir)
    if token:
        resolver.override_master(token)   # explicit --token wins over config for this run
    n_profiles = len(resolver.profile_list())

    if host not in LOOPBACK and not resolver.master_token and not n_profiles:
        raise SystemExit(
            f"refusing to bind {host} without a bearer token.\n"
            "  set the token first:  web UI -> 'Connect an agent' -> regenerate,\n"
            "  or add  mcp_token = \"long-r…dom\"  to the [server] section of\n"
            "  the config file (preferred over --token, which leaks into ps and history),\n"
            "  or bind 127.0.0.1 and tunnel/port-forward instead.")

    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=stateless,   # true = friendlier behind proxies, no session resume
        host=host,                  # used by the SDK's DNS-rebinding Host check
    )
    from .ingest import install_shutdown_hook
    install_shutdown_hook(app)      # Ctrl-C must stop an in-flight build/prune
    install_profile_scope(mcp, resolver)
    app = _gate(app, resolver, data_dir)   # auth gate + /file downloads (always:
    # loopback-no-credential = admin; credentials = enforced)
    creds = []
    if resolver.master_token:
        creds.append("master token")
    if n_profiles:
        creds.append(f"{n_profiles} profile token(s)")
    auth_note = (f"  [{' + '.join(creds)} required]" if creds
                 else "  [no token — loopback only]")

    scheme = "http"  # terminate TLS with a reverse proxy (caddy/nginx) if needed
    print(f"librarian MCP (streamable-http) on {scheme}://{host}:{port}/mcp" + auth_note)
    print(f"  document files: {scheme}://{host}:{port}/file/<doc_id> (same token; "
          "subject to per-folder + per-profile download switches)")
    print("  example: mcp-client url = http://"
          + ("127.0.0.1" if host == "0.0.0.0" else host) + f":{port}/mcp")
    uvicorn.run(app, host=host, port=port, log_level="info")
