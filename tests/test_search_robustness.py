"""Regression (v0.2.8, the Unsloth agent-log incident): search() crashed with
'sqlite3.OperationalError: no such column' on every hyphenated model number
(MMP-15, LT2-F, Mini-CALIBER, Table 3-1), and read_doc's label_contains was
appended AFTER ORDER BY so page-targeted reads silently returned page 1 —
the agent burned 30+ calls fighting both."""
import sqlite3
import unittest

from librarian.search import (_fq, _lexical_like, fts_and_query, fts_query,
                              find_term, read_doc)

CRASHERS = [
    "CALIBER T5 Mini-CALIBER weight payload price specifications",
    "MMP-15 Tactical Robot Andros SR1 Dragon Runner 20",
    "SPUR Squad Packable Utility Robot REV9-18 MTGR",
    "LT2-F Bulldog LT2 Bloodhound HD2-S Doberman Mastiff",
    "Table 3-1 general characteristics specifications products price",
    'the "Jaguar V4" arm\'s payload: $2,950 — 100%',
    "p.24 AND (caliber OR bot:1) NEAR weight",
]


class FtsSafetyTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE VIRTUAL TABLE t USING fts5(title, text, tokenize='porter unicode61')")
        self.db.execute("INSERT INTO t VALUES('robot',"
                        "'the mmp-15 mini-caliber lt2-f bulldog dragon runner 20 table 3-1 weight')")

    def test_agent_log_queries_never_raise(self):
        for q in CRASHERS:
            for builder in (fts_query, fts_and_query):
                built = builder(q)
                try:
                    self.db.execute("SELECT * FROM t WHERE t MATCH ?", (built,)).fetchall()
                except sqlite3.OperationalError as e:
                    self.fail(f"fts_query crash reproduced for {q!r}: {built!r} -> {e}")

    def test_hyphen_token_matches_the_document(self):
        # 'MMP-15' quoted as a phrase still FINDS 'mmp-15' in text
        rows = self.db.execute("SELECT * FROM t WHERE t MATCH ?",
                               (fts_query("MMP-15"),)).fetchall()
        self.assertEqual(len(rows), 1)

    def test_quotes_are_escaped_not_injectable(self):
        built = fts_query('a "weird ""nested"" token" b')
        self.db.execute("SELECT * FROM t WHERE t MATCH ?", (built,))  # must not raise

    def test_fallback_scan_finds_text(self):
        db = sqlite3.connect(":memory:")
        db.executescript(
            "CREATE TABLE docs(id INTEGER PRIMARY KEY, dup_of, root, folder, path, kind, title);"
            "CREATE TABLE chunks(id INTEGER PRIMARY KEY, doc_id, label, text);"
            "INSERT INTO docs VALUES(7, NULL, 'lib', '/', '/x.pdf', 'pdf', 'M');"
            "INSERT INTO chunks VALUES(1, 7, 'p.1', 'the LT2-F bulldog chassis');")
        rows = _lexical_like(db, _fq("lt2-f") + " OR " + _fq("bulldog"), 5, None, None)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["doc_id"], 7)


class ReadDocLabelTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            "CREATE TABLE docs(id INTEGER PRIMARY KEY, title, path, folder, kind, pages, summary, note);"
            "CREATE TABLE chunks(id INTEGER PRIMARY KEY, doc_id, ordinal, label, text);"
            "INSERT INTO docs VALUES(1,'T','/t.pdf','/','pdf',3,'s','');"
            "INSERT INTO chunks VALUES(1,1,0,'p.1','first page');"
            "INSERT INTO chunks VALUES(2,1,1,'p.2 (cont 1)','second a');"
            "INSERT INTO chunks VALUES(3,1,2,'p.2 (cont 2)','second b');"
            "INSERT INTO chunks VALUES(4,1,3,'p.3','third page');")

    def test_label_filter_actually_filters(self):
        r = read_doc(self.db, 1, label_contains="p.2")
        self.assertIn("second a", r["text"])
        self.assertIn("second b", r["text"])
        self.assertNotIn("first page", r["text"])   # THE bug: this leaked page 1
        self.assertNotIn("third page", r["text"])

    def test_no_match_returns_labels_instead_of_page_one(self):
        r = read_doc(self.db, 1, label_contains="Jaguar V4")
        self.assertIn("error", r)
        self.assertIn("labels_sample", r)
        self.assertEqual(r["labels_sample"][0], "p.1")

    def test_truncation_names_the_next_label(self):
        r = read_doc(self.db, 1, max_chars=40)
        self.assertIn("[p.1]", r["text"])          # first section shown whole
        self.assertIn("label_contains='p.2'", r["text"])  # next one named for paging

    def test_page_request_never_returns_empty(self):
        # single big page requested with tiny max_chars must still show content
        self.db.execute("UPDATE chunks SET text=? WHERE id=4", ("x" * 5000,))
        r = read_doc(self.db, 1, label_contains="p.3", max_chars=500)
        self.assertIn("xxxxx", r["text"])
        self.assertIn("raise max_chars", r["text"])
        self.assertNotIn("[p.1]", r["text"])   # still filtered to p.3


if __name__ == "__main__":
    unittest.main()
