"""librarian mcp-serve --http: serve the MCP server over Streamable-HTTP.

Makes librarian reachable from any MCP client on any machine (e.g. over a
Tailscale interface): the client points at  http://HOST:PORT/mcp  and gets the
exact same tools as the stdio transport Hermes uses.

Security model, deliberately simple:
- Loopback bind (127.0.0.1/localhost): a token is optional — the machine's own
  user already owns the box.
- Any other bind (LAN/Tailscale/0.0.0.0): a bearer token is REQUIRED; refuse to
  start without one. Clients send  Authorization: Bearer <token>.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _bearer_guard(app, token: str):
    """ASGI middleware enforcing the bearer token.

    Accepted forms (either is enough):
      Authorization: Bearer <token>   header  (standard)
      ?token=<token>                  query   (fallback for clients whose MCP
                                               config only takes a URL)
    """
    expected = f"Bearer {token}".encode()
    qtoken = token.encode()

    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            # header names arrive lowercased per ASGI spec
            auth = headers.get(b"authorization", b"")
            if auth != expected:
                # fall back to ?token= query parameter
                from urllib.parse import parse_qs
                qs = parse_qs(scope.get("query_string", b"").decode())
                if qs.get("token", [""])[0].encode() != qtoken:
                    body = b'{"error":"unauthorized"}'
                    await send({"type": "http.response.start", "status": 401,
                                "headers": [(b"www-authenticate", b"Bearer"),
                                            (b"content-type", b"application/json"),
                                            (b"content-length", str(len(body)).encode())]})
                    await send({"type": "http.response.body", "body": body})
                    return
        await app(scope, receive, send)

    return wrapped


def serve(cfg_dir: Path | None, host: str = "127.0.0.1", port: int = 8770,
          token: str | None = None, stateless: bool = False) -> None:
    if cfg_dir:
        import os
        os.environ["LIBRARIAN_DATA"] = str(Path(cfg_dir).expanduser())

    import uvicorn
    from .mcp_server import mcp

    if host not in LOOPBACK and not token:
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
    if token:
        app = _bearer_guard(app, token)

    scheme = "http"  # terminate TLS with a reverse proxy (caddy/nginx) if needed
    print(f"librarian MCP (streamable-http) on {scheme}://{host}:{port}/mcp"
          + ("  [bearer token required]" if token else "  [no token — loopback only]"))
    print("  example: mcp-client url = http://"
          + ("127.0.0.1" if host == "0.0.0.0" else host) + f":{port}/mcp")
    uvicorn.run(app, host=host, port=port, log_level="info")
