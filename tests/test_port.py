"""Regression: launching the UI on a port that is already taken must not
crash with a raw OSError traceback (v0.2.0 OptiPlex incident)."""
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from librarian.ui import _bind


def busy_port():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):  # noqa: A002
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1], srv


class PortTests(unittest.TestCase):
    def _close(self, srv):
        # NB: never shutdown() a server whose serve_forever loop isn't running
        # — it blocks forever. server_close() is enough here.
        srv.server_close()

    def test_default_launch_slides_to_free_port(self):
        port, blocker = busy_port()
        try:
            srv, got = _bind(Path("."), port, explicit=False)
            self.assertNotEqual(got, port)
            self.assertLess(got, port + 26)
            self._close(srv)
        finally:
            blocker.shutdown()
            self._close(blocker)

    def test_explicit_port_reports_plain_error(self):
        port, blocker = busy_port()
        try:
            with self.assertRaises(SystemExit) as cm:
                _bind(Path("."), port, explicit=True)
            msg = str(cm.exception)
            self.assertIn("already in use", msg)
            self.assertNotIn("Traceback", msg)
        finally:
            blocker.shutdown()
            self._close(blocker)


if __name__ == "__main__":
    unittest.main()
