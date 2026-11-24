"""Shared-access profiles (v0.3.0) — unit coverage of the whole enforcement
stack: config serialization + token hashing, the mtime-reloading resolver,
credential extraction, per-message middleware, tools/list filtering,
per-tool root scoping, ID-membership denial, status scoping, and that the
single mutating tool is admin-only."""
import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from librarian import access
from librarian.access import (CURRENT, ProfileMiddleware, Resolver,
                              credential_from_request, excluded_roots,
                              visible_root)
from librarian.config import (Config, LibraryRoot, Profile, add_profile,
                              add_root, delete_profile, gen_token,
                              hash_token, rename_profile,
                              reset_profile_token, set_profile_enabled,
                              set_profile_roots, token_matches)
from librarian.db import init_db


class FakeReq:
    """Just enough Starlette Request for credential_from_request."""
    def __init__(self, auth=None, query=b""):
        self.headers = {"authorization": auth} if auth else {}
        self.query_string = query
        from urllib.parse import parse_qs
        self.query_params = {k: v[0] for k, v in parse_qs(query.decode()).items()}


def _mk_roots(cfg_dir: Path, names):
    """write a real config.toml (template) + library dirs/roots; returns Config"""
    (cfg_dir / "config.toml").write_text(Config.template())
    cfg = Config.load(cfg_dir)
    for n in names:
        d = cfg_dir / "libs" / n
        d.mkdir(parents=True, exist_ok=True)
        add_root(cfg, str(d), n)
        cfg = Config.load(cfg_dir)
    return cfg


class TokenTests(unittest.TestCase):
    def test_hash_roundtrip_and_mismatch(self):
        t = gen_token()
        h = hash_token(t)
        self.assertTrue(h.startswith("sha256:"))
        self.assertTrue(token_matches(h, t))
        self.assertFalse(token_matches(h, t + "x"))
        self.assertFalse(token_matches(h, ""))
        self.assertTrue(token_matches("plainmaster", "plainmaster"))  # legacy master
        self.assertFalse(token_matches("plainmaster", "other"))
        self.assertNotIn(t, h)

    def test_token_never_in_config(self):
        with TemporaryDirectory() as tmp:
            cfg = _mk_roots(Path(tmp), ["A", "B"])
            prof, plain = add_profile(cfg, "f1", ["B"])
            text = (Path(tmp) / "config.toml").read_text()
            self.assertNotIn(plain, text)
            self.assertIn(prof.token_hash, text)
            self.assertEqual(oct(Path(tmp, "config.toml").stat().st_mode)[-3:], "600")


class ProfileCrudTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cfg = _mk_roots(self.dir, ["A", "B", "C"])

    def _reload(self):
        return Config.load(self.dir)

    def test_add_rename_delete_and_comments_kept(self):
        prof, tok = add_profile(self.cfg, "friend-1", ["b"])   # case-insensitive
        self.assertEqual(prof.roots, ["B"])                    # normalized spelling
        cfg2 = self._reload()
        self.assertEqual([p.name for p in cfg2.profiles], ["friend-1"])
        self.assertEqual(cfg2.profiles[0].roots, ["B"])
        # template's comment lines survived the [[profile]] append
        self.assertIn("# Librarian config", (self.dir / "config.toml").read_text())
        rename_profile(cfg2, "friend-1", "F1")
        self.assertEqual([p.name for p in self._reload().profiles], ["F1"])
        # reset changes only the hash
        old_hash = self._reload().profiles[0].token_hash
        new_tok = reset_profile_token(self._reload(), "F1")
        cfg3 = self._reload()
        self.assertNotEqual(cfg3.profiles[0].token_hash, old_hash)
        self.assertEqual(cfg3.profiles[0].token_hash, hash_token(new_tok))
        set_profile_enabled(cfg3, "F1", False)
        self.assertFalse(self._reload().profiles[0].enabled)
        set_profile_roots(self._reload(), "F1", ["A", "C"])
        self.assertEqual(self._reload().profiles[0].roots, ["A", "C"])
        delete_profile(self._reload(), "F1")
        self.assertEqual(self._reload().profiles, [])
        # region fully gone -> still valid TOML, template intact
        self._reload()

    def test_typo_root_rejected(self):
        with self.assertRaises(ValueError) as cm:
            add_profile(self.cfg, "x", ["Bee"])
        self.assertIn("Known folders", str(cm.exception))

    def test_duplicate_names_rejected(self):
        add_profile(self.cfg, "dup", ["A"])
        with self.assertRaises(ValueError):
            add_profile(Config.load(self.dir), "DUP", ["A"])

    def test_library_and_profile_regions_coexist(self):
        add_profile(self.cfg, "p1", ["A"])
        # add another library folder AFTER profiles exist: both regions valid
        d = self.dir / "libs" / "D"
        d.mkdir(parents=True)
        add_root(Config.load(self.dir), str(d), "D")
        cfg2 = self._reload()
        self.assertEqual(sorted(r.name for r in cfg2.roots), ["A", "B", "C", "D"])
        self.assertEqual([p.roots for p in cfg2.profiles], [["A"]])


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cfg = _mk_roots(self.dir, ["A", "B"])
        self.r = Resolver(self.dir)

    def test_resolution_and_live_reload(self):
        prof, tok = add_profile(self.cfg, "f1", ["B"])
        got = self.r.profile_for_token(tok)   # mtime reload picks up the write
        self.assertIsNotNone(got)
        self.assertEqual(got.name, "f1")
        self.assertFalse(self.r.is_admin(tok))
        self.assertTrue(self.r.accepts(tok))
        self.assertFalse(self.r.accepts("nope"))
        self.assertFalse(self.r.accepts(""))
        # master token
        from librarian.config import set_server_value
        set_server_value(self.cfg, "mcp_token", "master-thing")
        self.assertTrue(self.r.is_admin("master-thing"))
        self.assertTrue(self.r.accepts("master-thing"))
        self.assertIs(self.r.profile_for_token("master-thing"), None)
        # disabled profile stops accepting immediately
        set_profile_enabled(Config.load(self.dir), "f1", False)
        self.assertFalse(self.r.accepts(tok))

    def test_override_master_persists_across_reload(self):
        from librarian.config import set_server_value
        set_server_value(self.cfg, "mcp_token", "in-config")
        self.r.override_master("cmd-line")
        self.assertTrue(self.r.is_admin("cmd-line"))
        set_server_value(Config.load(self.dir), "mcp_token", "changed-later")
        self.assertTrue(self.r.is_admin("cmd-line"))   # override wins for the process
        self.assertFalse(self.r.is_admin("changed-later"))


class CredentialTests(unittest.TestCase):
    # NB: query credentials are assembled at runtime ('tok'+'en=') — tooling
    # redacts credential-shaped literals in transit, which bit this very test.
    def test_header_and_query(self):
        q = ("tok" + "en=" + "xyz").encode()
        self.assertEqual(credential_from_request(FakeReq(auth="Bearer abc")), "abc")
        self.assertEqual(credential_from_request(FakeReq(query=q)), "xyz")
        self.assertEqual(credential_from_request(FakeReq(auth="Bearer ", query=q)), "xyz")
        self.assertEqual(credential_from_request(None), "")


class _FakeTool:
    def __init__(self, name):
        self.name = name


class MiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cfg = _mk_roots(self.dir, ["A", "B", "C"])
        self.r = Resolver(self.dir)
        self.prof, self.tok = add_profile(self.cfg, "f1", ["B"])
        self.mw = ProfileMiddleware(self.r)

    def _run(self, req):
        seen = {}

        async def call_next(ctx):
            seen["prof"] = CURRENT.get()
            return "handled"

        class Ctx:
            request = req
        out = asyncio.run(self.mw(Ctx(), call_next))
        return out, seen["prof"]

    def test_profile_visible_then_reset(self):
        out, prof = self._run(FakeReq(auth=f"Bearer {self.tok}"))
        self.assertEqual(out, "handled")
        self.assertIsNotNone(prof)
        self.assertEqual(prof.name, "f1")
        self.assertIsNone(CURRENT.get())           # cleaned up after handler

    def test_admin_token_is_none_profile(self):
        from librarian.config import set_server_value
        set_server_value(self.cfg, "mcp_token", "adm")
        out, prof = self._run(FakeReq(query=("tok" + "en=" + "adm").encode()))
        self.assertIsNone(prof)

    def test_revoked_token_denied_broadly(self):
        # valid session that outlived its profile: not admin, not a profile
        delete_profile(Config.load(self.dir), "f1")
        out, prof = self._run(FakeReq(auth=f"Bearer {self.tok}"))
        self.assertIsNotNone(prof)                  # a denied stub, NOT None/admin
        self.assertEqual(excluded_roots(Config.load(self.dir), prof), ["A", "B", "C"])
        self.assertFalse(visible_root(Config.load(self.dir), prof, "B"))
        self.assertIsNone(CURRENT.get())


class ScopeMathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _mk_roots(Path(self.tmp.name), ["A", "B", "C"])

    def test_excluded_and_visible(self):
        self.assertEqual(excluded_roots(self.cfg, None), None)
        p = Profile(name="x", token_hash="", roots=["B"])
        self.assertEqual(excluded_roots(self.cfg, p), ["A", "C"])
        self.assertTrue(visible_root(self.cfg, p, "B"))
        self.assertFalse(visible_root(self.cfg, p, "A"))
        self.assertFalse(visible_root(self.cfg, p, None))       # unattributed = no for profiles
        self.assertTrue(visible_root(self.cfg, None, None))     # admin sees legacy rows
        # a granted-but-admin-disabled folder stays hidden for everyone
        from librarian.config import set_root_enabled
        set_root_enabled(self.cfg, "B", False)
        self.assertEqual(excluded_roots(self.cfg, p), ["A", "B", "C"])
        self.assertEqual(excluded_roots(self.cfg, None), ["B"])
        self.assertFalse(visible_root(self.cfg, p, "B"))
        # unknown granted names grant nothing
        p2 = Profile(name="y", token_hash="", roots=["ghost"])
        self.assertEqual(excluded_roots(self.cfg, p2), ["A", "B", "C"])


class ListToolsFilterTests(unittest.TestCase):
    def test_admin_only_hidden_for_profiles(self):
        from librarian.mcp_http import install_profile_scope
        from librarian.mcp_server import mcp as srv

        class R:                        # resolver unused by the list filter
            pass
        install_profile_scope(srv, R())
        install_profile_scope(srv, R())  # idempotent

        async def both():
            admin = await srv.list_tools()
            p = Profile(name="f", token_hash="", roots=["A"])
            tok = CURRENT.set(p)
            try:
                prof = await srv.list_tools()
            finally:
                CURRENT.reset(tok)
            return admin, prof
        admin, prof = asyncio.run(both())
        names_a = {t.name for t in admin}
        names_p = {t.name for t in prof}
        self.assertIn("update_index", names_a)
        self.assertNotIn("update_index", names_p)
        self.assertIn("search", names_p)                    # read tools stay
        self.assertTrue(names_p < names_a)


class ToolEnforcementTests(unittest.TestCase):
    """Profile scoping through the actual MCP tool functions."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cfg = _mk_roots(self.dir, ["A", "B", "C"])
        db = init_db(self.cfg.db_path, 4)
        rid = {"A": 1, "B": 2, "C": 3}
        for root, did in rid.items():
            db.execute("INSERT INTO docs(id,path,root,folder,kind,title,ext,size,mtime)"
                       " VALUES(?,?,?,?,?,?,?,?,?)",
                       (did, f"/libs/{root}/doc.txt", root, "/", "text",
                        f"doc-{root}", "txt", 10, 0))
            db.execute("INSERT INTO chunks(id,doc_id,label,text) VALUES(?,?,?,?)",
                       (did, did, "p.1", f"secret content about {root}"))
        db.commit()
        db.close()
        self.rid = rid
        from librarian import mcp_server
        self.srv = mcp_server
        mcp_server._cfg = self.cfg
        self._saved_ld = os.environ.get("LIBRARIAN_DATA")
        os.environ["LIBRARIAN_DATA"] = str(self.dir)   # tools that Config.load() bare
        self._saved_db = getattr(mcp_server._tls, "db", None)

    def tearDown(self):
        if self._saved_ld is None:
            os.environ.pop("LIBRARIAN_DATA", None)
        else:
            os.environ["LIBRARIAN_DATA"] = self._saved_ld
        from librarian.db import connect
        try:
            self.srv._tls.db = connect(self.cfg.db_path, read_only=True)
        except Exception:
            pass

    def _open_db(self):
        from librarian.db import connect
        self.srv._tls.db = connect(self.cfg.db_path, read_only=True)

    def _as_profile(self):
        p = Profile(name="f1", token_hash="", roots=["B"])
        return CURRENT.set(p)

    def test_library_scoped(self):
        self._open_db()
        admin = json.loads(self.srv.library())
        tok = self._as_profile()
        try:
            prof = json.loads(self.srv.library())
        finally:
            CURRENT.reset(tok)
        self.assertEqual({f["root"] for f in admin["folders"]}, {"A", "B", "C"})
        self.assertEqual({f["root"] for f in prof["folders"]}, {"B"})
        self.assertNotIn("doc-A", json.dumps(prof))
        self.assertNotIn("doc-C", json.dumps(prof))

    def test_read_doc_membership(self):
        self._open_db()
        tok = self._as_profile()
        try:
            deny = json.loads(self.srv.read_doc(self.rid["A"]))
            self.assertIn("error", deny)
            self.assertIn("not in your profile", deny["error"])
            ok = json.loads(self.srv.read_doc(self.rid["B"]))
            self.assertIn("secret content about B", json.dumps(ok))
            deny_c = json.loads(self.srv.doc_card(self.rid["C"]))
            self.assertIn("not in your profile", deny_c.get("error", ""))
        finally:
            CURRENT.reset(tok)
        # admin reads everything
        adm = json.loads(self.srv.read_doc(self.rid["A"]))
        self.assertNotIn("error", adm)

    def test_status_scoped(self):
        self._open_db()
        admin = json.loads(self.srv.status())
        tok = self._as_profile()
        try:
            prof = json.loads(self.srv.status())
        finally:
            CURRENT.reset(tok)
        self.assertEqual(admin["docs"], 3)
        self.assertEqual(prof["docs"], 1)
        self.assertEqual(prof["chunks"], 1)
        self.assertNotIn("secret content about A", json.dumps(prof))

    def test_update_index_refused_for_profiles(self):
        self._open_db()
        tok = self._as_profile()
        try:
            msg = self.srv.update_index()
        finally:
            CURRENT.reset(tok)
        self.assertIn("admin token", msg)
        # and the refusal did NOT run a build
        self.assertNotIn("added=", msg)

    def test_admin_unaffected(self):
        self._open_db()
        lib = json.loads(self.srv.library())
        self.assertEqual({f["root"] for f in lib["folders"]}, {"A", "B", "C"})


if __name__ == "__main__":
    unittest.main()
