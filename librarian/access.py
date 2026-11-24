"""Per-request access scope for shared MCP profiles (v0.3.0).

Model
-----
- The admin runs the server on this machine: the master token (or no token
  on loopback / stdio) = full access. That is `CURRENT.get() is None`.
- A *profile* authenticates with its own token; it sees ONLY the library
  folders named in Profile.roots. Enforcement lives here + in the MCP tool
  layer (every query gains the profile's excluded roots; ID-based tools check
  membership; the one mutating tool, update_index, is admin-only and hidden
  from a profile's tools/list).

Propagation
-----------
The streamable-HTTP transport hands the Starlette Request to the MCP message
dispatcher, so a tiny server middleware (installed by mcp_http.serve) reads
the bearer/`?token=` credential per message and stores the resolved profile
in a contextvar. Sync tools run through anyio worker threads, which copy the
context — the value is visible no matter which worker executes the tool.
stdio has no token: the contextvar stays None (admin).
"""
from __future__ import annotations

import contextvars
import threading
from pathlib import Path
from urllib.parse import parse_qs

from .config import Config, Profile, token_matches

# active profile for the current MCP request; None = admin (master/local/stdio)
CURRENT: contextvars.ContextVar[Profile | None] = contextvars.ContextVar(
    "librarian_profile", default=None)

# tools a shared profile must never see or call (everything else is read-only)
ADMIN_ONLY_TOOLS = frozenset({"update_index"})


def credential_from_request(request) -> str:
    """Bearer header or ?token= query — same two forms the gate accepts."""
    if request is None:            # stdio transport
        return ""
    auth = ""
    try:
        auth = request.headers.get("authorization") or ""
    except Exception:
        auth = ""
    if auth.startswith("Bearer ") and auth[7:].strip():
        return auth[7:].strip()
    try:  # starlette Request: .query_params is the canonical accessor
        tok = request.query_params.get("token", "")
        if tok:
            return tok
    except Exception:
        pass
    try:  # raw ASGI-ish fallback
        raw = getattr(request, "query_string", b"") or b""
        if isinstance(raw, bytes):
            raw = raw.decode(errors="ignore")
        return parse_qs(raw).get("token", [""])[0]
    except Exception:
        return ""


class Resolver:
    """Maps token -> Profile. Re-reads config.toml when its mtime changes, so
    UI/CLI profile edits apply to the RUNNING server without a restart."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self._lock = threading.Lock()
        self._mtime: float = -1.0
        self._master: str = ""
        self._master_override: str | None = None
        self._by_hash: dict[str, Profile] = {}

    def _refresh(self) -> None:
        cfg_path = self.data_dir / "config.toml"
        try:
            mtime = cfg_path.stat().st_mtime_ns
        except OSError:
            return
        if mtime == self._mtime:
            return
        with self._lock:
            if mtime == self._mtime:
                return
            try:
                cfg = Config.load(self.data_dir)
            except Exception:
                return
            self._master = cfg.mcp_token
            self._by_hash = {p.token_hash: p for p in cfg.profiles
                             if p.enabled and p.token_hash}
            self._mtime = mtime

    @property
    def master_token(self) -> str:
        self._refresh()
        return self._master_override or self._master

    def override_master(self, token: str) -> None:
        """Explicit --token on the command line wins over [server] mcp_token,
        persistently for this process (a later config rewrite must not
        silently restore the config value)."""
        self._master_override = token

    def profile_list(self) -> list[Profile]:
        self._refresh()
        return list(self._by_hash.values())

    def is_admin(self, token: str) -> bool:
        self._refresh()
        return bool(token) and token_matches(self.master_token, token)

    def profile_for_token(self, token: str) -> Profile | None:
        self._refresh()
        if not token:
            return None
        return self._by_hash.get("sha256:" + _sha256_hex(token))

    def accepts(self, token: str) -> bool:
        """True for the master token or any enabled profile's token."""
        self._refresh()
        if not token:
            return False
        return self.is_admin(token) or (self.profile_for_token(token) is not None)


def _sha256_hex(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode()).hexdigest()


def excluded_roots(cfg: Config, prof: Profile | None) -> list[str] | None:
    """Folders to hide from a query. Admin: only user-disabled folders
    (existing behavior). Profile: everything it was not granted (plus
    admin-disabled ones)."""
    disabled = set(cfg.disabled_roots)
    if prof is None:
        return sorted(disabled) or None
    names = {r.name for r in cfg.roots}
    allowed = {n for n in prof.roots} & names
    return sorted((names - allowed) | disabled) or None


def visible_root(cfg: Config, prof: Profile | None, root: str | None) -> bool:
    """Membership test for ID-based access (read_doc/doc_card/related input):
    an unattributed row is invisible to profiles — only roots explicitly
    granted are visible."""
    if prof is None:
        return root not in set(cfg.disabled_roots)
    if root is None:
        return False
    return (root in set(prof.roots)) and (root not in set(cfg.disabled_roots))


def doc_root(db, doc_id: int) -> str | None | bool:
    """root column of a doc; False means the doc does not exist at all."""
    row = db.execute("SELECT root FROM docs WHERE id=?", (doc_id,)).fetchone()
    if row is None:
        return False
    try:
        return row["root"]
    except (IndexError, KeyError, TypeError):
        return row[0] if len(row) else None


class ProfileMiddleware:
    """MCP server middleware: resolve the credential of THIS message and run
    the handler with CURRENT set. Registered on the lowlevel server's
    middleware chain by librarian.mcp_http."""

    def __init__(self, resolver: Resolver):
        self.resolver = resolver

    async def __call__(self, ctx, call_next):
        token = credential_from_request(getattr(ctx, "request", None))
        prof = self.resolver.profile_for_token(token) if token else None
        # a non-admin credential that is not a valid profile never reaches
        # here (the HTTP gate 401s it); if a stale profile was deleted
        # mid-session, fall back to its last-known object or deny broadly:
        if token and prof is None and not self.resolver.is_admin(token):
            prof = _DENIED
        t = CURRENT.set(prof)
        try:
            return await call_next(ctx)
        finally:
            CURRENT.reset(t)


class _Denied:
    """A profile that grants nothing (token revoked/deleted mid-session)."""
    name = "(revoked)"
    token_hash = ""
    roots: list[str] = []
    enabled = False


_DENIED = _Denied()
