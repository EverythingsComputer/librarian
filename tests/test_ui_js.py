"""The whole inline <script> dies if a Python-escaped real newline lands
inside a JS single/double-quoted string (py_compile cannot see this).
Run: python -m unittest tests.test_ui_js  (add node for the parser check)."""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from librarian.ui import PAGE


def inline_js() -> str:
    return re.search(r"<script>(.*)</script>", PAGE, re.S).group(1)


class UiJsTests(unittest.TestCase):
    @unittest.skipIf(shutil.which("node") is None, "node not installed")
    def test_inline_js_parses(self):
        fd, path = tempfile.mkstemp(suffix=".js")
        try:
            with open(fd, "w") as f:
                f.write(inline_js())
            r = subprocess.run(["node", "--check", path],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr[:800])
        finally:
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
