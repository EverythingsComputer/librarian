"""Regression (v0.2.9, a 2026-11-22 production prune incident): an agent called
update_index while the USB root was transiently unreadable; the scan yielded
zero files, prune treated 2,501 existing rows as 'deleted', and the sync ran
detached from the cancelled request, deleting past server shutdown.
Guards proven here: unreadable-root protection, mass-prune BLOCK, prune runs
only after indexing (a cancelled build never prunes), cancellation stops the
prune loop, OOXML package internals skipped + purged, mojibake PDF titles
repaired, tool_path finds bundled helpers without AppRun's PATH."""
import os
import sys
import threading
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from librarian.config import Config, LibraryRoot
from librarian.db import connect, init_db
from librarian.extract import fix_mojibake, tool_path
from librarian.ingest import _ooxml_internals, build_or_update
from librarian.search import coverage


def _cfg(dd):
    return Config(data_dir=dd / "data", db_path=dd / "data" / "library.db",
                  roots=[LibraryRoot(path=dd / "lib", name="lib")],
                  embed_backend="openai", embed_model="m",
                  embed_base_url="http://stub")  # dims come from stubbed embed_one


class FakeEmbedder:
    dims = 4

    def __init__(self, *a, **k):
        pass

    def _fast(self):
        self.dims = 4

    def embed_one(self, text):
        return [0.1, 0.2, 0.3, 0.4]

    def embed_batch(self, texts, batch_size: int = 64):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    def embed(self, texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


def _make_txt(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _seed_db(cfg, paths):
    """Rows for files that were indexed before. size/mtime match disk where the
    file exists, so sync sees them unchanged unless the test wants otherwise."""
    db = init_db(cfg.db_path, 4)
    for i, sp in enumerate(paths):
        try:
            st = Path(sp).stat()
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 10, 0
        db.execute("INSERT INTO docs(id,path,root,folder,kind,title,ext,size,mtime)"
                   " VALUES(?,?,?,?,?,?,?,?,?)",
                   (i + 1, sp, "lib", "/", "text", Path(sp).stem, "txt", size, mtime))
        db.execute("INSERT INTO chunks(id,doc_id,label,text) VALUES(?,?,?,?)",
                   (i + 1, i + 1, "p.1", f"body of {Path(sp).stem}"))
    db.commit()
    db.close()


class PruneSafetyTests(unittest.TestCase):
    def setUp(self):
        self._saved = sys.modules.get("librarian.embed")
        fake = types.ModuleType("librarian.embed")
        setattr(fake, "Embedder", FakeEmbedder)
        sys.modules["librarian.embed"] = fake
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dd = Path(self.tmp.name)
        self.cfg = _cfg(self.dd)
        self.paths = [str(self.dd / "lib" / f"d{i}.txt") for i in range(200)]

    def tearDown(self):
        if self._saved is not None:
            sys.modules["librarian.embed"] = self._saved

    def _count(self):
        db = connect(self.cfg.db_path, read_only=True)
        n = db.execute("SELECT COUNT(*) c FROM docs").fetchone()["c"]
        db.close()
        return n

    def test_unreadable_root_prunes_nothing(self):
        # root exists but is EMPTY (offline mount case): zero files scanned
        (self.dd / "lib").mkdir()
        _seed_db(self.cfg, self.paths)
        rep = {}
        build_or_update(self.cfg, report=rep)
        self.assertEqual(self._count(), 200, "unreadable root must not prune")
        self.assertEqual(rep["removed"], 0)
        self.assertFalse(rep.get("cancelled"))

    def test_missing_root_dir_prunes_nothing(self):
        # root directory itself gone
        _seed_db(self.cfg, self.paths)
        rep = {}
        build_or_update(self.cfg, report=rep)
        self.assertEqual(self._count(), 200)

    def test_normal_delete_still_prunes(self):
        # 199 files on disk, 1 row gone for real -> that one row goes
        for sp in self.paths[:-1]:
            _make_txt(Path(sp), "content " + Path(sp).stem)
        _seed_db(self.cfg, self.paths)
        rep = {}
        build_or_update(self.cfg, report=rep)
        self.assertEqual(rep["removed"], 1)
        self.assertEqual(self._count(), 199)

    def test_mass_disappearance_is_blocked_not_executed(self):
        # all 200 files really missing but root yields >0 elsewhere: force the
        # BLOCK path by putting one unrelated file in the root so it scans OK,
        # while 200 old rows vanish at once (> max(100, 25%))
        _make_txt(self.dd / "lib" / "newcomer.txt", "hello new file")
        _seed_db(self.cfg, self.paths)
        rep = {}
        build_or_update(self.cfg, report=rep)
        self.assertEqual(rep["prune_blocked"], 200)
        self.assertEqual(rep["removed"], 0)
        self.assertGreaterEqual(self._count(), 200)   # 200 old + newcomer

    def test_cancel_skips_prune_entirely(self):
        for sp in self.paths[:-1]:
            _make_txt(Path(sp), "content")
        # newcomer missing too => 'gone' would include newcomer row; cancel first
        _seed_db(self.cfg, self.paths + [str(self.dd / "lib" / "z.txt")])
        cancel = threading.Event()
        cancel.set()
        rep = {}
        build_or_update(self.cfg, report=rep, cancel=cancel)
        self.assertTrue(rep["cancelled"])
        self.assertEqual(rep["removed"], 0)
        self.assertEqual(self._count(), 201)

    def test_ooxml_internals_skipped_and_purged(self):
        root = self.dd / "lib"
        _make_txt(root / "real.txt", "a real document about things")
        junk = root / "unzipped_docx"
        for rel in ("[Content_Types].xml", "word/document.xml", "docProps/app.xml"):
            _make_txt(junk / rel, "<?xml version='1.0'?><a:theme/>")
        _seed_db(self.cfg, [str(root / "real.txt"), str(junk / "word" / "document.xml")])
        rep = {}
        build_or_update(self.cfg, report=rep)
        db = connect(self.cfg.db_path, read_only=True)
        titles = [r["title"] for r in db.execute("SELECT title FROM docs")]
        db.close()
        self.assertTrue(all("document" not in t and "[Content" not in t for t in titles),
                        f"OOXML internals still indexed: {titles}")

    def test_mass_prune_env_override(self):
        _make_txt(self.dd / "lib" / "newcomer.txt", "hello")
        _seed_db(self.cfg, self.paths)
        os.environ["LIBRARIAN_ALLOW_MASS_PRUNE"] = "1"
        self.addCleanup(os.environ.pop, "LIBRARIAN_ALLOW_MASS_PRUNE", None)
        rep = {}
        build_or_update(self.cfg, report=rep)
        self.assertEqual(rep["prune_blocked"], 0)
        self.assertEqual(rep["removed"], 200)

    def test_mojibake_titles_repaired_in_place(self):
        correct = "اللغة العربية"
        broken = correct.encode("utf-8").decode("cp1252")
        p = self.dd / "lib" / "ar.txt"
        _make_txt(p, "نص عربي عن الحوكمة")   # file present: no re-extract needed
        _seed_db(self.cfg, [str(p)])
        db = connect(self.cfg.db_path)
        db.execute("UPDATE docs SET title=? WHERE id=1", (broken,))
        db.execute("INSERT INTO chunks_fts(rowid,title,text) VALUES(1,?,?)",
                   (broken, "نص عربي"))
        db.commit()
        db.close()
        build_or_update(self.cfg, report={})
        db = connect(self.cfg.db_path, read_only=True)
        title = db.execute("SELECT title FROM docs WHERE id=1").fetchone()["title"]
        fts_title = db.execute("SELECT title FROM chunks_fts WHERE rowid=1").fetchone()["title"]
        db.close()
        self.assertEqual(title, correct)
        self.assertEqual(fts_title, correct)

    def test_progress_reports_current_file(self):
        import librarian.ingest as I
        p = self.dd / "lib" / "slow.txt"
        _make_txt(p, "hello world, some real content here")
        seen = {}
        orig = I._ingest_one

        def spy(db, cfg, probe, llm, path, *a, **k):
            p = I.progress_snapshot()
            seen["file"] = p["file"]
            seen["active"] = p["active"]
            seen["pos"] = (p["done"], p["total"])
            return orig(db, cfg, probe, llm, path, *a, **k)
        I._ingest_one = spy
        try:
            build_or_update(self.cfg, report={})
        finally:
            I._ingest_one = orig
        self.assertEqual(seen["file"], "slow.txt")   # visible WHILE working on it
        self.assertTrue(seen["active"])
        self.assertEqual(seen["pos"], (1, 1))
        self.assertFalse(I.progress_snapshot()["active"])   # cleared when done


class MojibakeTests(unittest.TestCase):
    def test_arabic_double_decode(self):
        correct = "اللله"                       # real Arabic
        broken = correct.encode("utf-8").decode("cp1252")  # the PDF /Title bug
        self.assertNotEqual(broken, correct)
        self.assertEqual(fix_mojibake(broken), correct)

    def test_plain_ascii_and_true_latin_pass_through(self):
        self.assertEqual(fix_mojibake("Nuclear Doctrine 1962"), "Nuclear Doctrine 1962")
        self.assertEqual(fix_mojibake("Café résumé"), "Café résumé")  # not valid utf-8 round-trip

    def test_double_encoded_converges_in_one_call(self):
        correct = "Mapping Bacillus Calmette–Guérin vaccine"
        double = correct.encode("utf-8").decode("cp1252").encode("cp1252").decode("cp1252")
        self.assertNotEqual(double, correct)
        self.assertEqual(fix_mojibake(double), correct)
        # idempotent on already-clean text
        self.assertEqual(fix_mojibake(correct), correct)


class BundleToolPathTests(unittest.TestCase):
    def test_sibling_of_executable_found_without_path(self):
        with TemporaryDirectory() as td:
            bd = Path(td) / "usr" / "bin"
            bd.mkdir(parents=True)
            (bd / "tesseract").write_text("#!/bin/sh\n")
            (bd / "tesseract").chmod(0o755)
            saved_exe = sys.executable
            saved_frozen = getattr(sys, "frozen", None)
            saved_env = os.environ.get("APPIMAGE")
            sys.executable = str(bd / "librarian")
            setattr(sys, "frozen", True)
            os.environ.pop("APPIMAGE", None)
            saved_path = os.environ["PATH"]
            os.environ["PATH"] = "/nonexistent"
            try:
                self.assertEqual(tool_path("tesseract"), str(bd / "tesseract"))
                self.assertIsNone(tool_path("definitely-not-here"))
            finally:
                sys.executable, os.environ["PATH"] = saved_exe, saved_path
                if saved_env is not None:
                    os.environ["APPIMAGE"] = saved_env
                if saved_frozen is None:
                    del sys.frozen
                else:
                    setattr(sys, "frozen", saved_frozen)


class CoverageNoEmbedderTests(unittest.TestCase):
    def test_coverage_runs_lexically(self):
        # coverage must not touch the embedder at all now (C1: 300s timeouts)
        from librarian.db import init_db
        with TemporaryDirectory() as td:
            db = init_db(Path(td) / "t.db", 4)
            db.execute("INSERT INTO docs(id,path,root,folder,kind,title,ext,size,mtime)"
                       " VALUES(1,'/a.txt','r','/','text','A','txt',5,0)")
            db.execute("INSERT INTO chunks(id,doc_id,label,text) VALUES(1,1,'p.1',"
                       "'the dirty bomb plot was stopped')")
            db.execute("INSERT INTO chunks_fts(rowid,title,text) VALUES(1,'A',"
                       "'the dirty bomb plot was stopped')")
            db.commit()
            res = coverage(db, None, "dirty bomb plot", k_docs=5)
            self.assertEqual(res["docs_matching"], 1)


if __name__ == "__main__":
    unittest.main()
