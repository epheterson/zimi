#!/usr/bin/env python3
"""Integration tests — start a real Zimi server and hit HTTP endpoints.

These tests verify the full request/response cycle including routing,
content types, static file serving, and management flows. No ZIM files needed
for most tests.

Usage:
    python3 -m pytest tests/test_server.py -v
"""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error

# Make zimi importable from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _start_server(zim_dir, port=0):
    """Start a Zimi server on the given port, return (server, actual_port)."""
    import zimi
    from http.server import ThreadingHTTPServer

    os.environ["ZIM_DIR"] = zim_dir
    os.environ["ZIMI_MANAGE"] = "1"

    zimi.ZIM_DIR = zim_dir
    zimi.ZIMI_DATA_DIR = os.path.join(zim_dir, ".zimi")
    os.makedirs(zimi.ZIMI_DATA_DIR, exist_ok=True)
    zimi.ZIMI_MANAGE = True
    zimi._TITLE_INDEX_DIR = os.path.join(zimi.ZIMI_DATA_DIR, "titles")
    zimi.load_cache()

    server = ThreadingHTTPServer(("127.0.0.1", port), zimi.ZimHandler)
    actual_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, actual_port


class TestServerEndpoints(unittest.TestCase):
    """Test HTTP endpoints against a running server."""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp()
        cls._server, cls._port = _start_server(cls._tmpdir)
        cls._base = f"http://127.0.0.1:{cls._port}"

    @classmethod
    def tearDownClass(cls):
        cls._server.shutdown()
        import shutil
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def _get(self, path, expect_json=True):
        url = f"{self._base}{path}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = resp.read()
            if expect_json:
                return json.loads(data), resp.status
            return data, resp.status

    def _get_status(self, path):
        """GET and return just the status code (handles 4xx/5xx)."""
        url = f"{self._base}{path}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    def _post(self, path, body=None):
        """POST JSON and return (parsed_json, status_code)."""
        url = f"{self._base}{path}"
        payload = json.dumps(body or {}).encode()
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read()), resp.status
        except urllib.error.HTTPError as e:
            return json.loads(e.read()), e.code

    def _delete(self, path):
        """DELETE and return (parsed_json, status_code)."""
        url = f"{self._base}{path}"
        req = urllib.request.Request(url, method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read()), resp.status
        except urllib.error.HTTPError as e:
            return json.loads(e.read()), e.code

    # ── Health ──

    def test_health(self):
        data, status = self._get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")
        self.assertIn("version", data)
        self.assertIn("zim_count", data)

    def test_health_has_pdf_support_field(self):
        data, _ = self._get("/health")
        self.assertIn("pdf_support", data)

    # ── Web UI ──

    def test_ui_returns_html(self):
        url = f"{self._base}/"
        with urllib.request.urlopen(url, timeout=10) as resp:
            content_type = resp.headers.get("Content-Type", "")
            self.assertIn("text/html", content_type)
            body = resp.read().decode()
            self.assertIn("Zimi", body)

    def test_favicon(self):
        status = self._get_status("/favicon.ico")
        self.assertEqual(status, 200)

    def test_apple_touch_icon(self):
        status = self._get_status("/apple-touch-icon.png")
        self.assertEqual(status, 200)

    # ── List (empty library) ──

    def test_list_empty(self):
        data, status = self._get("/list")
        self.assertEqual(status, 200)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 0)

    # ── Search ──

    def test_search_empty_library(self):
        data, status = self._get("/search?q=test&limit=5")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 0)
        self.assertIn("results", data)
        self.assertIn("elapsed", data)
        self.assertIn("partial", data)

    def test_search_fast_empty(self):
        data, status = self._get("/search?q=test&limit=5&fast=1")
        self.assertEqual(status, 200)
        self.assertTrue(data["partial"])

    def test_search_full_not_partial(self):
        data, status = self._get("/search?q=test&limit=5")
        self.assertEqual(status, 200)
        self.assertFalse(data["partial"])

    def test_search_nonexistent_zim(self):
        data, status = self._get("/search?q=test&zim=does_not_exist")
        self.assertEqual(status, 200)
        self.assertIn("error", data)

    def test_search_missing_query(self):
        status = self._get_status("/search")
        self.assertEqual(status, 400)

    def test_search_nonexistent_collection(self):
        status = self._get_status("/search?q=test&collection=no_such_collection")
        self.assertEqual(status, 400)

    # ── Suggest ──

    def test_suggest_empty(self):
        data, status = self._get("/suggest?q=test")
        self.assertEqual(status, 200)
        # Suggest returns a dict keyed by ZIM name
        self.assertIsInstance(data, (list, dict))

    def test_suggest_missing_query(self):
        status = self._get_status("/suggest")
        self.assertEqual(status, 400)

    # ── Random ──

    def test_random_empty_library(self):
        data, status = self._get("/random")
        self.assertEqual(status, 200)
        self.assertIn("error", data)

    def test_random_nonexistent_zim(self):
        status = self._get_status("/random?zim=does_not_exist")
        self.assertEqual(status, 404)

    # ── Read ──

    def test_read_missing_params(self):
        status = self._get_status("/read")
        self.assertEqual(status, 400)

    def test_read_missing_path(self):
        status = self._get_status("/read?zim=wikipedia")
        self.assertEqual(status, 400)

    # ── Snippet ──

    def test_snippet_missing_params(self):
        status = self._get_status("/snippet")
        self.assertEqual(status, 400)

    # ── Catalog ──

    def test_catalog_missing_zim(self):
        status = self._get_status("/catalog")
        self.assertEqual(status, 400)

    # ── Collections ──

    def test_collections_empty(self):
        data, status = self._get("/collections")
        self.assertEqual(status, 200)
        self.assertIsInstance(data, dict)

    def test_collections_crud(self):
        """Create, read, and delete a collection."""
        # Create
        data, status = self._post("/collections", {
            "name": "test-coll",
            "label": "Test Collection",
            "zims": []
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["collection"], "test-coll")

        # Read — should be in the list
        data, status = self._get("/collections")
        self.assertIn("test-coll", data.get("collections", {}))

        # Delete
        data, status = self._delete("/collections?name=test-coll")
        self.assertEqual(status, 200)

        # Verify deleted
        data, status = self._get("/collections")
        self.assertNotIn("test-coll", data.get("collections", {}))

    def test_collections_create_missing_name(self):
        data, status = self._post("/collections", {"zims": []})
        self.assertEqual(status, 400)

    def test_collections_auto_name_from_label(self):
        data, status = self._post("/collections", {
            "label": "My Dev Docs",
            "zims": ["devdocs_python"]
        })
        self.assertEqual(status, 200)
        # Should auto-generate name from label
        self.assertTrue(len(data["collection"]) > 0)
        # Clean up
        self._delete(f"/collections?name={data['collection']}")

    # ── Static files (pdf.js) ──

    def test_static_pdfjs_viewer(self):
        status = self._get_status("/static/pdfjs/web/viewer.html")
        self.assertEqual(status, 200)

    def test_static_pdfjs_js(self):
        status = self._get_status("/static/pdfjs/build/pdf.mjs")
        self.assertEqual(status, 200)

    def test_static_pdfjs_css(self):
        status = self._get_status("/static/pdfjs/web/viewer.css")
        self.assertEqual(status, 200)

    def test_static_cache_headers(self):
        url = f"{self._base}/static/pdfjs/web/viewer.html"
        with urllib.request.urlopen(url, timeout=10) as resp:
            cc = resp.headers.get("Cache-Control", "")
            self.assertIn("immutable", cc)

    def test_static_path_traversal_blocked(self):
        status = self._get_status("/static/../zimi.py")
        self.assertIn(status, (400, 403))

    def test_static_double_dot_in_middle(self):
        status = self._get_status("/static/pdfjs/../../../zimi.py")
        self.assertIn(status, (400, 403))

    def test_static_nonexistent_404(self):
        status = self._get_status("/static/does_not_exist.txt")
        self.assertEqual(status, 404)

    # ── Management endpoints ──

    def test_manage_status(self):
        data, status = self._get("/manage/status")
        self.assertEqual(status, 200)
        self.assertIn("zim_count", data)
        self.assertIn("total_size_gb", data)
        self.assertTrue(data["manage_enabled"])

    def test_manage_stats(self):
        data, status = self._get("/manage/stats")
        self.assertEqual(status, 200)
        self.assertIn("metrics", data)
        self.assertIn("disk", data)
        self.assertIn("auto_update", data)
        self.assertIn("title_index", data)

    def test_manage_usage(self):
        data, status = self._get("/manage/usage")
        self.assertEqual(status, 200)

    def test_manage_has_password(self):
        data, status = self._get("/manage/has-password")
        self.assertEqual(status, 200)
        self.assertIn("has_password", data)
        self.assertFalse(data["has_password"])  # no password set in test

    def test_manage_downloads_empty(self):
        data, status = self._get("/manage/downloads")
        self.assertEqual(status, 200)
        self.assertIn("downloads", data)
        self.assertIsInstance(data["downloads"], list)

    def test_manage_check_updates_empty(self):
        data, status = self._get("/manage/check-updates")
        self.assertEqual(status, 200)
        self.assertIn("updates", data)
        self.assertEqual(data["count"], 0)

    def test_manage_history(self):
        data, status = self._get("/manage/history")
        self.assertEqual(status, 200)
        self.assertIn("history", data)

    def test_manage_refresh(self):
        data, status = self._post("/manage/refresh")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "refreshed")
        self.assertIn("zim_count", data)

    def test_manage_catalog_fetch(self):
        """Test that the catalog endpoint reaches the OPDS proxy."""
        # This will try to fetch from Kiwix's OPDS feed. If internet is
        # unavailable (CI), it returns a 502 — both outcomes are valid.
        status = self._get_status("/manage/catalog?count=1")
        self.assertIn(status, (200, 502))

    def test_manage_download_missing_url(self):
        data, status = self._post("/manage/download", {})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_manage_download_bad_url(self):
        data, status = self._post("/manage/download", {"url": "not-a-real-url"})
        # Should either reject or start (and fail later), both are OK
        self.assertIn(status, (200, 400))

    def test_manage_cancel_nonexistent(self):
        data, status = self._post("/manage/cancel", {"id": "fake-id-123"})
        self.assertEqual(status, 404)

    def test_manage_clear_downloads(self):
        data, status = self._post("/manage/clear-downloads")
        self.assertEqual(status, 200)
        self.assertIn("removed", data)

    def test_manage_delete_invalid_filename(self):
        data, status = self._post("/manage/delete", {"filename": "../etc/passwd"})
        self.assertEqual(status, 400)

    def test_manage_delete_nonexistent(self):
        data, status = self._post("/manage/delete", {"filename": "no_such_file.zim"})
        self.assertEqual(status, 404)

    def test_manage_delete_non_zim(self):
        data, status = self._post("/manage/delete", {"filename": "readme.txt"})
        self.assertEqual(status, 400)

    # ── Password management ──

    def test_set_and_clear_password(self):
        """Test the full password lifecycle: set → verify → clear."""
        # Set password
        data, status = self._post("/manage/set-password", {"password": "test123"})
        self.assertEqual(status, 200)

        # Verify password is set
        data, status = self._get("/manage/has-password")
        self.assertTrue(data["has_password"])

        # Clear password (requires current password)
        data, status = self._post("/manage/set-password", {
            "current": "test123",
            "password": ""
        })
        self.assertEqual(status, 200)

        # Verify cleared
        data, status = self._get("/manage/has-password")
        self.assertFalse(data["has_password"])

    # ── Auto-update config ──

    def test_auto_update_toggle(self):
        # Enable
        data, status = self._post("/manage/auto-update", {
            "enabled": True,
            "frequency": "weekly"
        })
        self.assertEqual(status, 200)
        self.assertTrue(data["enabled"])
        self.assertEqual(data["frequency"], "weekly")

        # Disable
        data, status = self._post("/manage/auto-update", {"enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(data["enabled"])

    def test_auto_update_invalid_frequency(self):
        data, status = self._post("/manage/auto-update", {"frequency": "hourly"})
        self.assertEqual(status, 400)

    # ── FTS build ──

    def test_build_fts_missing_name(self):
        data, status = self._post("/manage/build-fts", {})
        self.assertEqual(status, 400)

    def test_build_fts_nonexistent_zim(self):
        data, status = self._post("/manage/build-fts", {"name": "no_such_zim"})
        self.assertEqual(status, 404)

    # ── /w/ content route ──

    def test_w_nonexistent_zim(self):
        """Requesting content from a non-existent ZIM should 404."""
        # /w/ routes serve HTML for browser nav, so request with Sec-Fetch-Dest: iframe
        url = f"{self._base}/w/nonexistent_zim/A/Test"
        req = urllib.request.Request(url)
        req.add_header("Sec-Fetch-Dest", "iframe")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        self.assertEqual(status, 404)

    # ── 404 for unknown routes ──

    def test_unknown_route_404(self):
        status = self._get_status("/nonexistent-endpoint")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
