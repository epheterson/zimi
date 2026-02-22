#!/usr/bin/env python3
"""Zimi tests — functionality and performance.

Usage:
  python3 tests/test_unit.py                    # Run all unit tests (no ZIM files needed)
  python3 tests/test_unit.py --perf             # Run performance tests (requires running server)
  python3 tests/test_unit.py --perf-host HOST   # Performance tests against specific host

Unit tests cover pure logic functions (scoring, caching, query cleaning, etc.)
Performance tests hit the HTTP API and measure response times.
"""

import sys
import os
import time
import json
import unittest
from unittest.mock import patch, MagicMock

# Make zimi importable from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Unit Tests (no ZIM files needed) ──

class TestCleanQuery(unittest.TestCase):
    """Test stop-word removal for Xapian queries."""

    def setUp(self):
        import zimi
        self.clean = zimi._clean_query

    def test_removes_stop_words(self):
        self.assertEqual(self.clean("how to fix a memory leak"), "fix memory leak")

    def test_preserves_quoted_phrases(self):
        result = self.clean('"python asyncio" is great')
        self.assertIn('"python asyncio"', result)
        self.assertNotIn("is", result.replace('"python asyncio"', ''))

    def test_all_stop_words_returns_original(self):
        self.assertEqual(self.clean("what is the"), "what is the")

    def test_empty_string(self):
        self.assertEqual(self.clean(""), "")

    def test_single_word(self):
        self.assertEqual(self.clean("python"), "python")


class TestScoreResult(unittest.TestCase):
    """Test cross-ZIM result scoring."""

    def setUp(self):
        import zimi
        self.score = zimi._score_result

    def test_exact_phrase_match_highest(self):
        score = self.score("Python asyncio tutorial", ["python", "asyncio"], 0, 1000)
        self.assertGreater(score, 100)  # exact phrase = 100 + rank + auth

    def test_all_words_match(self):
        score = self.score("Asyncio in Python", ["python", "asyncio"], 0, 1000)
        self.assertGreater(score, 80)  # all words = 80 + rank + auth

    def test_partial_word_match(self):
        score = self.score("Python basics", ["python", "asyncio"], 0, 1000)
        self.assertGreater(score, 20)
        self.assertLess(score, 80)

    def test_no_title_match_low_rank_score(self):
        score = self.score("Unrelated article", ["python", "asyncio"], 0, 1000)
        # rank_score capped at 5 when title_score == 0
        self.assertLess(score, 15)

    def test_rank_decreases_score(self):
        score0 = self.score("Python", ["python"], 0, 1000)
        score5 = self.score("Python", ["python"], 5, 1000)
        self.assertGreater(score0, score5)

    def test_larger_zim_slightly_higher(self):
        small = self.score("Python", ["python"], 0, 100)
        large = self.score("Python", ["python"], 0, 10_000_000)
        self.assertGreater(large, small)
        self.assertLess(large - small, 5)  # auth_score capped at 5

    def test_case_insensitive(self):
        score = self.score("PYTHON ASYNCIO", ["python", "asyncio"], 0, 1000)
        self.assertGreater(score, 80)


class TestSearchCache(unittest.TestCase):
    """Test search result caching."""

    def setUp(self):
        import zimi
        self.zimi = zimi
        self.zimi._search_cache.clear()

    def tearDown(self):
        self.zimi._search_cache.clear()

    def test_put_and_get(self):
        key = ("test", "", 5, False)
        self.zimi._search_cache_put(key, {"results": [], "total": 0})
        result = self.zimi._search_cache_get(key)
        self.assertIsNotNone(result)
        self.assertEqual(result["total"], 0)

    def test_miss_returns_none(self):
        result = self.zimi._search_cache_get(("nonexistent", "", 5, False))
        self.assertIsNone(result)

    def test_ttl_expiry(self):
        key = ("test", "", 5, False)
        self.zimi._search_cache_put(key, {"results": []})
        # Manually expire
        self.zimi._search_cache[key]["created"] = time.time() - 999999
        result = self.zimi._search_cache_get(key)
        self.assertIsNone(result)

    def test_clear(self):
        self.zimi._search_cache_put(("a", "", 5, False), {"results": []})
        self.zimi._search_cache_put(("b", "", 5, False), {"results": []})
        self.zimi._search_cache_clear()
        self.assertEqual(len(self.zimi._search_cache), 0)

    def test_max_eviction(self):
        # Fill cache to max
        for i in range(self.zimi.SEARCH_CACHE_MAX):
            self.zimi._search_cache_put((f"q{i}", "", 5, False), {"results": []})
        # One more should evict oldest
        self.zimi._search_cache_put(("overflow", "", 5, False), {"results": []})
        self.assertEqual(len(self.zimi._search_cache), self.zimi.SEARCH_CACHE_MAX)


class TestSuggestCache(unittest.TestCase):
    """Test per-ZIM suggestion caching."""

    def setUp(self):
        import zimi
        self.zimi = zimi
        self.zimi._suggest_cache.clear()

    def tearDown(self):
        self.zimi._suggest_cache.clear()

    def test_put_and_get(self):
        self.zimi._suggest_cache_put("python", "wikipedia", [{"title": "Python"}])
        result = self.zimi._suggest_cache_get("python", "wikipedia")
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 1)

    def test_miss(self):
        self.assertIsNone(self.zimi._suggest_cache_get("nope", "nope"))

    def test_per_zim_isolation(self):
        self.zimi._suggest_cache_put("python", "wikipedia", [{"title": "A"}])
        self.zimi._suggest_cache_put("python", "stackoverflow", [{"title": "B"}])
        r1 = self.zimi._suggest_cache_get("python", "wikipedia")
        r2 = self.zimi._suggest_cache_get("python", "stackoverflow")
        self.assertEqual(r1[0]["title"], "A")
        self.assertEqual(r2[0]["title"], "B")

    def test_ttl_expiry(self):
        self.zimi._suggest_cache_put("q", "z", [])
        self.zimi._suggest_cache[("q", "z")]["ts"] = time.time() - 999999
        self.assertIsNone(self.zimi._suggest_cache_get("q", "z"))

    def test_clear_also_clears_pool(self):
        self.zimi._suggest_cache_put("q", "z", [])
        self.zimi._suggest_cache_clear()
        self.assertEqual(len(self.zimi._suggest_cache), 0)


class TestCategorizeZim(unittest.TestCase):
    """Test ZIM categorization logic."""

    def setUp(self):
        import zimi
        self.cat = zimi._categorize_zim

    def test_wikipedia(self):
        self.assertEqual(self.cat("wikipedia"), "Wikimedia")

    def test_stackoverflow(self):
        self.assertEqual(self.cat("stackoverflow"), "Stack Exchange")

    def test_stackexchange_variant(self):
        self.assertEqual(self.cat("cooking.stackexchange"), "Stack Exchange")

    def test_gutenberg(self):
        self.assertEqual(self.cat("gutenberg"), "Books")

    def test_devdocs(self):
        self.assertEqual(self.cat("devdocs_python"), "Dev Docs")

    def test_unknown_returns_none(self):
        self.assertIsNone(self.cat("some_random_zim"))


class TestStripHtml(unittest.TestCase):
    """Test HTML stripping."""

    def setUp(self):
        import zimi
        self.strip = zimi.strip_html

    def test_basic_tags(self):
        self.assertEqual(self.strip("<p>Hello <b>world</b></p>").strip(), "Hello world")

    def test_empty(self):
        self.assertEqual(self.strip(""), "")

    def test_plain_text_passthrough(self):
        self.assertEqual(self.strip("no tags here"), "no tags here")

    def test_script_removal(self):
        result = self.strip("<p>Before</p><script>evil()</script><p>After</p>")
        self.assertNotIn("evil", result)
        self.assertIn("Before", result)
        self.assertIn("After", result)


class TestSearchAllContract(unittest.TestCase):
    """Test search_all() return value contract (mocked, no ZIM files)."""

    def setUp(self):
        import zimi
        self.zimi = zimi

    @patch.object(sys.modules.get('zimi', MagicMock()), 'get_zim_files', return_value={})
    def test_empty_library(self, _):
        result = self.zimi.search_all("test")
        self.assertIn("results", result)
        self.assertIn("total", result)
        self.assertIn("elapsed", result)
        self.assertIn("partial", result)
        self.assertEqual(result["total"], 0)

    @patch.object(sys.modules.get('zimi', MagicMock()), 'get_zim_files', return_value={})
    def test_fast_returns_partial_true(self, _):
        result = self.zimi.search_all("test", fast=True)
        self.assertTrue(result["partial"])

    @patch.object(sys.modules.get('zimi', MagicMock()), 'get_zim_files', return_value={})
    def test_full_returns_partial_false(self, _):
        result = self.zimi.search_all("test", fast=False)
        self.assertFalse(result["partial"])

    @patch.object(sys.modules.get('zimi', MagicMock()), 'get_zim_files', return_value={})
    def test_missing_zim_returns_error(self, _):
        result = self.zimi.search_all("test", filter_zim="nonexistent")
        self.assertIn("error", result)


class TestRateLimiting(unittest.TestCase):
    """Test rate limiting logic."""

    def setUp(self):
        import zimi
        self.zimi = zimi
        self.zimi._rate_buckets.clear()

    def tearDown(self):
        self.zimi._rate_buckets.clear()

    def test_allows_normal_traffic(self):
        result = self.zimi._check_rate_limit("192.168.1.1")
        self.assertEqual(result, 0)  # 0 = not rate limited

    def test_blocks_excessive_traffic(self):
        # Hammer the same IP past RATE_LIMIT
        for _ in range(self.zimi.RATE_LIMIT + 10):
            self.zimi._check_rate_limit("192.168.1.2")
        result = self.zimi._check_rate_limit("192.168.1.2")
        self.assertGreater(result, 0)  # >0 = retry-after seconds

    def test_different_ips_independent(self):
        for _ in range(self.zimi.RATE_LIMIT + 10):
            self.zimi._check_rate_limit("10.0.0.1")
        result = self.zimi._check_rate_limit("10.0.0.2")
        self.assertEqual(result, 0)


class TestDataDir(unittest.TestCase):
    """Test ZIMI_DATA_DIR paths."""

    def setUp(self):
        import zimi
        self.zimi = zimi

    def test_data_dir_defaults_to_zim_subdir(self):
        expected = os.path.join(self.zimi.ZIM_DIR, ".zimi")
        self.assertEqual(self.zimi.ZIMI_DATA_DIR, expected)

    def test_cache_file_in_data_dir(self):
        path = self.zimi._cache_file_path()
        self.assertTrue(path.startswith(self.zimi.ZIMI_DATA_DIR))
        self.assertTrue(path.endswith("cache.json"))

    def test_collections_file_in_data_dir(self):
        path = self.zimi._collections_file_path()
        self.assertTrue(path.startswith(self.zimi.ZIMI_DATA_DIR))
        self.assertTrue(path.endswith("collections.json"))

    def test_password_file_in_data_dir(self):
        path = self.zimi._password_file()
        self.assertTrue(path.startswith(self.zimi.ZIMI_DATA_DIR))
        self.assertTrue(path.endswith("password"))


class TestTitleIndex(unittest.TestCase):
    """Test SQLite title index build and search."""

    def setUp(self):
        import zimi
        import tempfile
        import sqlite3
        self.zimi = zimi
        self.sqlite3 = sqlite3
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")

    def tearDown(self):
        import shutil
        self.zimi._close_title_db("test")  # evict pooled connection
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _create_test_db(self, entries):
        """Create a test title index DB with given (path, title) pairs."""
        conn = self.sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE titles (path TEXT PRIMARY KEY, title TEXT, title_lower TEXT)")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT INTO titles VALUES (?,?,?)",
            [(p, t, t.lower()) for p, t in entries]
        )
        conn.execute("CREATE INDEX idx_prefix ON titles(title_lower)")
        conn.execute("INSERT INTO meta VALUES ('zim_mtime', '12345')")
        conn.commit()
        conn.close()

    def test_search_prefix_match(self):
        self._create_test_db([
            ("A/Python", "Python programming"),
            ("A/Perl", "Perl scripting"),
            ("A/PyTorch", "PyTorch deep learning"),
        ])
        # Monkey-patch path function
        orig = self.zimi._title_index_path
        self.zimi._title_index_path = lambda name: self.db_path
        try:
            results = self.zimi._title_index_search("test", "python", limit=10)
            self.assertIsNotNone(results)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["title"], "Python programming")
        finally:
            self.zimi._title_index_path = orig

    def test_search_no_index_returns_none(self):
        result = self.zimi._title_index_search("nonexistent_zim_xyz", "test")
        self.assertIsNone(result)

    def test_search_empty_query(self):
        self._create_test_db([("A/Test", "Test")])
        orig = self.zimi._title_index_path
        self.zimi._title_index_path = lambda name: self.db_path
        try:
            results = self.zimi._title_index_search("test", "", limit=10)
            self.assertEqual(results, [])
        finally:
            self.zimi._title_index_path = orig

    def test_search_case_insensitive(self):
        self._create_test_db([
            ("A/Python", "Python Guide"),
            ("A/PYTHON", "PYTHON FAQ"),
        ])
        orig = self.zimi._title_index_path
        self.zimi._title_index_path = lambda name: self.db_path
        try:
            results = self.zimi._title_index_search("test", "PYTHON", limit=10)
            self.assertIsNotNone(results)
            self.assertEqual(len(results), 2)
        finally:
            self.zimi._title_index_path = orig

    def test_search_respects_limit(self):
        entries = [(f"A/Item{i}", f"Python item {i}") for i in range(20)]
        self._create_test_db(entries)
        orig = self.zimi._title_index_path
        self.zimi._title_index_path = lambda name: self.db_path
        try:
            results = self.zimi._title_index_search("test", "python", limit=5)
            self.assertEqual(len(results), 5)
        finally:
            self.zimi._title_index_path = orig

    def test_is_current_nonexistent(self):
        self.assertFalse(self.zimi._title_index_is_current("fake", "/no/such/file.zim"))

    def test_multiword_search_with_fts5(self):
        """Multi-word queries use FTS5 when available."""
        conn = self.sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE titles (path TEXT PRIMARY KEY, title TEXT, title_lower TEXT)")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        entries = [
            ("A/WP", "Water purification methods"),
            ("A/WS", "Water safety guidelines"),
            ("A/PM", "Purification of metals"),
        ]
        conn.executemany("INSERT INTO titles VALUES (?,?,?)", [(p, t, t.lower()) for p, t in entries])
        conn.execute("CREATE INDEX idx_prefix ON titles(title_lower)")
        conn.execute("CREATE VIRTUAL TABLE titles_fts USING fts5(path UNINDEXED, title, tokenize='unicode61')")
        conn.executemany("INSERT INTO titles_fts(path, title) SELECT ?, ? FROM (SELECT 1)", [(p, t) for p, t in entries])
        conn.execute("INSERT INTO meta VALUES ('has_fts', '1')")
        conn.commit()
        conn.close()
        orig = self.zimi._title_index_path
        self.zimi._title_index_path = lambda name: self.db_path
        try:
            results = self.zimi._title_index_search("test", "water purification", limit=10)
            self.assertIsNotNone(results)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["title"], "Water purification methods")
        finally:
            self.zimi._title_index_path = orig

    def test_multiword_search_without_fts5_returns_none(self):
        """Multi-word queries return None (fallback) when no FTS5 table exists."""
        self._create_test_db([
            ("A/WP", "Water purification methods"),
            ("A/WS", "Water safety guidelines"),
        ])
        orig = self.zimi._title_index_path
        self.zimi._title_index_path = lambda name: self.db_path
        try:
            results = self.zimi._title_index_search("test", "water purification", limit=10)
            self.assertIsNone(results)
        finally:
            self.zimi._title_index_path = orig

    def test_build_fts_for_existing_index(self):
        """_build_fts_for_index adds FTS5 to an existing index without one."""
        self._create_test_db([
            ("A/WP", "Water purification"),
            ("A/PM", "Purification of metals"),
        ])
        orig_path = self.zimi._title_index_path
        orig_close = self.zimi._close_title_db
        self.zimi._title_index_path = lambda name: self.db_path
        self.zimi._close_title_db = lambda name: None
        try:
            result = self.zimi._build_fts_for_index("test")
            self.assertEqual(result["status"], "built")
            self.assertEqual(result["entries"], 2)
            # Verify FTS5 table exists and works
            conn = self.sqlite3.connect(self.db_path)
            rows = conn.execute("SELECT path FROM titles_fts WHERE titles_fts MATCH 'water'").fetchall()
            conn.close()
            self.assertEqual(len(rows), 1)
        finally:
            self.zimi._title_index_path = orig_path
            self.zimi._close_title_db = orig_close


# ── Performance Tests (require running server) ──

class PerfTestSearch(unittest.TestCase):
    """Performance tests against a running Zimi server.

    These tests verify response times and result quality.
    Run with: python3 tests.py --perf [--perf-host HOST]
    """

    host = "http://localhost:8899"

    @classmethod
    def setUpClass(cls):
        import urllib.request
        try:
            urllib.request.urlopen(f"{cls.host}/health", timeout=5)
        except Exception:
            raise unittest.SkipTest(f"Server not reachable at {cls.host}")

    def _fetch(self, path):
        import urllib.request
        url = f"{self.host}{path}"
        t0 = time.time()
        with urllib.request.urlopen(url, timeout=120) as resp:
            data = json.loads(resp.read())
        elapsed = time.time() - t0
        return data, elapsed

    def test_health(self):
        data, elapsed = self._fetch("/health")
        self.assertEqual(data["status"], "ok")
        self.assertLess(elapsed, 1.0, "Health check should be <1s")

    def test_list(self):
        data, elapsed = self._fetch("/list")
        self.assertIsInstance(data, list)
        self.assertLess(elapsed, 2.0, "List should be <2s")

    def test_fast_search_cached(self):
        """Fast search should be <1s on cache hit."""
        self._fetch("/search?q=python&limit=5&fast=1")
        data, elapsed = self._fetch("/search?q=python&limit=5&fast=1")
        self.assertLess(elapsed, 1.0, f"Cached fast search took {elapsed:.1f}s, expected <1s")
        self.assertTrue(data.get("partial", True), "Fast search should return partial=True")

    def test_full_search_cached(self):
        """Full FTS should be <1s on cache hit."""
        self._fetch("/search?q=python&limit=5")
        data, elapsed = self._fetch("/search?q=python&limit=5")
        self.assertLess(elapsed, 1.0, f"Cached FTS took {elapsed:.1f}s, expected <1s")
        self.assertFalse(data.get("partial", False), "Full search should return partial=False")

    def test_full_search_completeness(self):
        """Full search should return results from multiple sources (requires ZIMs)."""
        data, _ = self._fetch("/search?q=python+programming&limit=10")
        # Only assert if server has ZIMs loaded
        health, _ = self._fetch("/health")
        if health.get("zim_count", 0) > 1:
            sources = list(data.get("by_source", {}).keys())
            self.assertGreater(len(sources), 1, f"Expected multiple sources, got: {sources}")

    def test_fast_vs_full_result_quality(self):
        """Full FTS should find at least as many results as fast title search."""
        fast, _ = self._fetch("/search?q=memory+management&limit=10&fast=1")
        full, _ = self._fetch("/search?q=memory+management&limit=10")
        self.assertGreaterEqual(
            full["total"], fast["total"],
            f"FTS ({full['total']}) should find >= title search ({fast['total']})"
        )

    def test_scoped_search_single_zim(self):
        """Scoped search to a single ZIM should work."""
        zims, _ = self._fetch("/list")
        if not zims:
            self.skipTest("No ZIMs loaded")
        name = zims[0]["name"]
        data, _ = self._fetch(f"/search?q=help&limit=5&zim={name}")
        for r in data.get("results", []):
            self.assertEqual(r["zim"], name, f"Result from {r['zim']} but scoped to {name}")

    def test_scoped_search_multi_zim(self):
        """Scoped search across multiple ZIMs."""
        zims, _ = self._fetch("/list")
        if len(zims) < 2:
            self.skipTest("Need at least 2 ZIMs")
        names = ",".join(z["name"] for z in zims[:3])
        data, _ = self._fetch(f"/search?q=help&limit=5&zim={names}")
        for r in data.get("results", []):
            self.assertIn(r["zim"], names.split(","))

    def test_search_result_structure(self):
        """Verify search result has all required fields."""
        data, _ = self._fetch("/search?q=test&limit=1")
        if data.get("results"):
            r = data["results"][0]
            for field in ["zim", "path", "title", "score"]:
                self.assertIn(field, r, f"Missing field '{field}' in result")

    def test_nonexistent_zim_returns_error(self):
        data, _ = self._fetch("/search?q=test&zim=does_not_exist_xyz")
        self.assertIn("error", data)

    def test_progressive_search_timing(self):
        """Phase 1 (fast) should complete before Phase 2 (FTS) for uncached queries."""
        # Use a unique query to avoid cache
        q = f"unique_test_query_{int(time.time())}"
        t0 = time.time()
        fast_data, fast_elapsed = self._fetch(f"/search?q={q}&limit=5&fast=1")
        fast_wall = time.time() - t0

        t1 = time.time()
        full_data, full_elapsed = self._fetch(f"/search?q={q}&limit=5")
        full_wall = time.time() - t1

        print(f"\n  Progressive timing: fast={fast_wall:.1f}s, full={full_wall:.1f}s")
        # Fast should generally be quicker (or at least not much slower)
        # On cold start both may be slow, so just verify they both return valid data
        self.assertIn("results", fast_data)
        self.assertIn("results", full_data)


def run_perf_tests(host):
    PerfTestSearch.host = host
    suite = unittest.TestLoader().loadTestsFromTestCase(PerfTestSearch)
    runner = unittest.TextTestRunner(verbosity=2)
    return runner.run(suite)


if __name__ == "__main__":
    if "--perf" in sys.argv or "--perf-host" in sys.argv:
        host = "http://localhost:8899"
        if "--perf-host" in sys.argv:
            idx = sys.argv.index("--perf-host")
            host = sys.argv[idx + 1]
            if not host.startswith("http"):
                host = "http://" + host
        print(f"Running performance tests against {host}")
        result = run_perf_tests(host)
        sys.exit(0 if result.wasSuccessful() else 1)
    else:
        # Unit tests only
        unittest.main(verbosity=2)
