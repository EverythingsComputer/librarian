"""Regression (v0.2.5, the Optiplex incident): inside the AppImage every PDF
indexed as an empty stub — bundled poppler was invisible to PATH, the host
poppler was killed by the bundle's LD_LIBRARY_PATH, and the failure was
swallowed. Covers: loader-env isolation for child tools, loud notes when the
extractor is missing/broken, and the retry classification used to self-heal
old indexes."""
import os
import unittest
from pathlib import Path
from unittest import mock

from librarian import extract as X
from librarian.ingest import TOOL_FAILURE_NOTES, is_tool_failure


class ToolEnvTests(unittest.TestCase):
    def test_host_tool_loses_loader_env_when_bundled(self):
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/bundle/usr/lib"}), \
             mock.patch.object(X, "_bundle_bin", lambda: Path("/bundle/usr/bin")):
            env = X.tool_env("/usr/bin/pdftotext")
            self.assertNotIn("LD_LIBRARY_PATH", env)

    def test_bundled_tool_keeps_loader_env(self):
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/bundle/usr/lib"}), \
             mock.patch.object(X, "_bundle_bin", lambda: Path("/bundle/usr/bin")):
            env = X.tool_env("/bundle/usr/bin/pdftotext")
            self.assertEqual(env.get("LD_LIBRARY_PATH"), "/bundle/usr/lib")

    def test_plain_install_env_untouched(self):
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/weird/place"}), \
             mock.patch.object(X, "_bundle_bin", lambda: None):
            self.assertEqual(X.tool_env("/usr/bin/pdftotext").get("LD_LIBRARY_PATH"),
                             "/weird/place")


    def test_short_real_text_is_indexed_not_discarded(self):
        # v0.2.6 regression: <200 chars used to be classified "scanned" and
        # the EXTRACTED text was thrown away — silent data loss on short PDFs
        fake = mock.Mock(returncode=0, stdout="ok then\x0c", stderr="")
        with mock.patch.object(X, "tool_path", lambda n: "/usr/bin/pdftotext"), \
             mock.patch.object(X.subprocess, "run", mock.Mock(return_value=fake)), \
             mock.patch.object(X, "_pdfinfo", lambda p: {"Pages": "1"}):
            ex = X.extract_pdf(Path("/fake.pdf"))
        self.assertEqual([t for _, t in ex.units], ["ok then"])
        self.assertFalse(ex.note.startswith("no text layer"))

    def test_zero_chars_is_the_scanned_case(self):
        fake = mock.Mock(returncode=0, stdout="\x0c\x0c", stderr="")
        with mock.patch.object(X, "tool_path", lambda n: "/usr/bin/pdftotext"), \
             mock.patch.object(X.subprocess, "run", mock.Mock(return_value=fake)), \
             mock.patch.object(X, "_pdfinfo", lambda p: {"Pages": "2"}):
            ex = X.extract_pdf(Path("/fake.pdf"))
        self.assertFalse(ex.units)
        self.assertIn("no text layer", ex.note)
        self.assertFalse(is_tool_failure(ex.note))  # must NOT auto-retry forever


class LoudFailureTests(unittest.TestCase):
    def test_missing_extractor_says_so(self):
        with mock.patch.object(X, "tool_path", lambda n: None):
            ex = X.extract_pdf(Path("/nonexistent.pdf"))
        self.assertIn("pdftotext not found", ex.note)
        self.assertFalse(ex.units)

    def test_broken_extractor_error_is_surfaced(self):
        boom = mock.Mock(returncode=127, stdout="",
                         stderr="libpoppler.so.118: cannot open shared object")
        with mock.patch.object(X, "tool_path", lambda n: "/usr/bin/pdftotext"), \
             mock.patch.object(X, "subprocess", mock.Mock(run=mock.Mock(return_value=boom))):
            ex = X.extract_pdf(Path("/nonexistent.pdf"))
        self.assertIn("pdftotext failed", ex.note)
        self.assertIn("libpoppler", ex.note)

    def test_pdfinfo_absent_is_empty_dict(self):
        with mock.patch.object(X, "tool_path", lambda n: None):
            self.assertEqual(X._pdfinfo(Path("/nonexistent.pdf")), {})


class RetryClassificationTests(unittest.TestCase):
    def test_tool_failures_are_healable(self):
        for note in ("pdftotext not installed (apt install poppler-utils)",
                     "no text extracted",
                     "pdftotext failed (exit 127): oops",
                     "extraction error: RuntimeError",
                     "pdftotext not found — install poppler-utils"):
            self.assertTrue(is_tool_failure(note), note)

    def test_true_scans_are_not_healed_forever(self):
        self.assertFalse(is_tool_failure(
            "no text layer (12 pages) — scanned? enable [index] ocr=true or OCR the file"))
        self.assertFalse(is_tool_failure(""))
        self.assertFalse(is_tool_failure(None))

    def test_marks_stay_in_sync_with_extract_notes(self):
        # if extract.py changes a failure note, the retry list must change too
        with mock.patch.object(X, "tool_path", lambda n: None):
            note = X.extract_pdf(Path("/nonexistent.pdf")).note
        self.assertTrue(is_tool_failure(note), note)


if __name__ == "__main__":
    unittest.main()
