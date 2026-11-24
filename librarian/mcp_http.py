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


def _guard(app, resolver):
    """HTTP gate: 401 anything that is not the master token or a valid,
    enabled profile token."""

    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            from urllib.parse import parse_qs
            headers = dict(scope.get("headers") or [])
            tok = ""
            # header names arrive lowercased per ASGI spec
            auth = headers.get(b"authorization", b"").decode(errors="ignore")
            if auth.startswith("Bearer "):
                tok = auth[7:].strip()
            if not tok:
                qs = parse_qs(scope.get("query_string", b"").decode(errors="ignore"))
                tok = qs.get("token", [""])[0]
            if not resolver.accepts(tok):
                body = b'{"error":"unauthorized"}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"www-authenticate", b"Bearer"),
                                        (b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await app(scope, receive, send)

    return wrapped


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
    if resolver.master_token or n_profiles:
        app = _guard(app, resolver)
        creds = []
        if resolver.master_token:
            creds.append("master token")
        if n_profiles:
            creds.append(f"{n_profiles} profile token(s)")
        auth_note = f"  [{' + '.join(creds)} required]"
    else:
        auth_note = "  [no token — loopback only]"

    scheme = "http"  # terminate TLS with a reverse proxy (caddy/nginx) if needed
    print(f"librarian MCP (streamable-http) on {scheme}://{host}:{port}/mcp" + auth_note)
    print("  example: mcp-client url = http://"
          + ("127.0.0.1" if host == "0.0.0.0" else host) + f":{port}/mcp")
    uvicorn.run(app, host=host, port=port, log_level="info")
