"""v0.4.0 — downloadable originals + searchable-copy export.

Covers: the download hierarchy (folder master flag > profile files grant >
admin), /file/<doc_id> ASGI behavior end-to-end at the app layer, the
file_url tool, export selection logic, and a REAL scanned-PDF export whose
invisible text layer is proven extractable from the copy while the original
stays byte-identical.
"""
import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from librarian import access, mcp_http
from librarian.access import CURRENT, ORIGIN, download_block
from librarian.config import (Config, LibraryRoot, Profile, add_profile,
                              add_root, set_profile_files, set_root_files)
from librarian.db import init_db
from librarian.export import exportable_docs, run_export

_ORIG_LD = os.environ.get("LIBRARIAN_DATA")
_ORIG_PATH = os.environ.get("PATH", "")

# give unit tests the same tesseract the AppImage bundles use
_TESS = Path.home() / "buildtools" / "tessprefix" / "usr" / "bin"
if _TESS.is_dir():
    os.environ["PATH"] = str(_TESS) + os.pathsep + _ORIG_PATH
    for _tl in (_TESS.parent / "lib", _TESS.parent / "lib" / "x86_64-linux-gnu"):
        if _tl.is_dir():   # deb-extracted tesseract needs its own loader path
            os.environ["LD_LIBRARY_PATH"] = str(_tl) + os.pathsep + os.environ.get("LD_LIBRARY_PATH", "")
    _TD = Path.home() / "buildtools" / "tessprefix" / "usr" / "share" / "tesseract-ocr" / "5" / "tessdata"
    if _TD.is_dir():
        os.environ.setdefault("TESSDATA_PREFIX", str(_TD))


def _mk_env(tmp: Path, root_names=("A", "B")) -> Config:
    os.environ["LIBRARIAN_DATA"] = str(tmp / "data")
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    (tmp / "data" / "config.toml").write_text(Config.template())
    cfg = Config.load(tmp / "data")
    for n in root_names:
        d = tmp / "libs" / n
        d.mkdir(parents=True, exist_ok=True)
        add_root(cfg, str(d), n)
        cfg = Config.load(tmp / "data")
    return cfg


class _PolicyTests(unittest.TestCase):
    def tearDown(self):
        if _ORIG_LD is None:
            os.environ.pop("LIBRARIAN_DATA", None)
        else:
            os.environ["LIBRARIAN_DATA"] = _ORIG_LD

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cfg = _mk_env(self.dir)

    def prof(self, name="f1", roots=("A",), files=False):
        return Profile(name=name, token_hash="", roots=list(roots), files=files)

    def test_hierarchy(self):
        p = self.prof(files=True)
        # both gates open -> allowed
        self.assertIsNone(download_block(self.cfg, p, "A"))
        # profile text-only -> refused even though folder allows
        self.assertIn("text-only", download_block(self.cfg, self.prof(), "A"))
        # folder master switch -> refuses EVERY profile including files-granted
        set_root_files(self.cfg, "A", False)
        cfg2 = Config.load(self.dir / "data")
        self.assertIn("switched off", download_block(cfg2, p, "A"))
        # ...and a DIFFERENT folder still fine for that same profile
        p_ab = self.prof(roots=("A", "B"), files=True)
        self.assertIsNone(download_block(cfg2, p_ab, "B"))
        # admin passes everything
        self.assertIsNone(download_block(cfg2, None, "A"))
        # invisible folder -> generic not-in-profile (no policy probing)
        self.assertIn("not in your profile", download_block(cfg2, p, "B"))
        # unattributed doc -> invisible -> refused for profiles, ok for admin
        self.assertIn("not in your profile", download_block(cfg2, p, None))

    def test_flags_round_trip(self):
        prof, tok = add_profile(self.cfg, "dl", ["A"])
        self.assertFalse(prof.files)                      # default OFF
        set_profile_files(Config.load(self.dir / "data"), "dl", True)
        self.assertTrue(Config.load(self.dir / "data").profiles[0].files)
        set_root_files(Config.load(self.dir / "data"), "A", False)
        self.assertFalse(Config.load(self.dir / "data").roots[0].files)
        # both flags survive as explicit toml lines; comments elsewhere intact
        text = (self.dir / "data" / "config.toml").read_text()
        self.assertIn("files = false", text)
        self.assertIn("files = true", text)
        self.assertIn("# Librarian config", text)
        # and the file is still valid toml
        Config.load(self.dir / "data")


class _FakeReq:
    def __init__(self, auth=None, query=b""):
        self.headers = {"authorization": auth} if auth else {}
        self.query_string = query


def _scope(path, tok, method="GET"):
    headers = []
    if tok:
        headers = [(b"authorization", f"Bearer {tok}".encode())]
    return {"type": "http", "method": method, "path": path,
            "query_string": b"", "headers": headers, "raw_path": path.encode()}


async def _call(app, scope):
    out = {"status": None, "body": b""}
    state = {"n": 0}

    async def receive():
        # first: deliver an empty request body; after that block forever,
        # like a real server whose socket stays open (starlette's
        # FileResponse listens for http.disconnect on this)
        state["n"] += 1
        if state["n"] == 1:
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    await app(scope, receive, send)
    return out


class _FileRouteTests(unittest.TestCase):
    """The real gate+route wrapper, no uvicorn: scope in, status/bytes out."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cfg = _mk_env(self.dir)
        db = init_db(self.cfg.db_path, 4)
        for i, (root, name) in enumerate((("A", "a.txt"), ("B", "b.txt")), 1):
            p = self.dir / "libs" / root / name
            p.write_text(f"content of {name} for download tests")
            db.execute("INSERT INTO docs(id,path,root,folder,kind,title,ext,size,mtime,chars)"
                       " VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (i, str(p), root, "/", "text", name, "txt", p.stat().st_size, 0, 30))
        db.commit()
        db.close()
        self.resolver = access.Resolver(self.dir / "data")
        self.app = mcp_http._gate(_echo_app, self.resolver, self.dir / "data")
        from librarian.config import set_server_value
        set_server_value(self.cfg, "mcp_token", "big-master")

    def _profile(self, name, roots, files):
        cfg = Config.load(self.dir / "data")
        prof, tok = add_profile(cfg, name, list(roots))
        if files:
            set_profile_files(Config.load(self.dir / "data"), name, True)
        self.resolver = access.Resolver(self.dir / "data")
        self.app = mcp_http._gate(_echo_app, self.resolver, self.dir / "data")
        return tok

    def _get(self, path, tok):
        return asyncio.run(_call(self.app, _scope(path, tok)))

    def test_matrix(self):
        tok_open = self._profile("opendl", ["A", "B"], files=True)
        tok_text = self._profile("textonly", ["A", "B"], files=False)
        set_root_files(Config.load(self.dir / "data"), "B", False)
        self.resolver = access.Resolver(self.dir / "data")
        self.app = mcp_http._gate(_echo_app, self.resolver, self.dir / "data")

        r = self._get("/file/1", "big-master")               # admin: fine
        self.assertEqual(r["status"], 200)
        self.assertIn(b"content of a.txt", r["body"])
        r = self._get("/file/1", tok_open)                   # granted both
        self.assertEqual(r["status"], 200)
        r = self._get("/file/1", tok_text)                   # profile text-only
        self.assertEqual(r["status"], 403)
        self.assertIn(b"text-only", r["body"])
        r = self._get("/file/2", tok_open)                   # folder B master-blocked
        self.assertEqual(r["status"], 403)
        self.assertIn(b"switched off", r["body"])
        r = self._get("/file/2", "big-master")               # admin overrides folder block
        self.assertEqual(r["status"], 200)
        r = self._get("/file/1", "nope")                     # bad credential
        self.assertEqual(r["status"], 401)
        r = self._get("/file/1", "")                         # no credential at all
        self.assertEqual(r["status"], 401)
        r = self._get("/file/999", "big-master")             # unknown id
        self.assertEqual(r["status"], 404)
        # mcp traffic still flows through the same wrapper
        scope = _scope("/mcp", "big-master")
        scope["type"] = "http"
        out = asyncio.run(_call(self.app, scope))
        self.assertEqual(out["body"], b"passthrough")

    def test_no_path_escape(self):
        # a tampered index row pointing outside the roots must 404
        from librarian.db import connect
        db = connect(self.cfg.db_path)
        db.execute("UPDATE docs SET path='/etc/passwd' WHERE id=1")
        db.commit()
        db.close()
        r = self._get("/file/1", "big-master")
        self.assertEqual(r["status"], 404)


async def _echo_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"passthrough"})


class _FileUrlToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cfg = _mk_env(self.dir)
        db = init_db(self.cfg.db_path, 4)
        p = self.dir / "libs" / "A" / "a.txt"
        p.write_text("hello file")
        db.execute("INSERT INTO docs(id,path,root,folder,kind,title,ext,size,mtime)"
                   " VALUES(?,?,?,?,?,?,?,?,?)",
                   (1, str(p), "A", "/", "text", "a.txt", "txt", 10, 0))
        db.commit()
        db.close()
        from librarian import mcp_server
        self.srv = mcp_server
        mcp_server._cfg = self.cfg
        from librarian.db import connect
        mcp_server._tls.db = connect(self.cfg.db_path, read_only=True)
        self._saved_ld = os.environ.get("LIBRARIAN_DATA")
        os.environ["LIBRARIAN_DATA"] = str(self.dir / "data")

    def tearDown(self):
        if self._saved_ld is None:
            os.environ.pop("LIBRARIAN_DATA", None)
        else:
            os.environ["LIBRARIAN_DATA"] = self._saved_ld

    def test_tool_gating(self):
        # admin + no HTTP origin (stdio) -> path note, no url
        t1 = CURRENT.set(None)
        t2 = ORIGIN.set(None)
        try:
            r = json.loads(self.srv.file_url(1))
            self.assertIsNone(r["url"])
            self.assertIn("path_on_server", r)
        finally:
            CURRENT.reset(t1); ORIGIN.reset(t2)
        # profile, text-only -> refused
        t1 = CURRENT.set(Profile(name="f", token_hash="", roots=["A"], files=False))
        t2 = ORIGIN.set("http://box:8770")
        try:
            r = json.loads(self.srv.file_url(1))
            self.assertIn("text-only", r["error"])
        finally:
            CURRENT.reset(t1); ORIGIN.reset(t2)
        # profile + files grant + folder allows -> URL
        t1 = CURRENT.set(Profile(name="f", token_hash="", roots=["A"], files=True))
        t2 = ORIGIN.set("http://box:8770")
        try:
            r = json.loads(self.srv.file_url(1))
            self.assertEqual(r["url"], "http://box:8770/file/1")
            self.assertEqual(r["name"], "a.txt")
        finally:
            CURRENT.reset(t1); ORIGIN.reset(t2)
        # invisible doc -> generic refusal, URL never leaks
        t1 = CURRENT.set(Profile(name="f", token_hash="", roots=[], files=True))
        try:
            r = json.loads(self.srv.file_url(1))
            self.assertIn("not in your profile", r["error"])
        finally:
            CURRENT.reset(t1)

    def test_admin_only_list_unaffected(self):
        # file_url must be DISCOVERABLE by profiles (it self-refuses by policy)
        names = {t.name for t in self.srv.mcp._tool_manager.list_tools()} \
            if hasattr(self.srv.mcp, "_tool_manager") else None
        if names is not None:
            self.assertIn("file_url", names)


@unittest.skipUnless(
    __import__("librarian.extract", fromlist=["x"]).tool_path("tesseract")
    and __import__("librarian.extract", fromlist=["x"]).tool_path("pdftoppm"),
    "needs pdftoppm + tesseract on this host")
class _ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cfg = _mk_env(self.dir, ("Scanlib",))
        self._make_scan()
        self._make_texty()

    def _make_scan(self):
        import subprocess
        from fpdf import FPDF
        d = FPDF(); d.add_page(); d.set_font("Helvetica", size=26)
        d.cell(text="BUNKER SEPTIC calibration foliate")
        d.output("/tmp/exp-src.pdf")
        stem = str(self.dir / "pg")
        subprocess.run(["pdftoppm", "-r", "300", "-png", "/tmp/exp-src.pdf", stem],
                       check=True)
        png = sorted(self.dir.glob("pg*.png"))[0]
        d2 = FPDF(); d2.add_page(); d2.image(str(png), x=10, y=10, w=190)
        self.scan = self.dir / "libs" / "Scanlib" / "scan-only.pdf"
        d2.output(str(self.scan))

    def _make_texty(self):
        from fpdf import FPDF
        d = FPDF(); d.add_page(); d.set_font("Helvetica", size=18)
        d.cell(text="already has a real text layer")
        self.texty = self.dir / "libs" / "Scanlib" / "texty.pdf"
        d.output(str(self.texty))

    def _seed(self):
        db = init_db(self.cfg.db_path, 4)
        for i, (p, chars, kind) in enumerate(
                [(self.scan, 0, "empty"), (self.texty, 120, "pdf")], 1):
            db.execute("INSERT INTO docs(id,path,root,folder,kind,title,ext,size,mtime,chars)"
                       " VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (i, str(p), "Scanlib", "/", kind, p.name, "pdf",
                        p.stat().st_size, 0, chars))
        db.commit()
        db.close()

    def test_selection(self):
        self._seed()
        # a THIRD doc: real scanned file, but DB says chars>0 because the
        # index OCR'd it — only the file check reveals it needs a copy
        import shutil
        ocred = self.dir / "libs" / "Scanlib" / "ocred.pdf"
        shutil.copyfile(self.scan, ocred)
        from librarian.db import connect
        db = connect(self.cfg.db_path)
        db.execute("INSERT INTO docs(id,path,root,folder,kind,title,ext,size,mtime,chars)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (3, str(ocred), "Scanlib", "/", "pdf", "ocred.pdf", "pdf",
                    ocred.stat().st_size, 0, 45))
        db.commit()
        db.close()
        cand, skipped = exportable_docs(self.cfg)
        self.assertEqual(sorted(c["title"] for c in cand), ["ocred.pdf", "scan-only.pdf"])
        self.assertEqual(skipped[0]["why"], "PDF already has a text layer")

    def test_roundtrip(self):
        import hashlib
        self._seed()
        before = hashlib.sha256(self.scan.read_bytes()).hexdigest()
        out_dir = self.dir / "out"
        rep = run_export(self.cfg, out_dir=out_dir, whole_library=True)
        self.assertEqual(rep["exported"], 1, msg=str(rep["failed"]))
        self.assertEqual(rep["skipped_already_text"], 1)
        dst = out_dir / "scan-only.searchable.pdf"
        self.assertTrue(dst.is_file())
        # THE point: the copy carries extractable text; original unchanged
        from pypdf import PdfReader
        words = set(PdfReader(str(dst)).pages[0].extract_text().upper().split())
        for w in ("BUNKER", "SEPTIC", "CALIBRATION"):
            self.assertIn(w, words)
        self.assertEqual(hashlib.sha256(self.scan.read_bytes()).hexdigest(), before)
        # layer is INVISIBLE: text render mode 3 must appear in the content stream
        page = PdfReader(str(dst)).pages[0]
        raw = page.get_contents().get_data().decode("latin-1", "ignore")
        self.assertIn("3 Tr", raw)
        # rerun -> existing kept, no double work
        rep2 = run_export(self.cfg, out_dir=out_dir, whole_library=True)
        self.assertEqual(rep2["exported"], 0)
        self.assertEqual(rep2["skipped_existing"], 1)


if __name__ == "__main__":
    unittest.main()
