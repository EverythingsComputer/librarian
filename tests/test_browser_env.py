"""Regression (v0.2.1 OptiPlex): auto-opening the browser must not hand the
AppImage's LD_LIBRARY_PATH to desktop helpers — they then load the bundled
libstdc++ and die (kde-open GLIBCXX_3.4.32 errors), so the page never shows."""
import os
import unittest
from unittest import mock

import librarian.ui as ui


class BrowserEnvTests(unittest.TestCase):
    def test_loader_vars_stripped_from_opener(self):
        with mock.patch.dict(os.environ,
                             {"LD_LIBRARY_PATH": "/tmp/.mount_x/usr/bin/_internal",
                              "LD_PRELOAD": "evil.so",
                              "KEEP_ME": "yes"}), \
             mock.patch("subprocess.Popen") as popen, \
             mock.patch("shutil.which", return_value="/usr/bin/xdg-open"):
            ui._open_browser("http://127.0.0.1:1/")
        self.assertTrue(popen.called)
        env = popen.call_args.kwargs["env"]
        self.assertNotIn("LD_LIBRARY_PATH", env)
        self.assertNotIn("LD_PRELOAD", env)
        self.assertEqual(env.get("KEEP_ME"), "yes")
        self.assertIn("http://127.0.0.1:1/", popen.call_args.args[0])

    def test_falls_back_to_webbrowser_when_no_opener(self):
        with mock.patch("subprocess.Popen", side_effect=OSError("nope")), \
             mock.patch("shutil.which", return_value=None), \
             mock.patch.object(ui.webbrowser, "open") as wb:
            ui._open_browser("http://x/")
        wb.assert_called_once_with("http://x/")


if __name__ == "__main__":
    unittest.main()
