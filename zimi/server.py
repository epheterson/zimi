#!/usr/bin/env python3
"""
Zimi -- Offline Knowledge Viewer & API

Search and read articles from Kiwix ZIM files. Provides both a CLI and an
HTTP server with JSON API + web UI for browsing offline knowledge archives.

Requires: libzim (pip install libzim)
Optional: PyMuPDF (pip install PyMuPDF) for PDF-in-ZIM text extraction

Configuration:
  ZIM_DIR      Path to directory containing *.zim files (default: /zims)
  ZIMI_MANAGE  Set to "1" to enable library management endpoints

Usage (CLI):
  zimi search "water purification" --limit 10
  zimi read stackoverflow "Questions/12345"
  zimi list
  zimi suggest "pytho"

Usage (HTTP API):
  zimi serve --port 8899

  GET /search?q=...&limit=5&zim=...   Full-text search (cross-ZIM or scoped)
  GET /read?zim=...&path=...           Read article as plaintext
  GET /w/<zim>/<path>                  Serve raw ZIM content (HTML, images)
  GET /suggest?q=...&limit=10          Title autocomplete
  GET /snippet?zim=...&path=...        Short text snippet
  GET /list                            List all ZIM sources with metadata
  GET /catalog?zim=...                 PDF catalog for zimgit-style ZIMs
  GET /random                          Random article
  GET /resolve?url=...                 Cross-ZIM URL resolution
  GET /resolve?domains=1               Domain→ZIM map for installed sources
  GET /health                          Health check
"""

import argparse
import ast
import base64
import gzip
import glob
import hashlib
import hmac
import html
import json
import logging
import math
import os
import random as _random
import re
import subprocess
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote, urlencode
import ssl
import urllib.error
import urllib.request

import certifi

from libzim.reader import Archive
from libzim.search import Query, Searcher
from libzim.suggestion import SuggestionSearcher

try:
    import fitz  # PyMuPDF — for reading PDFs embedded in ZIM files
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False

# SSL context using certifi CA bundle (PyInstaller bundles lack system certs)
SSL_CTX = ssl.create_default_context(cafile=certifi.where())

ZIMI_VERSION = "1.5.0"

log = logging.getLogger("zimi")
logging.basicConfig(format="%(asctime)s %(message)s", datefmt="%H:%M:%S", level=logging.INFO)

ZIM_DIR = os.environ.get("ZIM_DIR", "/zims")
ZIMI_MANAGE = os.environ.get("ZIMI_MANAGE", "0") == "1"
ZIMI_DATA_DIR = os.environ.get("ZIMI_DATA_DIR", os.path.join(ZIM_DIR, ".zimi"))
try:
    os.makedirs(ZIMI_DATA_DIR, exist_ok=True)
except OSError:
    pass  # ZIM_DIR may not exist yet (e.g. during import in tests)

def _migrate_data_files():
    """Move legacy .zimi_* files from ZIM_DIR root into ZIMI_DATA_DIR."""
    migrations = [
        (".zimi_password", "password"),
        (".zimi_collections.json", "collections.json"),
        (".zimi_cache.json", "cache.json"),
    ]
    for old_name, new_name in migrations:
        old_path = os.path.join(ZIM_DIR, old_name)
        new_path = os.path.join(ZIMI_DATA_DIR, new_name)
        if os.path.exists(old_path) and not os.path.exists(new_path):
            try:
                os.makedirs(ZIMI_DATA_DIR, exist_ok=True)
                os.rename(old_path, new_path)
                log.info("Migrated %s → %s", old_name, new_name)
            except OSError:
                pass

_migrate_data_files()

def _password_file():
    """Password file path inside ZIMI_DATA_DIR."""
    return os.path.join(ZIMI_DATA_DIR, "password")

def _hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def _get_manage_password_hash():
    """Get stored password hash from env var or file."""
    pw = os.environ.get("ZIMI_MANAGE_PASSWORD", "")
    if pw:
        return _hash_pw(pw)  # env var stores plaintext, hash on read
    try:
        return open(_password_file()).read().strip()
    except (FileNotFoundError, OSError):
        return ""

def _set_manage_password(pw):
    """Save hashed password to file."""
    pf = _password_file()
    with open(pf, "w") as f:
        f.write(_hash_pw(pw) if pw else "")
    log.info("Manage password %s", "set" if pw else "cleared")

def _check_manage_auth(handler):
    """Check authorization for manage endpoints. Returns True if unauthorized."""
    stored = _get_manage_password_hash()
    if not stored:
        return None  # no password set, allow access
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and hmac.compare_digest(_hash_pw(auth[7:]), stored):
        return None  # valid
    return True  # unauthorized
MAX_CONTENT_LENGTH = 8000  # chars returned per article, keeps responses manageable for LLMs
READ_MAX_LENGTH = 50000    # longer limit for the web UI reader
MAX_SEARCH_LIMIT = 50      # upper bound for search results per ZIM to prevent resource exhaustion
MAX_CONTENT_BYTES = 10 * 1024 * 1024  # 10 MB — skip snippet extraction for entries larger than this
MAX_SERVE_BYTES = 50 * 1024 * 1024    # 50 MB — refuse to serve entries larger than this (prevents OOM)
MAX_POST_BODY = 65536                 # max bytes accepted in POST requests (64KB — handles ~500 URLs for batch resolve)

# ── Rate Limiting ──
RATE_LIMIT = int(os.environ.get("ZIMI_RATE_LIMIT", "60"))  # requests per minute per IP (0 = disabled)
_rate_buckets = {}  # {ip: [timestamps]}
_rate_lock = threading.Lock()

def _check_rate_limit(ip):
    """Check if IP has exceeded rate limit. Returns seconds to wait, or 0 if OK."""
    if RATE_LIMIT <= 0:
        return 0
    now = time.time()
    window = 60.0  # 1 minute window
    with _rate_lock:
        timestamps = _rate_buckets.get(ip, [])
        # Prune old entries
        timestamps = [t for t in timestamps if now - t < window]
        if len(timestamps) >= RATE_LIMIT:
            retry_after = max(1, int(timestamps[0] + window - now) + 1)
            _rate_buckets[ip] = timestamps
            return retry_after
        timestamps.append(now)
        _rate_buckets[ip] = timestamps
        # Periodic cleanup of stale IPs (every ~100 requests)
        if len(_rate_buckets) > 1000:
            stale = [k for k, v in _rate_buckets.items() if not v or now - v[-1] > window]
            for k in stale:
                del _rate_buckets[k]
    return 0

# ── Metrics ──
_metrics = {
    "start_time": time.time(),
    "requests": {},       # {endpoint: count}
    "latency_sum": {},    # {endpoint: total_seconds}
    "errors": 0,
    "rate_limited": 0,
}
_metrics_lock = threading.Lock()

def _record_metric(endpoint, latency, error=False):
    """Record a request metric."""
    with _metrics_lock:
        _metrics["requests"][endpoint] = _metrics["requests"].get(endpoint, 0) + 1
        _metrics["latency_sum"][endpoint] = _metrics["latency_sum"].get(endpoint, 0) + latency
        if error:
            _metrics["errors"] += 1

def _get_metrics():
    """Get current metrics snapshot."""
    with _metrics_lock:
        uptime = time.time() - _metrics["start_time"]
        total_reqs = sum(_metrics["requests"].values())
        endpoints = {}
        for ep, count in _metrics["requests"].items():
            avg_latency = _metrics["latency_sum"].get(ep, 0) / count if count > 0 else 0
            endpoints[ep] = {"count": count, "avg_latency_ms": round(avg_latency * 1000, 1)}
        return {
            "uptime_seconds": round(uptime),
            "total_requests": total_reqs,
            "errors": _metrics["errors"],
            "rate_limited": _metrics["rate_limited"],
            "endpoints": endpoints,
        }

# ── Usage Stats (in-memory, resets on restart) ──
_usage_stats = {
    "searches": 0,
    "article_reads": 0,
    "by_zim": {},  # {zim_name: {"reads": N, "searches": N}}
}
_usage_lock = threading.Lock()

def _record_usage(event_type, zim_name=None):
    """Record a usage event. Thread-safe. Only tracks known ZIM names."""
    with _usage_lock:
        if event_type == "search":
            _usage_stats["searches"] += 1
        elif event_type in ("read", "iframe"):
            _usage_stats["article_reads"] += 1
        if zim_name and zim_name in get_zim_files():
            if zim_name not in _usage_stats["by_zim"]:
                _usage_stats["by_zim"][zim_name] = {"reads": 0, "searches": 0}
            bucket = _usage_stats["by_zim"][zim_name]
            if event_type == "search":
                bucket["searches"] += 1
            else:
                bucket["reads"] += 1

def _get_usage_stats():
    """Return usage snapshot: top ZIMs, totals."""
    with _usage_lock:
        by_zim = dict(_usage_stats["by_zim"])
        top = sorted(by_zim.items(), key=lambda x: x[1]["reads"] + x[1]["searches"], reverse=True)[:10]
        return {
            "searches": _usage_stats["searches"],
            "article_reads": _usage_stats["article_reads"],
            "top_zims": [{"name": n, **v} for n, v in top],
        }

def _get_disk_usage():
    """Get disk usage info for ZIM directory. Works on all platforms."""
    try:
        import shutil
        usage = shutil.disk_usage(ZIM_DIR)
        total = usage.total
        free = usage.free
        used = usage.used
        zim_size = sum(os.path.getsize(os.path.join(ZIM_DIR, f))
                       for f in os.listdir(ZIM_DIR) if f.endswith(".zim"))
        return {
            "zim_dir": ZIM_DIR,
            "disk_total_gb": round(total / (1024**3), 1),
            "disk_free_gb": round(free / (1024**3), 1),
            "disk_used_gb": round(used / (1024**3), 1),
            "disk_pct": round(used / total * 100, 1) if total > 0 else 0,
            "zim_size_gb": round(zim_size / (1024**3), 1),
        }
    except (OSError, AttributeError):
        return {}

# ── Auto-Update ──
# If ZIMI_AUTO_UPDATE env var is set, it's an admin override (UI locked).
# If not set, the UI controls it and settings persist to disk.
_AUTO_UPDATE_CONFIG = os.path.join(ZIMI_DATA_DIR, "auto_update.json")
_auto_update_env_locked = "ZIMI_AUTO_UPDATE" in os.environ

def _load_auto_update_config():
    """Load auto-update settings. Env var overrides; otherwise use persisted config."""
    if _auto_update_env_locked:
        enabled = os.environ.get("ZIMI_AUTO_UPDATE", "0") == "1"
        freq = os.environ.get("ZIMI_UPDATE_FREQ", "weekly")
        return enabled, freq
    try:
        with open(_AUTO_UPDATE_CONFIG) as f:
            cfg = json.loads(f.read())
            return cfg.get("enabled", False), cfg.get("frequency", "weekly")
    except (OSError, json.JSONDecodeError, KeyError):
        return False, "weekly"

def _save_auto_update_config(enabled, freq):
    """Persist auto-update settings to disk."""
    try:
        with open(_AUTO_UPDATE_CONFIG, "w") as f:
            f.write(json.dumps({"enabled": enabled, "frequency": freq}))
    except OSError:
        pass

_auto_update_enabled, _auto_update_freq = _load_auto_update_config()
_auto_update_last_check = None
_auto_update_thread = None

_FREQ_SECONDS = {"daily": 86400, "weekly": 604800, "monthly": 2592000}

def _auto_update_loop(initial_delay=0):
    """Background thread that checks for and applies ZIM updates."""
    global _auto_update_last_check
    if initial_delay > 0:
        log.info("Auto-update: first check in %ds", initial_delay)
        for _ in range(initial_delay):
            if not _auto_update_enabled:
                return
            time.sleep(1)
    log.info("Auto-update enabled: checking every %s", _auto_update_freq)
    while _auto_update_enabled:
        try:
            _auto_update_last_check = time.time()
            updates = _check_updates()
            if updates:
                log.info("Auto-update: %d updates available", len(updates))
                for upd in updates:
                    url = upd.get("download_url")
                    if not url:
                        continue
                    # Skip if already downloading this file
                    filename = url.rsplit("/", 1)[-1] if "/" in url else url
                    with _download_lock:
                        already = any(d["filename"] == filename and not d.get("done")
                                      for d in _active_downloads.values())
                    if already:
                        log.info("Auto-update: skipping %s (already downloading)", filename)
                        continue
                    dl_id, err = _start_download(url)
                    if err:
                        log.warning("Auto-update download failed for %s: %s", upd.get("name", "?"), err)
                    else:
                        log.info("Auto-update started download: %s (id=%s)", upd.get("name", "?"), dl_id)
            else:
                log.info("Auto-update: all ZIMs up to date")
        except Exception as e:
            log.warning("Auto-update check failed: %s", e)
        # Sleep in 60s chunks so we can exit cleanly; re-read frequency each cycle
        interval = _FREQ_SECONDS.get(_auto_update_freq, 604800)
        for _ in range(max(interval // 60, 1)):
            if not _auto_update_enabled:
                break
            time.sleep(60)

# ── Search Cache ──
_search_cache = {}       # {key: {"result": ..., "created": float, "accesses": int}}
_search_cache_lock = threading.Lock()
SEARCH_CACHE_MAX = 100
SEARCH_CACHE_TTL = 900          # 15 minutes base
SEARCH_CACHE_TTL_ACTIVE = 1800  # 30 minutes if re-accessed

def _search_cache_get(key):
    """Get cached search result if still valid. Re-accessed entries get extended TTL."""
    with _search_cache_lock:
        entry = _search_cache.get(key)
        if not entry:
            return None
        ttl = SEARCH_CACHE_TTL_ACTIVE if entry["accesses"] > 0 else SEARCH_CACHE_TTL
        if time.time() - entry["created"] < ttl:
            entry["accesses"] += 1
            return entry["result"]
        del _search_cache[key]
    return None

def _search_cache_put(key, result):
    """Store search result in cache, evicting oldest if full."""
    now = time.time()
    with _search_cache_lock:
        if len(_search_cache) >= SEARCH_CACHE_MAX:
            oldest_key = min(_search_cache, key=lambda k: _search_cache[k]["created"])
            del _search_cache[oldest_key]
        _search_cache[key] = {"result": result, "created": now, "accesses": 0}

def _search_cache_clear():
    """Clear all cached search results (e.g. after library changes)."""
    with _search_cache_lock:
        _search_cache.clear()

# ── Suggestion Cache (per-ZIM title search) ──
_suggest_cache = {}       # {(query_lower, zim_name): {"results": [...], "ts": float}}
_suggest_cache_lock = threading.Lock()
_SUGGEST_CACHE_TTL = 900   # 15 minutes
_SUGGEST_CACHE_MAX = 500

def _suggest_cache_get(query_lower, zim_name):
    key = (query_lower, zim_name)
    with _suggest_cache_lock:
        entry = _suggest_cache.get(key)
        if not entry:
            return None
        if time.time() - entry["ts"] < _SUGGEST_CACHE_TTL:
            return entry["results"]
        del _suggest_cache[key]
    return None

_suggest_cache_puts = 0  # count puts since last persist

def _suggest_cache_put(query_lower, zim_name, results):
    global _suggest_cache_puts
    with _suggest_cache_lock:
        if len(_suggest_cache) >= _SUGGEST_CACHE_MAX:
            oldest = min(_suggest_cache, key=lambda k: _suggest_cache[k]["ts"])
            del _suggest_cache[oldest]
        _suggest_cache[(query_lower, zim_name)] = {"results": results, "ts": time.time()}
        _suggest_cache_puts += 1
        should_persist = (_suggest_cache_puts % 50 == 0)
    if should_persist:
        threading.Thread(target=_suggest_cache_persist, daemon=True).start()

def _suggest_cache_clear():
    with _suggest_cache_lock:
        _suggest_cache.clear()
    _suggest_cache_persist()
    with _suggest_pool_lock:
        _suggest_pool.clear()
        _suggest_zim_locks.clear()
    with _fts_pool_lock:
        _fts_pool.clear()
        _fts_zim_locks.clear()

_SUGGEST_CACHE_PATH = os.path.join(ZIMI_DATA_DIR, "suggest_cache.json")

def _suggest_cache_persist():
    """Save suggest cache to disk so it survives restarts."""
    try:
        with _suggest_cache_lock:
            data = {}
            for (q, zim), entry in _suggest_cache.items():
                data[f"{q}\t{zim}"] = entry
        if not data:
            # Nothing to save — remove stale file if it exists
            if os.path.exists(_SUGGEST_CACHE_PATH):
                os.remove(_SUGGEST_CACHE_PATH)
            return
        with open(_SUGGEST_CACHE_PATH + ".tmp", "w") as f:
            json.dump(data, f)
        os.replace(_SUGGEST_CACHE_PATH + ".tmp", _SUGGEST_CACHE_PATH)
        log.debug("Suggest cache persisted: %d entries", len(data))
    except Exception as e:
        log.debug("Suggest cache persist failed: %s", e)

def _suggest_cache_restore():
    """Load suggest cache from disk on startup."""
    try:
        if not os.path.exists(_SUGGEST_CACHE_PATH):
            return 0
        with open(_SUGGEST_CACHE_PATH) as f:
            data = json.load(f)
        now = time.time()
        loaded = 0
        with _suggest_cache_lock:
            for key_str, entry in data.items():
                # Skip expired entries
                if now - entry.get("ts", 0) > _SUGGEST_CACHE_TTL:
                    continue
                parts = key_str.split("\t", 1)
                if len(parts) == 2:
                    _suggest_cache[(parts[0], parts[1])] = entry
                    loaded += 1
        return loaded
    except Exception:
        return 0

# MIME type fallback for ZIM entries with empty mimetype
MIME_FALLBACK = {
    ".html": "text/html", ".htm": "text/html", ".css": "text/css",
    ".js": "application/javascript", ".mjs": "application/javascript", ".json": "application/json",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
    ".ico": "image/x-icon", ".pdf": "application/pdf",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
    ".eot": "application/vnd.ms-fontobject", ".otf": "font/otf",
    ".xml": "application/xml", ".txt": "text/plain",
    ".wasm": "application/wasm", ".bcmap": "application/octet-stream",
    ".properties": "text/plain",
    ".mp4": "video/mp4", ".webm": "video/webm", ".ogv": "video/ogg",
    ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".wav": "audio/wav",
    ".opus": "audio/opus", ".flac": "audio/flac",
    ".vtt": "text/vtt", ".srt": "text/plain",
}

def _namespace_fallbacks(path):
    """Generate alternative paths for old/new namespace ZIM compatibility.
    Old ZIMs use A/ (articles), I/ (images), C/ (CSS), -/ (metadata) prefixes.
    New ZIMs dropped them. Try stripping or adding prefixes to find the entry."""
    prefixes = ("A/", "I/", "C/", "-/")
    for p in prefixes:
        if path.startswith(p):
            yield path[len(p):]  # strip prefix
            return
    for p in prefixes:
        yield p + path  # add prefix

# MIME types that benefit from gzip (text-based, not already compressed)
COMPRESSIBLE_TYPES = {"text/", "application/javascript", "application/json", "application/xml", "image/svg+xml"}

def _categorize_zim(name):
    """Auto-categorize a ZIM by name pattern. Ordered rules, first match wins. None if unknown."""
    n = name.lower()
    # Medical — before Wikimedia so wikipedia_en_medicine categorizes correctly
    if ("medicine" in n or n == "wikem" or "ready.gov" in n
            or (n.startswith("zimgit-") and any(k in n for k in ("water", "food", "disaster")))):
        return "Medical"
    # Stack Exchange — check before Wikimedia (some SEs have wiki-adjacent names)
    if n in ("stackoverflow", "askubuntu", "superuser", "serverfault") or "stackexchange" in n:
        return "Stack Exchange"
    # Dev Docs
    if n.startswith("devdocs_") or n == "freecodecamp":
        return "Dev Docs"
    # Education
    if (n.startswith("ted_") or n.startswith("phzh_")
            or n in ("crashcourse", "phet", "appropedia", "artofproblemsolving", "edutechwiki", "explainxkcd", "coreeng1")):
        return "Education"
    # How-To — before Wikimedia so wikihow doesn't match wiki*
    if n in ("wikihow", "ifixit") or "off-the-grid" in n or "knots" in n:
        return "How-To"
    # Wikimedia — broad wiki* catch-all (wikt* for wiktionary)
    if n.startswith(("wiki", "wikt")) or n == "openstreetmap-wiki":
        return "Wikimedia"
    # Books
    if n in ("gutenberg", "rationalwiki", "theworldfactbook"):
        return "Books"
    return None


# ── History Log ──
# Persistent event log for downloads, deletions, etc. Stored in ZIMI_DATA_DIR/history.json.

_history_lock = threading.Lock()
_HISTORY_MAX = 500


def _history_file_path():
    return os.path.join(ZIMI_DATA_DIR, "history.json")


def _load_history():
    """Load event history from disk. Returns list of event dicts, newest first."""
    try:
        with open(_history_file_path()) as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return []


def _append_history(event):
    """Append an event dict to persistent history. Thread-safe."""
    with _history_lock:
        entries = _load_history()
        entries.insert(0, event)
        if len(entries) > _HISTORY_MAX:
            entries = entries[:_HISTORY_MAX]
        path = _history_file_path()
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(entries, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as e:
            log.warning("Failed to write history: %s", e)


# ── Collections & Favorites ──
# Stored in .zimi_collections.json alongside ZIM files (persists across container rebuilds).
_collections_lock = threading.Lock()

def _collections_file_path():
    """Path to the collections/favorites JSON file."""
    return os.path.join(ZIMI_DATA_DIR, "collections.json")

def _load_collections():
    """Load collections from disk. Returns default structure if missing."""
    try:
        with open(_collections_file_path()) as f:
            data = json.load(f)
        if data.get("version") != 1:
            return {"version": 1, "favorites": [], "collections": {}}
        return data
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return {"version": 1, "favorites": [], "collections": {}}

def _save_collections(data):
    """Save collections to disk (atomic write via rename)."""
    data["version"] = 1
    path = _collections_file_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("Could not save collections: %s", e)

# ── Startup cache ──
# Opening ZIM archives is expensive (~0.3s each on NAS spinning disks).
# Persistent cache in .zimi_cache.json enables instant startup on subsequent runs.
# Archives are opened lazily (on first search/read) instead of all at once.
_CACHE_VERSION = 1
_zim_list_cache = None
_zim_files_cache = None  # {name: path} — cached at startup, ZIM dir is read-only
_archive_pool = {}  # {name: Archive} — kept open for fast search
_archive_lock = threading.Lock()  # protects _archive_pool writes in threaded mode
_zim_lock = threading.Lock()      # serializes all libzim operations (C library is NOT thread-safe)
# Lock ordering: _zim_lock → _archive_lock (never acquire _zim_lock while holding _archive_lock)

# Separate archive handles for suggestion search — allows title lookups to run in
# parallel with Xapian FTS by using independent C++ Archive objects + their own lock.
# Each ZIM gets its own lock so multi-ZIM scoped searches can query in parallel.
_suggest_pool = {}   # {name: Archive} — independent handles for SuggestionSearcher
_suggest_pool_lock = threading.Lock()  # protects _suggest_pool writes
_suggest_zim_locks = {}  # {name: Lock} — per-ZIM lock for suggestion operations

# Separate archive handles for full-text search — allows parallel Xapian FTS across ZIMs.
# Same pattern as _suggest_pool: each ZIM gets its own Archive + Lock.
_fts_pool = {}       # {name: Archive}
_fts_pool_lock = threading.Lock()
_fts_zim_locks = {}  # {name: Lock}

# ── SQLite Title Index ──
# Persistent title index per ZIM for instant prefix search (<10ms vs 40s for large ZIMs).
# Built in background on startup using dedicated Archive handles (no _zim_lock needed).
import sqlite3

_TITLE_INDEX_DIR = os.path.join(ZIMI_DATA_DIR, "titles")
_TITLE_INDEX_VERSION = "4"  # bump to force rebuild (v4: add FTS5 for multi-word search)
_FTS5_ENTRY_THRESHOLD = 2_000_000  # skip FTS5 build for ZIMs above this (can be triggered manually)

# Connection pool: keep SQLite connections open to avoid per-query disk seeks.
# On spinning disk, each sqlite3.connect() is ~10ms (inode seek + first page read).
# With 54 ZIMs, that's 540ms+ of pure overhead per multi-word query.
_title_db_pool = {}       # {zim_name: sqlite3.Connection}
_title_db_pool_lock = threading.Lock()

def _get_title_db(zim_name):
    """Get a pooled SQLite connection for a title index, or None if no index."""
    with _title_db_pool_lock:
        conn = _title_db_pool.get(zim_name)
        if conn is not None:
            return conn
    db_path = _title_index_path(zim_name)
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA mmap_size=67108864")  # 64MB mmap for read perf
        with _title_db_pool_lock:
            # Another thread may have raced us — use theirs, close ours
            if zim_name in _title_db_pool:
                conn.close()
                return _title_db_pool[zim_name]
            _title_db_pool[zim_name] = conn
        return conn
    except Exception:
        return None

def _close_title_db(zim_name):
    """Close and remove a pooled connection (e.g. when index is rebuilt or ZIM deleted)."""
    with _title_db_pool_lock:
        conn = _title_db_pool.pop(zim_name, None)
    if conn:
        try:
            conn.close()
        except Exception:
            pass

def _title_index_path(zim_name):
    return os.path.join(_TITLE_INDEX_DIR, f"{zim_name}.db")

def _title_index_is_current(zim_name, zim_path):
    """Check if title index exists, matches ZIM mtime, and is current schema version."""
    db_path = _title_index_path(zim_name)
    if not os.path.exists(db_path):
        return False
    try:
        zim_mtime = str(os.path.getmtime(zim_path))
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='zim_mtime'").fetchone()
            ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            return (row is not None and row[0] == zim_mtime
                    and ver is not None and ver[0] == _TITLE_INDEX_VERSION)
        finally:
            conn.close()
    except Exception:
        return False

def _build_title_index(zim_name, zim_path):
    """Build SQLite title index for a ZIM file.

    Opens a dedicated Archive handle (not from _archive_pool) so this is safe
    to run without _zim_lock. Commits in batches to keep memory low.
    """
    os.makedirs(_TITLE_INDEX_DIR, exist_ok=True)
    db_path = _title_index_path(zim_name)
    tmp_path = db_path + ".tmp"
    t0 = time.time()
    count = 0

    # Open dedicated archive handle — never touches shared pool
    archive = open_archive(zim_path)
    conn = sqlite3.connect(tmp_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=OFF")  # safe: tmp file, rebuilt on failure
        conn.execute("CREATE TABLE titles (path TEXT PRIMARY KEY, title TEXT, title_lower TEXT)")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")

        # Asset extensions to skip — these are images, fonts, scripts, not articles
        _asset_exts = frozenset((
            '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.avif',
            '.css', '.js', '.json', '.woff', '.woff2', '.ttf', '.eot', '.otf',
            '.mp3', '.mp4', '.ogg', '.wav', '.webm',
        ))
        batch = []
        total_entries = archive.all_entry_count
        for i in range(total_entries):
            try:
                entry = archive._get_entry_by_id(i)
                if entry.is_redirect:
                    continue
                path = entry.path
                # Skip asset paths by extension
                dot = path.rfind('.')
                if dot != -1 and path[dot:].lower() in _asset_exts:
                    continue
                title = entry.title
                if not title:
                    continue
                batch.append((path, title, title.lower()))
                if len(batch) >= 10000:
                    conn.executemany("INSERT OR IGNORE INTO titles VALUES (?,?,?)", batch)
                    conn.commit()
                    count += len(batch)
                    batch.clear()
            except Exception:
                continue

        if batch:
            conn.executemany("INSERT OR IGNORE INTO titles VALUES (?,?,?)", batch)
            count += len(batch)

        if count == 0:
            conn.close()
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            log.warning("Title index: %s has 0 indexable entries, skipping", zim_name)
            return

        conn.execute("CREATE INDEX idx_prefix ON titles(title_lower)")
        # FTS5 inverted index for multi-word search (finds words anywhere in title)
        # Skip for very large ZIMs — user can trigger manually from UI
        has_fts = "0"
        if count <= _FTS5_ENTRY_THRESHOLD:
            conn.execute("CREATE VIRTUAL TABLE titles_fts USING fts5(path UNINDEXED, title, tokenize='unicode61')")
            conn.execute("INSERT INTO titles_fts(path, title) SELECT path, title FROM titles")
            has_fts = "1"
        else:
            log.info("Title index: %s has %d entries, skipping FTS5 (above %d threshold)", zim_name, count, _FTS5_ENTRY_THRESHOLD)
        zim_mtime = str(os.path.getmtime(zim_path))
        conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (_TITLE_INDEX_VERSION,))
        conn.execute("INSERT INTO meta VALUES ('zim_mtime', ?)", (zim_mtime,))
        conn.execute("INSERT INTO meta VALUES ('built_at', ?)", (str(time.time()),))
        conn.execute("INSERT INTO meta VALUES ('entry_count', ?)", (str(count),))
        conn.execute("INSERT INTO meta VALUES ('has_fts', ?)", (has_fts,))
        conn.commit()
    except Exception:
        conn.close()
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    else:
        conn.close()
        # Evict stale pooled connection before atomic replace
        _close_title_db(zim_name)
        # Atomic replace (os.replace is atomic on POSIX, avoids remove+rename race)
        os.replace(tmp_path, db_path)
        dt = time.time() - t0
        log.info("Title index: built %s (%d entries%s, %.1fs)", zim_name, count,
                 "" if has_fts == "1" else ", no FTS5", dt)

def _build_fts_for_index(zim_name):
    """Add FTS5 table to an existing title index that was built without one.
    This avoids re-scanning the ZIM file — just reads from the titles table."""
    _close_title_db(zim_name)
    db_path = _title_index_path(zim_name)
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"No title index for {zim_name}")
    t0 = time.time()
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        # Check if FTS5 already exists
        existing = conn.execute("SELECT name FROM sqlite_master WHERE name='titles_fts'").fetchone()
        if existing:
            conn.close()
            return {"status": "already_exists"}
        count = conn.execute("SELECT COUNT(*) FROM titles").fetchone()[0]
        conn.execute("CREATE VIRTUAL TABLE titles_fts USING fts5(path UNINDEXED, title, tokenize='unicode61')")
        conn.execute("INSERT INTO titles_fts(path, title) SELECT path, title FROM titles")
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('has_fts', '1')")
        conn.commit()
        conn.close()
        _close_title_db(zim_name)  # evict stale pooled connection
        dt = time.time() - t0
        log.info("Title index: built FTS5 for %s (%d entries, %.1fs)", zim_name, count, dt)
        return {"status": "built", "entries": count, "elapsed": round(dt, 1)}
    except Exception:
        conn.close()
        raise

def _title_index_search(zim_name, query, limit=10):
    """Search title index. Returns list or None if no index.

    For single-word queries: B-tree prefix range scan (instant, <1ms).
    For multi-word queries: FTS5 inverted index search — finds titles
    containing ALL query words regardless of position.

    Uses pooled connections to avoid per-query sqlite3.connect() overhead.
    """
    conn = _get_title_db(zim_name)
    if conn is None:
        return None  # no index or DB error → fallback to SuggestionSearcher
    q = query.lower().strip()
    if not q:
        return []
    words = q.split()
    try:
        if len(words) == 1:
            # Single word: B-tree prefix range scan
            q_upper = q[:-1] + chr(ord(q[-1]) + 1)
            rows = conn.execute(
                "SELECT path, title FROM titles WHERE title_lower >= ? AND title_lower < ? LIMIT ?",
                (q, q_upper, limit)
            ).fetchall()
            return [{"path": r[0], "title": r[1], "snippet": ""} for r in rows]
        else:
            # Multi-word: B-tree prefix on first word, then filter in Python.
            # FTS5 wildcard intersection is 5-6s/ZIM on spinning disk — too slow.
            # Strategy: prefix-scan the first word (fast via B-tree index), read more
            # rows than needed, then filter for titles containing all other words.
            first_word = words[0]
            other_words = [w for w in words[1:]]
            first_upper = first_word[:-1] + chr(ord(first_word[-1]) + 1)
            # Fetch more candidates (10x limit) to filter down
            fetch_limit = limit * 20
            rows = conn.execute(
                "SELECT path, title FROM titles WHERE title_lower >= ? AND title_lower < ? LIMIT ?",
                (first_word, first_upper, fetch_limit)
            ).fetchall()
            # Filter: title must contain all other words
            results = []
            for path, title in rows:
                tl = title.lower()
                if all(w in tl for w in other_words):
                    results.append({"path": path, "title": title, "snippet": ""})
                    if len(results) >= limit:
                        break
            if results:
                return results
            # Prefix on first word found nothing — skip to SuggestionSearcher fallback
            return None
    except Exception:
        # Connection may be stale (e.g. DB was rebuilt) — evict and retry once
        _close_title_db(zim_name)
        return None  # fallback on DB error

_title_index_status = {
    "state": "idle",       # idle | building | ready
    "building_now": None,  # zim name currently being built
    "built": 0,            # count built this session
    "total": 0,            # total ZIMs to index
    "ready": 0,            # indexes currently available
    "started_at": None,
    "finished_at": None,
    "errors": [],          # [(name, error_str)]
}
_title_index_status_lock = threading.Lock()

def _get_title_index_stats():
    """Return title index status + per-ZIM details for the stats API."""
    with _title_index_status_lock:
        status = dict(_title_index_status)
        status["errors"] = list(status["errors"])  # copy

    # Gather per-index file sizes and entry counts
    total_size = 0
    indexes = []
    if os.path.exists(_TITLE_INDEX_DIR):
        for f in sorted(os.listdir(_TITLE_INDEX_DIR)):
            if not f.endswith(".db"):
                continue
            db_path = os.path.join(_TITLE_INDEX_DIR, f)
            size = os.path.getsize(db_path)
            total_size += size
            name = f[:-3]
            # Read entry count and FTS5 status from meta (uses pool if available)
            entry_count = 0
            has_fts = False
            try:
                c = _get_title_db(name)
                if c:
                    row = c.execute("SELECT value FROM meta WHERE key='entry_count'").fetchone()
                    if row:
                        entry_count = int(row[0])
                    fts_row = c.execute("SELECT value FROM meta WHERE key='has_fts'").fetchone()
                    if fts_row:
                        has_fts = fts_row[0] == "1"
                    else:
                        # Legacy v4 indexes don't have has_fts key — check for table
                        tbl = c.execute("SELECT name FROM sqlite_master WHERE name='titles_fts'").fetchone()
                        has_fts = tbl is not None
            except Exception:
                pass
            indexes.append({"name": name, "size_mb": round(size / (1024 * 1024), 1), "entries": entry_count, "has_fts": has_fts})

    status["total_size_gb"] = round(total_size / (1024 ** 3), 1)
    status["index_count"] = len(indexes)
    # Use live counts: ready = indexes on disk, total = ZIM files
    status["ready"] = len(indexes)
    status["total"] = len(get_zim_files())
    status["indexes"] = sorted(indexes, key=lambda x: -x["size_mb"])
    return status

def _build_all_title_indexes():
    """Build missing/stale title indexes for all ZIM files (background task)."""
    os.makedirs(_TITLE_INDEX_DIR, exist_ok=True)
    zims = get_zim_files()

    # Count how many are already current
    need_build = []
    current = 0
    for name, path in zims.items():
        if _title_index_is_current(name, path):
            current += 1
        else:
            need_build.append((name, path))

    with _title_index_status_lock:
        _title_index_status["total"] = len(zims)
        _title_index_status["ready"] = current
        if not need_build:
            _title_index_status["state"] = "ready"
            return
        _title_index_status["state"] = "building"
        _title_index_status["started_at"] = time.time()

    built = 0
    for name, path in need_build:
        with _title_index_status_lock:
            _title_index_status["building_now"] = name
        try:
            _build_title_index(name, path)
            built += 1
            with _title_index_status_lock:
                _title_index_status["ready"] += 1
                _title_index_status["built"] += 1
        except Exception as e:
            log.warning("Title index build failed for %s: %s", name, e)
            with _title_index_status_lock:
                _title_index_status["errors"].append((name, str(e)))

    with _title_index_status_lock:
        _title_index_status["state"] = "ready"
        _title_index_status["building_now"] = None
        _title_index_status["finished_at"] = time.time()

    if built:
        log.info("Title index: built %d new indexes", built)
    # Clean up indexes for ZIMs that no longer exist
    _clean_stale_title_indexes()
    # Pre-warm connection pool: open all DBs and touch B-tree root pages
    # so first search doesn't pay ~20s of cold disk seeks across 54 ZIMs
    t0 = time.time()
    warmed = 0
    for name in zims:
        conn = _get_title_db(name)
        if conn:
            try:
                conn.execute("SELECT 1 FROM titles LIMIT 1").fetchone()
                warmed += 1
            except Exception:
                pass
    log.info("Title index pool warmed: %d connections (%.1fs)", warmed, time.time() - t0)

    # Auto-build FTS5 for ZIMs where estimated build time < 5 minutes.
    # Index DB size < 2.5 GB correlates with ~5 min on spinning disk.
    _FTS5_AUTO_BUILD_MAX_MB = 2500
    auto_fts = 0
    for name in zims:
        conn = _get_title_db(name)
        if not conn:
            continue
        try:
            fts_row = conn.execute("SELECT value FROM meta WHERE key='has_fts'").fetchone()
        except Exception:
            continue
        if fts_row and fts_row[0] == "1":
            continue
        db_path = _title_index_path(name)
        try:
            size_mb = os.path.getsize(db_path) / (1024 * 1024)
        except OSError:
            continue
        if size_mb < _FTS5_AUTO_BUILD_MAX_MB:
            try:
                with _title_index_status_lock:
                    _title_index_status["building_now"] = name
                    _title_index_status["state"] = "building"
                _build_fts_for_index(name)
                auto_fts += 1
            except Exception as e:
                log.warning("Auto FTS5 build failed for %s: %s", name, e)
    if auto_fts:
        log.info("Auto-built FTS5 for %d indexes", auto_fts)
    with _title_index_status_lock:
        _title_index_status["state"] = "ready"
        _title_index_status["building_now"] = None
        _title_index_status["finished_at"] = time.time()

def _clean_stale_title_indexes():
    """Remove title index DBs for ZIM files that no longer exist."""
    if not os.path.exists(_TITLE_INDEX_DIR):
        return
    zims = get_zim_files()
    for f in os.listdir(_TITLE_INDEX_DIR):
        if f.endswith(".db"):
            name = f[:-3]  # strip .db
            if name not in zims:
                _close_title_db(name)
                try:
                    os.remove(os.path.join(_TITLE_INDEX_DIR, f))
                    log.info("Removed stale title index: %s", f)
                except OSError:
                    pass

# Load UI template from file (next to this script)
_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
try:
    with open(os.path.join(_TEMPLATE_DIR, "index.html")) as f:
        SEARCH_UI_HTML = f.read()
except FileNotFoundError:
    SEARCH_UI_HTML = "<html><body><h1>Zimi</h1><p>UI template not found. API endpoints are still available.</p></body></html>"


def _scan_zim_files():
    """Scan filesystem for ZIM files. Returns {short_name: path} mapping."""
    zims = {}
    for path in sorted(glob.glob(os.path.join(ZIM_DIR, "*.zim"))):
        filename = os.path.basename(path)
        # Create short name: stackoverflow.com_en_all_2023-11.zim → stackoverflow
        name = filename.split(".zim")[0]
        # Simplify common patterns
        name = re.sub(r"\.com_en_all.*", "", name)
        name = re.sub(r"\.stackexchange\.com_en_all.*", "", name)
        name = re.sub(r"_en_all_maxi.*", "", name)
        name = re.sub(r"_en_all.*", "", name)
        name = re.sub(r"_en_maxi.*", "", name)
        name = re.sub(r"_en_2\d{3}.*", "", name)
        name = re.sub(r"_maxi_2\d{3}.*", "", name)
        name = re.sub(r"_2\d{3}-\d{2}$", "", name)
        zims[name] = path
    return zims


def get_zim_files():
    """Get ZIM file mapping. Uses startup cache (ZIM dir is read-only mount)."""
    global _zim_files_cache
    if _zim_files_cache is not None:
        return _zim_files_cache
    _zim_files_cache = _scan_zim_files()
    return _zim_files_cache


# ── Cross-ZIM Domain Map ──
# Maps external domains → installed ZIM short names, enabling cross-ZIM navigation.
# Built once at startup (or on cache reload), lives in memory.

# Wellknown domains where filename ≠ domain (Wikimedia projects, etc.)

_domain_zim_map = {}  # {domain: zim_name} — only installed ZIMs
_xzim_refs = {}  # {(source_zim, target_zim): count} — cross-ZIM reference tracking
_xzim_refs_lock = threading.Lock()  # protects _xzim_refs read-modify-write


def _build_domain_zim_map():
    """Build domain→ZIM map entirely from ZIM metadata — no hardcoded lists.

    Three auto-discovery methods, in order:
    1. Filename extraction: "stackoverflow.com_en_all_*.zim" → stackoverflow.com
    2. Source metadata: ZIM Source="www.appropedia.org" → appropedia.org
    3. Name-based inference: ZIM name "wikihow" → wikihow.com (try common TLDs)

    For each discovered domain, also registers www. and mobile (en.m.) variants.
    """
    global _domain_zim_map
    zims = get_zim_files()
    dmap = {}

    def _add_domain(domain, name):
        """Register a domain and its common variants (www., mobile)."""
        domain = domain.lower().strip()
        if not domain or "." not in domain:
            return
        if domain not in dmap:
            dmap[domain] = name
        # www. variant
        if domain.startswith("www."):
            bare = domain[4:]
            if bare not in dmap:
                dmap[bare] = name
        else:
            www = "www." + domain
            if www not in dmap:
                dmap[www] = name
        # Mobile Wikimedia variant: XX.wiki*.org → XX.m.wiki*.org (all languages)
        m = re.match(r'^(\w{2,3})\.(wiki\w+\.org)$', domain)
        if m:
            mobile = f"{m.group(1)}.m.{m.group(2)}"
            if mobile not in dmap:
                dmap[mobile] = name
        # Common mobile variants for non-wiki sites
        if domain in ("stackoverflow.com", "stackexchange.com"):
            mob = "m." + domain
            if mob not in dmap:
                dmap[mob] = name

    # 1. Extract domains from ZIM filenames
    for name, path in zims.items():
        filename = os.path.basename(path)
        base = filename.split(".zim")[0]
        m = re.match(r'^([a-zA-Z0-9.-]+\.[a-z]{2,})_', base)
        if m:
            _add_domain(m.group(1), name)

    # 2. Extract domains from ZIM Source metadata
    mapped_names = set(dmap.values())
    for name, path in zims.items():
        if name in mapped_names:
            continue
        archive = get_archive(name)
        if not archive:
            continue
        try:
            source = bytes(archive.get_metadata("Source")).decode("utf-8", "replace").strip()
        except Exception:
            continue
        if not source:
            continue
        try:
            if "://" in source:
                domain = urlparse(source).hostname or ""
            else:
                domain = source.split("/")[0]
        except Exception:
            continue
        _add_domain(domain, name)

    # 3. Name-based inference for unmapped ZIMs: try <name>.com, .org, .io
    mapped_names = set(dmap.values())
    for name in zims:
        if name in mapped_names:
            continue
        # Skip names that clearly aren't domains (zimgit-, devdocs_, etc.)
        if name.startswith("zimgit") or "_en_" in name:
            continue
        for tld in [".com", ".org", ".io", ".net"]:
            candidate = name + tld
            _add_domain(candidate, name)

    _domain_zim_map = dmap
    log.info("Domain map: %d domains → %d ZIMs", len(dmap), len(set(dmap.values())))


def _resolve_url_to_zim(url_str):
    """Resolve an external URL to a ZIM name + entry path, or None.

    Returns {"zim": name, "path": path} if found, else None.
    Must be called with _zim_lock held (uses archive.get_entry_by_path).
    """
    try:
        parsed = urlparse(url_str)
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None

    # Look up domain (try exact, then without www.)
    zim_name = _domain_zim_map.get(host)
    if not zim_name:
        bare = re.sub(r'^www\.', '', host)
        zim_name = _domain_zim_map.get(bare)
    if not zim_name:
        return None

    archive = get_archive(zim_name)
    if archive is None:
        return None

    url_path = unquote(parsed.path).lstrip("/")

    # Build candidate paths based on domain type
    candidates = []
    if "wikipedia.org" in host or "wiktionary.org" in host or "wikivoyage.org" in host \
       or "wikibooks.org" in host or "wikiversity.org" in host or "wikiquote.org" in host \
       or "wikinews.org" in host:
        # Wikimedia: /wiki/Article_Name → A/Article_Name
        rest = re.sub(r'^wiki/', '', url_path)
        candidates.append("A/" + rest)
        candidates.append(rest)
        # Strip Wikimedia namespaces (Topic:, Category:, Portal:, etc.)
        ns_stripped = re.sub(r'^[A-Z][a-z]+:', '', rest)
        if ns_stripped != rest:
            candidates.append(ns_stripped)
            candidates.append("A/" + ns_stripped)
    elif "stackexchange.com" in host or "stackoverflow.com" in host \
         or "serverfault.com" in host or "superuser.com" in host or "askubuntu.com" in host:
        # Stack Exchange: /questions/12345/title → A/questions/12345/title
        candidates.append("A/" + url_path)
        candidates.append(url_path)
    elif "rationalwiki.org" in host or "appropedia.org" in host:
        # MediaWiki sites: /wiki/Article → Article (no A/ prefix)
        rest = re.sub(r'^wiki/', '', url_path)
        candidates.append(rest)
        candidates.append("A/" + rest)
    elif "explainxkcd.com" in host:
        # /wiki/index.php/1234 → 1234:_Title (try number prefix match)
        rest = re.sub(r'^wiki/index\.php/', '', url_path)
        candidates.append(rest)
        candidates.append("A/" + rest)
    elif "wikihow.com" in host:
        # WikiHow: /Article-Name → A/Article-Name
        candidates.append("A/" + url_path)
        candidates.append(url_path)
    else:
        # General: try both A/<path> and raw <path>, plus domain-prefixed path
        candidates.append("A/" + url_path)
        candidates.append(url_path)
        # Some ZIMs prefix paths with domain (e.g. apod.nasa.gov/apod/ap...)
        if host:
            candidates.append(host + "/" + url_path)

    # Try each candidate path
    for cpath in candidates:
        if not cpath:
            continue
        try:
            archive.get_entry_by_path(cpath)
            return {"zim": zim_name, "path": cpath}
        except KeyError:
            continue
    return None


def strip_html(text):
    """Remove HTML tags and decode entities, return plain text."""
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_pdf_text(pdf_bytes, max_length=MAX_CONTENT_LENGTH):
    """Extract text from a PDF byte stream using PyMuPDF."""
    if not HAS_PYMUPDF:
        return "[PDF content — install PyMuPDF to extract text]"
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text()
            if len(text) >= max_length:
                break
        doc.close()
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_length]
    except Exception as e:
        return f"[PDF extraction error: {e}]"


def parse_catalog(archive):
    """Parse database.js from zimgit-style ZIMs to get PDF metadata catalog."""
    try:
        entry = archive.get_entry_by_path("database.js")
        content = bytes(entry.get_item().content).decode("UTF-8", errors="replace")
        # database.js uses Python-style dicts with single quotes
        content = content.replace("var DATABASE = ", "").strip().rstrip(";")
        # ast.literal_eval handles Python-style single-quoted dicts safely
        items = ast.literal_eval(content)
        return items
    except Exception:
        return None


def open_archive(path):
    """Open a ZIM archive."""
    return Archive(path)


def _get_suggest_archive(name):
    """Get a suggestion-dedicated Archive handle and per-ZIM lock.

    Each ZIM gets its own Archive + Lock, allowing parallel suggestion searches
    across different ZIMs while keeping each ZIM's C++ object single-threaded.
    """
    if name in _suggest_pool:
        return _suggest_pool[name], _suggest_zim_locks[name]
    zims = get_zim_files()
    if name in zims:
        with _suggest_pool_lock:
            if name in _suggest_pool:
                return _suggest_pool[name], _suggest_zim_locks[name]
            archive = open_archive(zims[name])
            _suggest_pool[name] = archive
            _suggest_zim_locks[name] = threading.Lock()
            return archive, _suggest_zim_locks[name]
    return None, None


def _get_fts_archive(name):
    """Get an FTS-dedicated Archive handle and per-ZIM lock.

    Same pattern as _get_suggest_archive: each ZIM gets its own Archive + Lock,
    allowing parallel Xapian full-text searches across different ZIMs.
    """
    if name in _fts_pool:
        return _fts_pool[name], _fts_zim_locks[name]
    zims = get_zim_files()
    if name in zims:
        with _fts_pool_lock:
            if name in _fts_pool:
                return _fts_pool[name], _fts_zim_locks[name]
            archive = open_archive(zims[name])
            _fts_pool[name] = archive
            _fts_zim_locks[name] = threading.Lock()
            return archive, _fts_zim_locks[name]
    return None, None


def suggest_search_zim(archive, query_str, limit=5):
    """Fast title search via SuggestionSearcher (B-tree, ~10-50ms any ZIM size)."""
    results = []
    try:
        ss = SuggestionSearcher(archive)
        suggestion = ss.suggest(query_str)
        count = min(suggestion.getEstimatedMatches(), limit)
        for path in suggestion.getResults(0, count):
            try:
                entry = archive.get_entry_by_path(path)
                results.append({"path": path, "title": entry.title, "snippet": ""})
            except Exception:
                results.append({"path": path, "title": path, "snippet": ""})
    except Exception:
        pass
    return results


def search_zim(archive, query_str, limit=10, snippets=True):
    """Full-text search within a ZIM file. Returns list of {path, title, snippet}.

    With snippets=False, skips reading article content — much faster on spinning disks
    since it avoids random seeks for each result's body.
    """
    results = []
    try:
        searcher = Searcher(archive)
        query = Query().set_query(query_str)
        search = searcher.search(query)
        count = min(search.getEstimatedMatches(), limit)
        for path in search.getResults(0, count):
            try:
                entry = archive.get_entry_by_path(path)
                if not snippets:
                    results.append({"path": path, "title": entry.title, "snippet": ""})
                    continue
                item = entry.get_item()
                content_size = item.size
                if content_size > MAX_CONTENT_BYTES:
                    results.append({
                        "path": path,
                        "title": entry.title,
                        "snippet": f"[Large entry: {content_size // 1024}KB]",
                    })
                    continue
                content = bytes(item.content).decode("UTF-8", errors="replace")
                plain = strip_html(content)
                snippet = plain[:300] + "..." if len(plain) > 300 else plain
                results.append({
                    "path": path,
                    "title": entry.title,
                    "snippet": snippet,
                })
            except Exception:
                results.append({"path": path, "title": path, "snippet": ""})
    except Exception as e:
        results.append({"error": str(e)})
    return results


_meta_title_re = re.compile(r'^(Portal:|Category:|Wikipedia:|Template:|Help:|File:|Special:|List of |Index of |Outline of )', re.IGNORECASE)

STOP_WORDS = {"a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
              "has", "have", "how", "i", "in", "is", "it", "its", "my", "not",
              "of", "on", "or", "so", "that", "the", "this", "to", "was", "we",
              "what", "when", "where", "which", "who", "will", "with", "you"}


def _clean_query(q):
    """Strip stop words for better Xapian matching. Keep quoted phrases intact."""
    phrases = re.findall(r'"[^"]*"', q)
    rest = re.sub(r'"[^"]*"', '', q)
    words = [w for w in rest.split() if w.lower() not in STOP_WORDS]
    return ' '.join(phrases + words).strip() or q


def _score_result(title, query_words, rank, entry_count):
    """Score a search result for cross-ZIM ranking."""
    tl = title.lower()
    hits = sum(1 for w in query_words if w in tl)
    if hits == len(query_words):
        title_score = 80
    elif hits > 0:
        title_score = 50 * (hits / len(query_words))
    else:
        title_score = 0
    # Exact phrase match bonus
    if ' '.join(query_words) in tl:
        title_score = 100
    # Position within source (rank 0 = 20, rank 5 = 3.3, capped at 5 if no title match)
    rank_score = 20 / (rank + 1)
    if title_score == 0:
        rank_score = min(rank_score, 5)
    # Source authority: slight boost for larger ZIMs (log scale)
    auth_score = min(5, math.log10(max(entry_count, 1)) / 2)
    return title_score + rank_score + auth_score


def search_all(query_str, limit=5, filter_zim=None, fast=False):
    """Search across all ZIM files, a specific one, or a list.

    filter_zim can be None (all), a string (single ZIM), or a list of strings.
    fast=True: title-only search via SuggestionSearcher (~10-50ms), returns partial=True.

    Returns unified ranked format:
    {
      "results": [{"zim": ..., "path": ..., "title": ..., "snippet": ..., "score": ...}],
      "by_source": {"zim_name": count, ...},
      "total": N,
      "elapsed": seconds,
      "partial": bool  (True when fast=True, False otherwise)
    }

    Searches smallest ZIMs first. No time budgets or skipping — every ZIM is
    searched fully. Use fast=True for instant title matches, then full FTS for
    complete results (progressive two-phase pattern).
    """
    zims = get_zim_files()
    cache_meta = {z["name"]: (z.get("entries") if isinstance(z.get("entries"), int) else 0) for z in (_zim_list_cache or [])}

    # Normalize filter_zim to None or list
    if isinstance(filter_zim, str):
        filter_zim = [filter_zim]
    scoped = bool(filter_zim)
    single_zim = scoped and len(filter_zim) == 1  # single-ZIM: no time limits

    if filter_zim:
        missing = [z for z in filter_zim if z not in zims]
        if missing:
            return {"results": [], "by_source": {}, "total": 0, "elapsed": 0,
                    "partial": fast, "error": f"ZIM(s) not found: {', '.join(missing)}"}
        # Sort multi-ZIM scopes smallest-first (like global) for speed
        if single_zim:
            target_names = filter_zim
        else:
            target_names = sorted(filter_zim, key=lambda n: cache_meta.get(n, 0))
    else:
        target_names = sorted(zims.keys(), key=lambda n: cache_meta.get(n, 0))

    # Clean query for Xapian (only pass raw query for single-ZIM scope)
    cleaned = _clean_query(query_str) if not single_zim else query_str
    query_words = [w.lower() for w in cleaned.split() if w.lower() not in STOP_WORDS] or [w.lower() for w in query_str.split()]

    raw_results = []
    by_source = {}
    timings = []
    search_start = time.time()

    # Junk path patterns (SE tag index pages, etc.)
    _junk_re = re.compile(r'questions/tagged/|/tags$|/tags/page')

    if fast:
        # ── Fast path: title-only via SuggestionSearcher ──
        # Uses dedicated archive handles with per-ZIM locks so multiple ZIMs
        # can be searched in parallel (and independently of _zim_lock FTS).
        q_lower = query_str.lower().strip()
        thread_results = {}  # {name: [results]}

        def _search_one_zim(name):
            try:
                cached_suggest = _suggest_cache_get(q_lower, name)
                if cached_suggest is not None:
                    thread_results[name] = cached_suggest
                    return
                # Try SQLite title index first (instant, <10ms)
                idx_results = _title_index_search(name, query_str, limit=limit)
                if idx_results is not None:
                    _suggest_cache_put(q_lower, name, idx_results)
                    thread_results[name] = idx_results
                    return
                # Fallback: SuggestionSearcher (slow for large ZIMs on spinning disk)
                archive, lock = _get_suggest_archive(name)
                if archive is None or lock is None:
                    return
                with lock:
                    results = suggest_search_zim(archive, query_str, limit=limit)
                _suggest_cache_put(q_lower, name, results)
                thread_results[name] = results
            except Exception:
                pass

        if len(target_names) == 1:
            _search_one_zim(target_names[0])
        else:
            threads = [threading.Thread(target=_search_one_zim, args=(n,), daemon=True) for n in target_names]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        for name, results in thread_results.items():
            valid = [r for r in results if not _junk_re.search(r.get("path", ""))]
            if valid:
                entry_count = cache_meta.get(name, 1)
                for rank, r in enumerate(valid):
                    score = _score_result(r["title"], query_words, rank, entry_count)
                    raw_results.append({
                        "zim": name, "path": r["path"], "title": r["title"],
                        "snippet": "", "score": round(score, 1),
                    })
                by_source[name] = len(valid)
    else:
        # ── Full path: Xapian FTS — search every ZIM in parallel ──
        # Each ZIM gets its own Archive handle + per-ZIM lock via _fts_pool,
        # so Xapian queries run concurrently (bounded by slowest ZIM, not sum).
        fts_results = {}  # {name: (results_list, dt)}

        def _fts_one_zim(name):
            try:
                archive, lock = _get_fts_archive(name)
                if archive is None or lock is None:
                    return
                t0 = time.time()
                with lock:
                    results = search_zim(archive, cleaned, limit=limit, snippets=False)
                dt = time.time() - t0
                fts_results[name] = (results, dt)
            except Exception:
                pass

        threads = [threading.Thread(target=_fts_one_zim, args=(n,), daemon=True) for n in target_names]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)  # Don't wait forever for a single ZIM

        for name, (results, dt) in fts_results.items():
            if dt > 0.3:
                timings.append(f"{name}={dt:.1f}s")
            valid = [r for r in results if "error" not in r and not _junk_re.search(r.get("path", ""))]
            if valid:
                entry_count = cache_meta.get(name, 1)
                for rank, r in enumerate(valid):
                    score = _score_result(r["title"], query_words, rank, entry_count)
                    raw_results.append({
                        "zim": name, "path": r["path"], "title": r["title"],
                        "snippet": r.get("snippet", ""), "score": round(score, 1),
                    })
                by_source[name] = len(valid)

    if timings:
        log.info("  slow zims: %s", ", ".join(timings))

    # Sort by score descending
    raw_results.sort(key=lambda r: r["score"], reverse=True)

    # Deduplicate by title (keep highest-scored)
    seen_titles = set()
    deduped = []
    for r in raw_results:
        key = r["title"].lower().strip()
        if key not in seen_titles:
            seen_titles.add(key)
            deduped.append(r)

    elapsed = round(time.time() - search_start, 2)
    return {
        "results": deduped,
        "by_source": by_source,
        "total": len(deduped),
        "elapsed": elapsed,
        "partial": fast,
    }


def read_article(zim_name, article_path, max_length=MAX_CONTENT_LENGTH):
    """Read a specific article from a ZIM file. Returns plain text. Handles HTML and PDF."""
    zims = get_zim_files()
    if zim_name not in zims:
        return {"error": f"ZIM '{zim_name}' not found. Available: {list(zims.keys())}"}

    archive = get_archive(zim_name) or open_archive(zims[zim_name])
    try:
        entry = archive.get_entry_by_path(article_path)
        item = entry.get_item()
        raw = bytes(item.content)

        title = entry.title
        if item.mimetype == "application/pdf":
            # Extract text from embedded PDF
            plain = extract_pdf_text(raw, max_length=max_length)
            # Try to find a better title from the catalog
            catalog = parse_catalog(archive)
            if catalog:
                for doc in catalog:
                    fps = doc.get("fp", [])
                    if any(article_path.endswith(fp) for fp in fps):
                        title = doc.get("ti", title)
                        break
        else:
            content = raw.decode("UTF-8", errors="replace")
            plain = strip_html(content)

        truncated = len(plain) > max_length
        return {
            "zim": zim_name,
            "path": article_path,
            "title": title,
            "content": plain[:max_length],
            "truncated": truncated,
            "full_length": len(plain),
            "mimetype": item.mimetype,
        }
    except KeyError:
        return {"error": f"Article '{article_path}' not found in {zim_name}"}


def get_catalog(zim_name):
    """Get the document catalog for zimgit-style ZIMs (PDF collections with metadata)."""
    zims = get_zim_files()
    if zim_name not in zims:
        return {"error": f"ZIM '{zim_name}' not found. Available: {list(zims.keys())}"}

    archive = get_archive(zim_name) or open_archive(zims[zim_name])
    catalog = parse_catalog(archive)
    if not catalog:
        return {"error": f"No catalog (database.js) found in {zim_name} — not a zimgit-style PDF collection"}

    docs = []
    for doc in catalog:
        fps = doc.get("fp", [])
        docs.append({
            "title": doc.get("ti", "?"),
            "description": doc.get("dsc", ""),
            "author": doc.get("aut", ""),
            "path": f"files/{fps[0]}" if fps else None,
        })
    return {"zim": zim_name, "documents": docs, "count": len(docs)}


# Diverse seed words for random article selection via FTS.
# Broad topics ensure good coverage across any ZIM's content.

def _pick_html_entry(archive, paths):
    """From a list of entry paths, return the first valid HTML/PDF article."""
    _random.shuffle(paths)
    for path in paths:
        try:
            entry = archive.get_entry_by_path(path)
            if entry.is_redirect:
                entry = entry.get_redirect_entry()
            item = entry.get_item()
            mt = item.mimetype or ""
            if mt and not mt.startswith("text/html") and mt != "application/pdf":
                continue
            return {"path": entry.path, "title": entry.title}
        except Exception:
            continue
    return None


def random_entry(archive, max_attempts=8, rng=None):
    """Pick a random article using random entry index (fast, no seed lists).

    Primary: pick random indices from the archive's entry range.
    Fallback: SuggestionSearcher with random 2-char prefixes.
    If rng is provided, use it for deterministic picks (daily persistence).
    """
    if rng is None:
        rng = _random
    # Phase 1: Random entry by index (O(1) per attempt, works on all ZIMs)
    total = archive.entry_count
    if total > 0:
        for _ in range(max_attempts):
            idx = rng.randint(0, total - 1)
            try:
                entry = archive._get_entry_by_id(idx)
                if entry.is_redirect:
                    entry = entry.get_redirect_entry()
                item = entry.get_item()
                mt = item.mimetype or ""
                if not mt.startswith("text/html") and mt != "application/pdf":
                    continue
                # Skip non-article entries (metadata, assets, etc.)
                if entry.path.startswith("_") or entry.path.startswith("-/"):
                    continue
                # Skip meta/portal pages — not interesting for "random article"
                title = entry.title or ""
                if _meta_title_re.search(title):
                    continue
                return {"path": entry.path, "title": title}
            except Exception:
                continue

    # Phase 2: SuggestionSearcher fallback
    chars = "abcdefghijklmnopqrstuvwxyz"
    for _ in range(max_attempts):
        prefix = rng.choice(chars) + rng.choice(chars)
        try:
            ss = SuggestionSearcher(archive)
            suggestion = ss.suggest(prefix)
            count = suggestion.getEstimatedMatches()
            if count == 0:
                continue
            paths = list(suggestion.getResults(0, min(count, 30)))
            result = _pick_html_entry(archive, paths)
            if result:
                return result
        except Exception:
            continue
    return None


_factbook_countries_cache = None  # list of (path, title) sorted alphabetically


def _get_factbook_countries(archive):
    """Build sorted list of country pages from World Factbook ZIM. Cached."""
    global _factbook_countries_cache
    if _factbook_countries_cache is not None:
        return _factbook_countries_cache
    countries = []
    # Try common path patterns: "countries/XX.html" or "geos/XX.html"
    for pattern_prefix in ("countries", "geos"):
        for i in range(archive.entry_count):
            try:
                entry = archive._get_entry_by_id(i)
                p = entry.path
                if p.startswith(pattern_prefix + "/") and p.endswith(".html") \
                        and len(p) == len(pattern_prefix) + 8:  # e.g. "geos/xx.html"
                    countries.append((p, entry.title))
            except Exception:
                continue
        if countries:
            break
    if not countries:
        # Fallback: collect any HTML pages that look like country pages
        # (short path, not a field page, not index)
        for i in range(archive.entry_count):
            try:
                entry = archive._get_entry_by_id(i)
                p = entry.path
                if p.endswith(".html") and "/" in p and len(p.split("/")) == 2 \
                        and not p.startswith("fields/") and p != "index.html" \
                        and not p.startswith("print_"):
                    countries.append((p, entry.title))
            except Exception:
                continue
    countries.sort(key=lambda x: x[1])
    _factbook_countries_cache = countries
    log.info("factbook countries: %d entries", len(countries))
    return countries


def _get_dated_entry(archive, zim_name, mmdd, rng=None):
    """Try to find an article for today's date in date-based or content ZIMs.

    Strategies:
    1. APOD: construct path directly (apYYMMDD)
    2. Wikipedia: look for "On this day" style pages (month+day events)
    3. Any ZIM with FTS: search for "month day" to find date-relevant content

    Must be called with _zim_lock held.
    """
    mm, dd = mmdd[:2], mmdd[2:]
    months = ["January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November", "December"]
    month_name = months[int(mm) - 1]
    day_num = str(int(dd))  # strip leading zero

    # APOD: try paths like apod.nasa.gov/apod/ap{YY}{MM}{DD}.html for recent years
    if "apod" in zim_name.lower():
        now = time.localtime()
        for year_offset in range(0, 30):
            yr = now.tm_year - year_offset
            yy = str(yr)[-2:]
            path = f"apod.nasa.gov/apod/ap{yy}{mm}{dd}.html"
            try:
                entry = archive.get_entry_by_path(path)
                return {"path": path, "title": entry.title}
            except KeyError:
                continue

    # Wikipedia: load the "Month_Day" article and follow a random internal link
    # to an actual interesting article (the date page itself is a list of events)
    if "wikipedia" in zim_name.lower():
        date_page_html = None
        for prefix in ["A/", ""]:
            dpath = f"{prefix}{month_name}_{day_num}"
            try:
                entry = archive.get_entry_by_path(dpath)
                if entry.is_redirect:
                    entry = entry.get_redirect_entry()
                raw = bytes(entry.get_item().content)
                date_page_html = raw.decode("utf-8", errors="replace")[:100000]
                break
            except KeyError:
                continue
        if date_page_html:
            # Extract article links from the date page (href="./Title" or href="A/Title")
            links = re.findall(r'href=["\'](?:\./|A/)([^"\'#]+)["\']', date_page_html)
            # Filter out year pages (just digits), meta pages, and duplicates
            seen = set()
            candidates = []
            for link in links:
                clean = unquote(link).replace("_", " ")
                if clean in seen or re.match(r'^\d+$', clean):
                    continue
                if any(clean.startswith(ns) for ns in ["Category:", "Wikipedia:", "Template:", "Help:", "Portal:", "File:", "Special:"]):
                    continue
                seen.add(clean)
                candidates.append(link)
            _rng = rng or _random
            _rng.shuffle(candidates)
            for link in candidates[:30]:
                for prefix in ["A/", ""]:
                    try:
                        entry = archive.get_entry_by_path(prefix + link)
                        if entry.is_redirect:
                            entry = entry.get_redirect_entry()
                        item = entry.get_item()
                        if not (item.mimetype or "").startswith("text/html"):
                            continue
                        title = entry.title or ""
                        if _meta_title_re.search(title) or len(title) < 3:
                            continue
                        return {"path": entry.path, "title": title}
                    except (KeyError, Exception):
                        continue

    # World Factbook: pick a country page by day-of-year index
    if "theworldfactbook" in zim_name.lower():
        countries = _get_factbook_countries(archive)
        if countries:
            now = time.localtime()
            doy = now.tm_yday
            path, title = countries[doy % len(countries)]
            # Clean factbook titles: "Africa :: Zambia — The World Factbook" → "Zambia"
            title = re.sub(r'\s*[\u2014–—]\s*The World Factbook.*$', '', title)
            title = re.sub(r'^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s*::\s*', '', title)
            return {"path": path, "title": title.strip()}

    # FTS search: look for "month day" in article titles
    try:
        searcher = Searcher(archive)
        query = Query().set_query(f"{month_name} {day_num}")
        search = searcher.search(query)
        count = search.getEstimatedMatches()
        if count > 0:
            paths = list(search.getResults(0, min(count, 10)))
            result = _pick_html_entry(archive, paths)
            if result:
                return result
    except Exception:
        pass

    return None


# XKCD comic date lookup — parsed from the archive page (cached per ZIM)
_xkcd_date_cache = {}  # comic_number → "YYYY-MM-DD"
_xkcd_date_cache_built = False

def _xkcd_date_lookup(archive, path):
    """Look up publication date for an XKCD comic from the archive page.

    Parses xkcd.com/archive/ once and caches the number→date mapping.
    Must be called with _zim_lock held.
    """
    global _xkcd_date_cache_built
    if not _xkcd_date_cache_built:
        _xkcd_date_cache_built = True
        try:
            entry = archive.get_entry_by_path("xkcd.com/archive/")
            raw = bytes(entry.get_item().content)
            html_str = raw.decode("utf-8", errors="replace")
            for m in re.finditer(r'href="[^"]*?/(\d+)/?"[^>]*?title="(\d{4}-\d{1,2}-\d{1,2})"', html_str):
                num, date_str = m.group(1), m.group(2)
                # Normalize to YYYY-MM-DD with zero-padding
                parts = date_str.split("-")
                normalized = f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"
                _xkcd_date_cache[num] = normalized
            log.info("xkcd date cache: %d comics", len(_xkcd_date_cache))
        except Exception as e:
            log.warning("xkcd date cache failed: %s", e)
    # Extract comic number from path like "xkcd.com/2607/"
    m = re.search(r'/(\d+)/?$', path)
    if m:
        return _xkcd_date_cache.get(m.group(1))
    return None


def _resolve_img_path(archive, path, src):
    """Resolve a relative image src to a ZIM entry path. Returns URL or None."""
    decoded = unquote(unquote(src))
    if decoded.startswith("/"):
        img_path = decoded.lstrip("/")
    else:
        base = "/".join(path.split("/")[:-1])
        img_path = (base + "/" + decoded) if base else decoded
    parts = []
    for seg in img_path.replace("\\", "/").split("/"):
        if seg == "..":
            if parts: parts.pop()
        elif seg and seg != ".":
            parts.append(seg)
    img_path = "/".join(parts)
    try:
        archive.get_entry_by_path(img_path)
        return img_path
    except KeyError:
        pass
    if img_path.startswith("A/"):
        try:
            bare = img_path[2:]
            archive.get_entry_by_path(bare)
            return bare
        except KeyError:
            pass
    return None


def _extract_preview(archive, zim_name, path):
    """Extract the best thumbnail image and a text blurb from an article.

    Uses Open Graph / Twitter meta tags first, falls back to largest content
    image and first substantial <p> text. This is the same approach used by
    iMessage, Slack, and Discord for link previews.

    Returns {"thumbnail": str|None, "blurb": str|None}.
    Must be called with _zim_lock held.
    """
    result = {"thumbnail": None, "blurb": None, "title": None}
    try:
        entry = archive.get_entry_by_path(path)
        if entry.is_redirect:
            entry = entry.get_redirect_entry()
        content = bytes(entry.get_item().content)
        html_str = content.decode("utf-8", errors="replace")[:80000]
    except Exception:
        return result

    # -- Title: extract from <title> or og:title if entry.title is a slug --
    entry_title = entry.title or ""
    if "-" in entry_title and " " not in entry_title:
        # Looks like a URL slug — try to extract a better title
        for pattern in [
            r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
            r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:title["\']',
            r'<title[^>]*>([^<]+)</title>',
            r'<p\s+class=["\']title\s+lang-default["\'][^>]*>(.*?)</p>',
            r'<p\s+class=["\']title["\'][^>]*>(.*?)</p>',
            r'<h1[^>]*>(.*?)</h1>',
        ]:
            tm = re.search(pattern, html_str, re.IGNORECASE | re.DOTALL)
            if tm:
                clean_title = strip_html(html.unescape(tm.group(1).strip()))
                # Strip site suffixes like " | TED Talk", "— The World Factbook"
                clean_title = re.sub(r'\s*[\|–—]\s*(TED\s*Talk|TED|Wikipedia|The World Factbook).*$', '', clean_title)
                # Strip Factbook region prefixes like "Africa :: " or "Europe :: "
                clean_title = re.sub(r'^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s*::\s*', '', clean_title)
                if len(clean_title) > 3 and clean_title != entry_title:
                    result["title"] = clean_title[:200]
                    break
        else:
            # No HTML title found — title-case the slug as last resort
            result["title"] = entry_title.replace("-", " ").replace("_", " ").title()[:200]

    # -- Wikiquote: extract an actual quote from <ul><li> blocks --
    if "wikiquote" in zim_name.lower():
        # Wikiquote structure: <ul><li>Quote text<ul><li>Attribution</li></ul></li></ul>
        # Strategy: find <ul> blocks that contain nested <ul> (quote + attribution).
        # Use a simple stack-based approach to find balanced top-level <ul> blocks.
        for ul_m in re.finditer(r'<ul>', html_str):
            start = ul_m.start()
            # Find the matching </ul> by counting nesting depth
            depth = 1
            pos = ul_m.end()
            while depth > 0 and pos < len(html_str) and pos < start + 5000:
                next_open = html_str.find('<ul', pos)
                next_close = html_str.find('</ul>', pos)
                if next_close < 0:
                    break
                if next_open >= 0 and next_open < next_close:
                    depth += 1
                    pos = next_open + 3
                else:
                    depth -= 1
                    pos = next_close + 5
            if depth != 0:
                continue
            block = html_str[start:pos]
            # Must have a nested <ul> (attribution) to be a quote block
            if block.count('<ul') < 2:
                continue
            # Extract text before the first nested <ul> as the quote
            inner_ul_pos = block.find('<ul', 4)  # skip the outer <ul>
            if inner_ul_pos < 0:
                continue
            quote_html = block[4:inner_ul_pos]  # between outer <ul> and first nested <ul>
            # Strip the wrapping <li> tag
            quote_html = re.sub(r'^\s*<li[^>]*>', '', quote_html)
            text = strip_html(quote_html).strip()
            if 20 < len(text) < 400 and len(text.split()) > 4:
                if text.startswith(("Category:", "See also", "External links", "Retrieved")):
                    continue
                result["blurb"] = "\u201c" + text[:250] + "\u201d"
                # Attribution: prefer page title (the person being quoted).
                # Wikiquote nested <li> often has "Author, Source (Year), Ch." format.
                # Extract the author name: text before first comma or parenthesis.
                author = result.get("title") or entry_title
                inner_block = block[inner_ul_pos:]
                attr_raw = strip_html(inner_block).strip()
                attr_raw = re.sub(r'^[\u2014\u2013\-~]+\s*', '', attr_raw).strip().split('\n')[0].strip()
                if attr_raw and 3 < len(attr_raw) < 200:
                    if not re.search(r'[\[\]{}]|https?:|www\.|^\d', attr_raw, re.IGNORECASE):
                        # Extract name: everything before first comma or opening paren
                        # e.g. "Henry Adams, Mont Saint Michel and Chartres (1904)" → "Henry Adams"
                        name_part = re.split(r'[,(]', attr_raw)[0].strip()
                        # Handle honorifics with commas: "Adams, Henry" or "King, Jr., Martin Luther"
                        # If name_part is a single word and next part also looks like a name, rejoin
                        if name_part and ',' in attr_raw:
                            parts = [p.strip() for p in attr_raw.split(',')]
                            # "Last, First" pattern: single capitalized word, then capitalized word(s)
                            if (len(parts) >= 2 and re.match(r'^[A-Z][a-z]+$', parts[0])
                                    and re.match(r'^(Jr\.|Sr\.|[A-Z])', parts[1])):
                                # Check for Jr./Sr. suffix
                                if parts[1] in ('Jr.', 'Sr.', 'III', 'II', 'IV') and len(parts) >= 3:
                                    name_part = parts[2].strip() + ' ' + parts[0] + ', ' + parts[1]
                                elif re.match(r'^[A-Z][a-z]', parts[1]):
                                    # "Last, First ..." — but only if second part is short (a name, not a book title)
                                    if len(parts[1].split()) <= 3:
                                        name_part = parts[1] + ' ' + parts[0]
                        # Validate: must start with uppercase letter, reasonable length
                        if (name_part and 2 < len(name_part) < 60
                                and re.match(r'^[A-Z]', name_part)
                                and not re.match(r'^(p\.|ch\.|vol\.|see |ibid)', name_part, re.IGNORECASE)):
                            author = name_part
                if author:
                    result["attribution"] = author[:100]
                break

    # -- TED Talks: extract speaker name and photo --
    if "ted" in zim_name.lower():
        # <p id="speaker"> has the last name; speaker_desc has the full name in prose.
        # Strategy: get last name, then find "FirstName LastName" in speaker_desc.
        speaker = None
        last_name = None
        sp_m = re.search(r'<p\s+id=["\']speaker["\'][^>]*>(.*?)</p>', html_str, re.DOTALL | re.IGNORECASE)
        if sp_m:
            last_name = re.sub(r'\s+', ' ', strip_html(sp_m.group(1))).strip()
            if ' ' in last_name:
                # Already a full name (some playlist ZIMs have full names)
                speaker = last_name
        # Find full name in speaker_desc by locating the last name in context
        if not speaker and last_name:
            sp_desc = re.search(r'<p\s+id=["\']speaker_desc["\'][^>]*>(.*?)</p>', html_str, re.DOTALL | re.IGNORECASE)
            if sp_desc:
                desc_text = re.sub(r'\s+', ' ', strip_html(sp_desc.group(1))).strip()
                # Find last name in the desc and grab preceding word(s) as first name
                # e.g. "Biologist E.O. Wilson explored..." → find "Wilson", grab "E.O. Wilson"
                esc_last = re.escape(last_name)
                name_m = re.search(r'((?:(?:[A-Z][\w.\'\u2019-]*|el|de|van|von|al)\s+){0,3})' + esc_last + r'\b', desc_text)
                if name_m:
                    prefix = name_m.group(1).strip()
                    if prefix:
                        speaker = (prefix + " " + last_name).strip()
                    else:
                        speaker = last_name
        if not speaker:
            speaker = last_name  # fallback to last name if desc search failed
        if speaker and len(speaker) > 1:
            result["speaker"] = speaker[:100]
        sp_img = re.search(r'<img\s+id=["\']speaker_img["\'][^>]*src=["\']([^"\']+)["\']', html_str, re.IGNORECASE)
        if not sp_img:
            sp_img = re.search(r'<img[^>]*id=["\']speaker_img["\'][^>]*src=["\']([^"\']+)["\']', html_str, re.IGNORECASE)
        if sp_img:
            src = sp_img.group(1)
            if not src.startswith("http") and not src.startswith("//") and not src.startswith("data:"):
                resolved = _resolve_img_path(archive, path, src)
                if resolved:
                    result["thumbnail"] = f"/w/{zim_name}/{resolved}"

    # -- World Factbook: extract country flag image --
    if "theworldfactbook" in zim_name.lower() and not result["thumbnail"]:
        # Look for flag images: <img> with alt/src containing "flag"
        for flag_m in re.finditer(r'<img\b([^>]*)>', html_str[:60000], re.IGNORECASE):
            attrs = flag_m.group(1)
            alt_m = re.search(r'alt=["\']([^"\']*)["\']', attrs, re.IGNORECASE)
            src_m = re.search(r'src=["\']([^"\']+)["\']', attrs, re.IGNORECASE)
            if not src_m:
                continue
            src = src_m.group(1)
            is_flag = False
            if alt_m and "flag" in alt_m.group(1).lower():
                is_flag = True
            if "flag" in src.lower():
                is_flag = True
            if is_flag and not src.startswith("http") and not src.startswith("//") and not src.startswith("data:"):
                resolved = _resolve_img_path(archive, path, src)
                if resolved:
                    result["thumbnail"] = f"/w/{zim_name}/{resolved}"
                    break

    # -- World Factbook: try locator map if no flag found --
    if "theworldfactbook" in zim_name.lower() and not result["thumbnail"]:
        for loc_m in re.finditer(r'<img\b([^>]*)>', html_str[:60000], re.IGNORECASE):
            attrs = loc_m.group(1)
            src_m = re.search(r'src=["\']([^"\']+)["\']', attrs, re.IGNORECASE)
            if not src_m:
                continue
            src = src_m.group(1)
            if "locator-map" in src.lower() and not src.startswith(("http", "//", "data:")):
                resolved = _resolve_img_path(archive, path, src)
                if resolved:
                    result["thumbnail"] = f"/w/{zim_name}/{resolved}"
                    break

    # -- xkcd: use comic alt-text (title attr) as blurb --
    if "xkcd" in zim_name.lower() and not result["blurb"]:
        for img_m in re.finditer(r'<img\b([^>]*)>', html_str, re.IGNORECASE):
            attrs = img_m.group(1)
            title_m = re.search(r'title=["\']([^"\']+)["\']', attrs)
            if title_m and len(title_m.group(1).strip()) > 20:
                text = html.unescape(title_m.group(1).strip())
                if "license" not in text.lower() and "creative commons" not in text.lower():
                    result["blurb"] = text[:200]
                    break

    # -- Gutenberg: extract author from dc.creator meta --
    if "gutenberg" in zim_name.lower():
        creator_m = re.search(r'<meta\s+content="([^"]+)"\s+name="dc\.creator"', html_str[:8000], re.IGNORECASE)
        if not creator_m:
            creator_m = re.search(r'<meta\s+name="dc\.creator"\s+content="([^"]+)"', html_str[:8000], re.IGNORECASE)
        if creator_m:
            author = creator_m.group(1).strip()
            # Convert "Last, First, dates" → "First Last"
            if ',' in author:
                parts = author.split(',')
                last = parts[0].strip()
                first = parts[1].strip() if len(parts) > 1 else ''
                # Skip date-like parts (e.g. "1808-1889")
                if first and not re.match(r'^\d', first):
                    author = first + ' ' + last
                else:
                    author = last
            if author and author.lower() != 'various':
                result["author"] = author[:100]

    # -- Wiktionary: extract definition and part of speech (English only) --
    if "wiktionary" in zim_name.lower():
        # Only extract from the English section of the page
        eng_m = re.search(r'<h2[^>]*id=["\']English["\']', html_str[:30000], re.IGNORECASE)
        if eng_m:
            # Slice from English header to next <h2> (next language section) or end
            eng_start = eng_m.start()
            next_h2 = re.search(r'<h2[^>]*id=', html_str[eng_start + 50:30000], re.IGNORECASE)
            eng_end = (eng_start + 50 + next_h2.start()) if next_h2 else 30000
            eng_section = html_str[eng_start:eng_end]
            # Part of speech from <h3>/<h4>
            for pos_m in re.finditer(r'<h[34][^>]*>(.*?)</h', eng_section, re.DOTALL | re.IGNORECASE):
                pos_text = strip_html(pos_m.group(1)).strip()
                if pos_text.lower() in ('noun', 'verb', 'adjective', 'adverb', 'pronoun', 'preposition',
                                         'conjunction', 'interjection', 'determiner', 'particle', 'prefix', 'suffix'):
                    result["part_of_speech"] = pos_text
                    break
            # Definition from first <ol><li> — skip boring inflected forms
            _boring_def = re.compile(r'^(plural of |third-person |simple past |past participle |present participle |alternative |archaic |obsolete |misspelling |eye dialect |nonstandard )', re.IGNORECASE)
            for def_m in re.finditer(r'<ol[^>]*>\s*<li[^>]*>(.*?)</li>', eng_section, re.DOTALL):
                def_text = strip_html(def_m.group(1)).strip()
                def_text = re.split(r'\n', def_text)[0].strip()
                if len(def_text) > 5 and not def_text.startswith(('Category:', 'See also')):
                    if _boring_def.match(def_text):
                        result["boring"] = True  # signal to retry
                    else:
                        result["blurb"] = def_text[:200]
                    break
        else:
            # No <h2 id="English"> — could be Simple Wiktionary (monolingual, no language headers)
            # or a non-English entry. Check if page has any <ol><li> definitions.
            is_simple = "simple" in zim_name.lower()
            if is_simple:
                # Simple Wiktionary: treat entire page as English content
                eng_section = html_str[:30000]
                # Part of speech: Simple Wiktionary uses <h2> for POS (not nested under language)
                for pos_m in re.finditer(r'<h[234][^>]*>(.*?)</h', eng_section, re.DOTALL | re.IGNORECASE):
                    pos_text = strip_html(pos_m.group(1)).strip()
                    if pos_text.lower() in ('noun', 'verb', 'adjective', 'adverb', 'pronoun', 'preposition',
                                             'conjunction', 'interjection', 'determiner', 'particle', 'prefix', 'suffix'):
                        result["part_of_speech"] = pos_text
                        break
                if not result.get("part_of_speech"):
                    # Try inline pattern: (noun), (verb), etc.
                    pos_inline = re.search(r'\((\w+)\)', eng_section[:3000])
                    if pos_inline and pos_inline.group(1).lower() in ('noun', 'verb', 'adjective', 'adverb'):
                        result["part_of_speech"] = pos_inline.group(1).capitalize()
                _boring_def = re.compile(r'^(plural of |third-person |simple past |past participle |present participle |alternative |archaic |obsolete |misspelling |eye dialect |nonstandard )', re.IGNORECASE)
                for def_m in re.finditer(r'<ol[^>]*>\s*<li[^>]*>(.*?)</li>', eng_section, re.DOTALL):
                    def_text = strip_html(def_m.group(1)).strip()
                    def_text = re.split(r'\n', def_text)[0].strip()
                    if len(def_text) > 5 and not def_text.startswith(('Category:', 'See also')):
                        if _boring_def.match(def_text):
                            result["boring"] = True
                        else:
                            result["blurb"] = def_text[:200]
                        break
            else:
                # Full Wiktionary, no English section — flag for the random endpoint to skip
                result["non_english"] = True

    # -- Blurb: og:description > meta description > first <p> --
    if not result["blurb"]:
        for pattern in [
            r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']+)["\']',
            r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:description["\']',
            r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']+)["\']',
            r'<meta\s+content=["\']([^"\']+)["\']\s+name=["\']description["\']',
        ]:
            m = re.search(pattern, html_str, re.IGNORECASE)
            if m and len(m.group(1).strip()) > 20:
                result["blurb"] = html.unescape(m.group(1).strip())[:200]
                break
    if not result["blurb"]:
        # First substantial <p> text (skip tiny nav/footer paragraphs and boilerplate)
        _skip_blurb = re.compile(r'(Creative Commons|This work is licensed|free to copy and share|All rights reserved|Copyright \d|DMCA)', re.IGNORECASE)
        for pm in re.finditer(r'<p\b[^>]*>(.*?)</p>', html_str, re.DOTALL | re.IGNORECASE):
            text = strip_html(pm.group(1))
            if len(text) > 40 and not _skip_blurb.search(text):
                result["blurb"] = text[:200]
                break

    # -- Thumbnail: og:image > twitter:image > largest content image --
    # Skip if already extracted (e.g., TED speaker photo, country flag)
    if result["thumbnail"]:
        return result
    for pattern in [
        r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
        r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:image["\']',
        r'<meta\s+name=["\']twitter:image["\']\s+content=["\']([^"\']+)["\']',
        r'<meta\s+content=["\']([^"\']+)["\']\s+name=["\']twitter:image["\']',
    ]:
        m = re.search(pattern, html_str, re.IGNORECASE)
        if m:
            src = m.group(1)
            if src.startswith("http://") or src.startswith("https://") or src.startswith("//"):
                continue  # external URL, can't serve from ZIM
            if not src.lower().endswith(".svg"):
                resolved = _resolve_img_path(archive, path, src)
                if resolved:
                    result["thumbnail"] = f"/w/{zim_name}/{resolved}"
                    return result

    # Fall back to best content image using scoring heuristics:
    # - Penalize banners (aspect ratio > 4:1)
    # - Prefer images with meaningful alt text (content images)
    # - Images without explicit dimensions are likely content (generous default)
    # - Skip images in header/nav/footer chrome
    best_img = None
    best_score = 0
    for m in re.finditer(r'<img\b([^>]*)>', html_str, re.IGNORECASE):
        attrs = m.group(1)
        src_m = re.search(r'src=["\']([^"\']+)["\']', attrs)
        if not src_m:
            continue
        src = src_m.group(1)
        if src.startswith("data:") or src.startswith("http") or src.startswith("//"):
            continue
        if src.lower().endswith(".svg") or src.lower().endswith(".svg.png"):
            continue
        # Skip generic site chrome images (navigation icons, banners)
        src_base = src.rsplit("/", 1)[-1].lower()
        if src_base in ("home_on.png", "home_off.png", "banner_ext2.png",
                         "photo_on.gif", "one-page-summary.png", "travel-facts.png"):
            continue
        w_m = re.search(r'width=["\']?(\d+)', attrs)
        h_m = re.search(r'height=["\']?(\d+)', attrs)
        has_dims = bool(w_m or h_m)
        w = int(w_m.group(1)) if w_m else 400  # no attrs → assume large content
        h = int(h_m.group(1)) if h_m else 300
        if w < 50 or h < 50:
            continue
        # Skip images inside header/nav/footer
        ctx_start = max(0, m.start() - 300)
        ctx = html_str[ctx_start:m.start()].lower()
        if re.search(r'<(header|nav|footer)\b', ctx) and not re.search(r'</(header|nav|footer)>', ctx):
            continue
        # Score: area + bonuses for content signals
        area = w * h
        ratio = max(w, h) / max(min(w, h), 1)
        score = area
        if ratio > 4:
            score *= 0.2  # heavy penalty for banners
        alt_m = re.search(r'alt=["\']([^"\']+)["\']', attrs)
        if alt_m and len(alt_m.group(1)) > 3:
            alt_lower = alt_m.group(1).lower()
            if alt_lower not in ("logo", "icon", "banner", "spacer"):
                score *= 1.5  # bonus for meaningful alt text
        if not has_dims:
            score *= 1.3  # content images often omit dimensions
        if score > best_score:
            resolved = _resolve_img_path(archive, path, src)
            if resolved:
                best_img = f"/w/{zim_name}/{resolved}"
                best_score = score

    result["thumbnail"] = best_img
    return result


def suggest(query_str, zim_name=None, limit=10):
    """Title-based autocomplete suggestions."""
    zims = get_zim_files()
    target_names = [zim_name] if zim_name and zim_name in zims else list(zims.keys())
    all_suggestions = {}

    for name in target_names:
        try:
            archive = get_archive(name) or open_archive(zims[name])
            ss = SuggestionSearcher(archive)
            suggestion = ss.suggest(query_str)
            count = min(suggestion.getEstimatedMatches(), limit)
            results = []
            for s_path in suggestion.getResults(0, count):
                try:
                    entry = archive.get_entry_by_path(s_path)
                    results.append({"path": s_path, "title": entry.title})
                except Exception:
                    results.append({"path": s_path, "title": s_path})
            if results:
                all_suggestions[name] = results
        except Exception as e:
            all_suggestions[name] = [{"error": str(e)}]

    return all_suggestions


def list_zims(use_cache=True):
    """List all available ZIM files with metadata. Uses startup cache when available."""
    global _zim_list_cache
    if use_cache and _zim_list_cache is not None:
        return _zim_list_cache

    zims = get_zim_files()
    info = []
    for name, path in zims.items():
        size_gb = os.path.getsize(path) / (1024 ** 3)
        try:
            archive = open_archive(path)
            entry_count = archive.entry_count
        except Exception:
            entry_count = "?"
        info.append({
            "name": name,
            "file": os.path.basename(path),
            "size_gb": round(size_gb, 3),
            "entries": entry_count,
        })
    return info


def get_archive(name):
    """Get a cached archive handle, or open it fresh. Thread-safe."""
    if name in _archive_pool:
        return _archive_pool[name]
    zims = get_zim_files()
    if name in zims:
        with _archive_lock:
            # Double-check after acquiring lock
            if name in _archive_pool:
                return _archive_pool[name]
            archive = open_archive(zims[name])
            _archive_pool[name] = archive
            return archive
    return None


def _cache_file_path():
    """Path to the persistent metadata cache file."""
    return os.path.join(ZIMI_DATA_DIR, "cache.json")


def _load_disk_cache():
    """Load persistent metadata cache from disk. Returns {filename: metadata} or None."""
    try:
        with open(_cache_file_path()) as f:
            data = json.load(f)
        if data.get("version") != _CACHE_VERSION:
            return None
        return data.get("files", {})
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


def _save_disk_cache(file_cache):
    """Save metadata cache to disk (atomic write via rename)."""
    data = {
        "version": _CACHE_VERSION,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": file_cache,
    }
    try:
        path = _cache_file_path()
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("Could not save cache: %s", e)


def _extract_zim_date(filename):
    """Extract the date portion from a ZIM filename. Returns (base_name, date_str) or (base_name, None)."""
    m = re.search(r'_(\d{4}-\d{2})\.zim$', filename)
    if m:
        base = filename[:m.start()]
        return base, m.group(1)
    return filename.replace('.zim', ''), None


def _extract_zim_metadata(name, path):
    """Open a ZIM archive and extract its metadata. Returns (info_dict, archive)."""
    size_gb = os.path.getsize(path) / (1024 ** 3)
    meta_title = name
    meta_desc = ""
    meta_date = ""
    has_icon = False
    main_path = ""
    archive = None
    try:
        archive = open_archive(path)
        entry_count = archive.entry_count
        for key in archive.metadata_keys:
            try:
                val = bytes(archive.get_metadata(key))
                if key == "Title":
                    meta_title = val.decode("utf-8", errors="replace").strip() or name
                elif key == "Description":
                    meta_desc = val.decode("utf-8", errors="replace").strip()
                elif key == "Date":
                    meta_date = val.decode("utf-8", errors="replace").strip()
                elif key.startswith("Illustration_48x48"):
                    has_icon = True
            except Exception:
                pass
        try:
            me = archive.main_entry
            if me.is_redirect:
                me = me.get_redirect_entry()
            main_path = me.path
        except Exception:
            pass
    except Exception:
        entry_count = "?"
    # Fall back to date from filename if not in metadata
    if not meta_date:
        _, file_date = _extract_zim_date(os.path.basename(path))
        if file_date:
            meta_date = file_date
    info = {
        "name": name,
        "file": os.path.basename(path),
        "size_gb": round(size_gb, 3),
        "entries": entry_count,
        "title": meta_title,
        "description": meta_desc,
        "date": meta_date,
        "has_icon": has_icon,
        "category": _categorize_zim(name),
        "main_path": main_path,
    }
    return info, archive


def load_cache(force=False):
    """Load ZIM metadata, using persistent disk cache for instant startup.

    On first run: scans all ZIMs (slow), saves cache to .zimi_cache.json.
    On subsequent runs: reads cache, validates mtimes, only re-scans changed files.
    Archives are opened lazily on first access, not at startup.
    """
    global _zim_list_cache, _zim_files_cache
    t0 = time.time()
    _zim_files_cache = _scan_zim_files()
    zims = _zim_files_cache

    disk_cache = None if force else _load_disk_cache()

    info = []
    scanned = 0
    file_cache = {}  # for saving back to disk

    for name, path in zims.items():
        filename = os.path.basename(path)
        try:
            stat = os.stat(path)
            mtime = stat.st_mtime
            size = stat.st_size
        except OSError:
            continue

        cached = disk_cache.get(filename) if disk_cache else None
        if cached and cached.get("mtime") == mtime and cached.get("size") == size:
            # Cache hit — use stored metadata, skip opening archive
            entry = {
                "name": name,
                "file": filename,
                "size_gb": cached.get("size_gb", round(size / (1024 ** 3), 3)),
                "entries": cached.get("entries", "?"),
                "title": cached.get("title", name),
                "description": cached.get("description", ""),
                "date": cached.get("date", ""),
                "has_icon": cached.get("has_icon", False),
                "category": _categorize_zim(name),
                "main_path": cached.get("main_path", ""),
            }
            info.append(entry)
            file_cache[filename] = cached
        else:
            # Cache miss — scan this ZIM
            entry, archive = _extract_zim_metadata(name, path)
            if archive:
                _archive_pool[name] = archive
            info.append(entry)
            scanned += 1
            file_cache[filename] = {
                "name": name,
                "mtime": mtime,
                "size": size,
                "size_gb": entry["size_gb"],
                "entries": entry["entries"],
                "title": entry["title"],
                "description": entry["description"],
                "date": entry.get("date", ""),
                "has_icon": entry["has_icon"],
                "main_path": entry["main_path"],
            }

    _zim_list_cache = info
    elapsed = time.time() - t0

    # Persist cache if we scanned anything new
    if scanned > 0 or disk_cache is None:
        _save_disk_cache(file_cache)

    cached_count = len(info) - scanned
    if cached_count > 0 and scanned > 0:
        print(f"  Cache loaded: {len(info)} ZIMs ({cached_count} cached, {scanned} scanned) in {elapsed:.1f}s", flush=True)
    elif scanned > 0:
        print(f"  Cache built: {len(info)} ZIMs scanned in {elapsed:.1f}s", flush=True)
    else:
        print(f"  Cache loaded: {len(info)} ZIMs from disk cache in {elapsed:.1f}s", flush=True)

    # Rebuild domain map whenever ZIM list changes
    _build_domain_zim_map()


# ── Library Management (gated by ZIMI_MANAGE=1) ──

_active_downloads = {}  # {id: {"url": ..., "filename": ..., "pid": ..., "started": ...}}
_download_counter = 0
_download_lock = threading.Lock()

KIWIX_OPDS_BASE = "https://library.kiwix.org/catalog/search"


def _fetch_kiwix_catalog(query="", lang="eng", count=20, start=0):
    """Fetch and parse the Kiwix OPDS catalog. Returns (total, items, error)."""
    params = {"count": str(count), "start": str(start)}
    if query:
        params["q"] = query
    if lang:
        params["lang"] = lang
    url = KIWIX_OPDS_BASE + "?" + urlencode(params)

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Zimi/1.0"})
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as resp:
            xml_bytes = resp.read()
    except Exception as e:
        return 0, [], str(e)

    # Parse OPDS (Atom) XML
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "opds": "http://opds-spec.org/2010/catalog",
        "dc": "http://purl.org/dc/terms/",
    }
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        return 0, [], str(e)

    # Total results — Kiwix puts this in the Atom namespace (not OpenSearch)
    atom_ns = ns["atom"]
    total_el = root.find(f"{{{atom_ns}}}totalResults")
    if total_el is None:
        total_el = root.find(".//{http://a9.com/-/spec/opensearch/1.1/}totalResults")
    try:
        total = int(total_el.text or "0") if total_el is not None else 0
    except (ValueError, TypeError):
        total = 0

    # Build set of installed filename bases (date-stripped) for accurate matching
    local_bases = set()
    for path in glob.glob(os.path.join(ZIM_DIR, "*.zim")):
        base, _ = _extract_zim_date(os.path.basename(path))
        local_bases.add(base.lower())
    items = []
    for entry in root.findall("atom:entry", ns):
        name = ""
        title = ""
        summary = ""
        language = ""
        category = ""
        author = ""
        date = ""
        article_count = 0
        media_count = 0
        size_bytes = 0
        download_url = ""
        icon_url = ""

        # Most fields are in the Atom namespace (default)
        _t = lambda tag: entry.findtext(f"{{{atom_ns}}}{tag}") or ""
        name = _t("name")
        title = _t("title")
        summary = _t("summary")
        language = _t("language")
        category = _t("category")
        try:
            article_count = int(_t("articleCount"))
        except (ValueError, TypeError):
            pass
        try:
            media_count = int(_t("mediaCount"))
        except (ValueError, TypeError):
            pass

        # Author is nested: <author><name>...</name></author>
        author_el = entry.find("atom:author/atom:name", ns)
        if author_el is not None and author_el.text and author_el.text != "-":
            author = author_el.text

        # Date from dc:issued
        date_el = entry.find("dc:issued", ns)
        if date_el is not None and date_el.text:
            date = date_el.text[:10]  # Just YYYY-MM-DD

        for link in entry.findall("atom:link", ns):
            rel = link.get("rel", "")
            href = link.get("href", "")
            ltype = link.get("type", "")
            if rel == "http://opds-spec.org/acquisition/open-access" and ltype == "application/x-zim":
                download_url = href
                try:
                    size_bytes = int(link.get("length", "0"))
                except (ValueError, TypeError):
                    pass
            elif rel == "http://opds-spec.org/image/thumbnail":
                icon_url = "https://library.kiwix.org" + href if href.startswith("/") else href

        # Determine if installed by matching download URL filename against local ZIMs
        installed = False
        if download_url:
            dl_fn = download_url.split("/")[-1]
            dl_base, _ = _extract_zim_date(dl_fn)
            installed = dl_base.lower() in local_bases

        items.append({
            "name": name,
            "title": title,
            "summary": summary,
            "language": language,
            "category": category,
            "author": author,
            "date": date,
            "article_count": article_count,
            "media_count": media_count,
            "size_bytes": size_bytes,
            "download_url": download_url,
            "icon_url": icon_url,
            "installed": installed,
        })

    return total, items, None


def _check_updates():
    """Compare installed ZIMs against Kiwix catalog to find available updates.

    Fetches a large batch from the catalog and matches by base name.
    Returns list of {name, installed_date, latest_date, download_url}.
    """
    zims = get_zim_files()
    # Build lookup: catalog_prefix → (short_name, installed_date, filename)
    # Match by checking if installed filename starts with catalog name + '_'
    installed_files = []
    for name, path in zims.items():
        filename = os.path.basename(path)
        _, date = _extract_zim_date(filename)
        if date:
            installed_files.append({"name": name, "date": date, "filename": filename, "filebase": filename.replace('.zim', '')})

    if not installed_files:
        return []

    # Fetch full catalog to check all installed ZIMs (paginated)
    all_items = []
    total, items, err = _fetch_kiwix_catalog(query="", lang="eng", count=500, start=0)
    if err:
        return []
    all_items.extend(items)
    while len(all_items) < total:
        _, more, err = _fetch_kiwix_catalog(query="", lang="eng", count=500, start=len(all_items))
        if err or not more:
            break
        all_items.extend(more)

    # Build index: for each catalog item, note its name and date
    catalog_index = []
    for item in all_items:
        dl_url = item.get("download_url", "")
        if not dl_url:
            continue
        cat_name = item.get("name", "")
        cat_date = item.get("date", "")[:7] if item.get("date") else ""
        if not cat_date or not cat_name:
            continue
        catalog_index.append((cat_name, cat_date, item))

    # For each installed ZIM, find the best catalog match (longest prefix = exact flavor)
    updates = []
    for inst in installed_files:
        best = None
        best_len = 0
        for cat_name, cat_date, item in catalog_index:
            if inst["filebase"].startswith(cat_name + "_") and cat_date > inst["date"]:
                if len(cat_name) > best_len:
                    best = (cat_name, cat_date, item)
                    best_len = len(cat_name)
        if best:
            _, cat_date, item = best
            updates.append({
                "name": inst["name"],
                "installed_file": inst["filename"],
                "installed_date": inst["date"],
                "latest_date": cat_date,
                "download_url": item.get("download_url", ""),
                "title": item.get("title", ""),
                "size_bytes": item.get("size_bytes", 0),
            })

    return updates


def _download_thread(dl):
    """Background thread that downloads a file via urllib.

    Downloads to a .zim.tmp file first, then atomically renames on completion.
    Supports resuming partial downloads via HTTP Range header.
    """
    tmp_dest = dl["dest"] + ".tmp"
    try:
        # Resume from existing partial download if present
        existing_size = 0
        if os.path.exists(tmp_dest):
            existing_size = os.path.getsize(tmp_dest)
        req = urllib.request.Request(dl["url"], headers={"User-Agent": "Zimi/1.0"})
        if existing_size > 0:
            req.add_header("Range", f"bytes={existing_size}-")
            log.info("Resuming download of %s from %d bytes", dl["filename"], existing_size)
        try:
            resp = urllib.request.urlopen(req, timeout=600, context=SSL_CTX)
        except urllib.error.HTTPError as e:
            if e.code == 416 and existing_size > 0:
                # Range not satisfiable — file already complete, just rename
                os.replace(tmp_dest, dl["dest"])
                dl["done"] = True
                return
            raise
        if resp.status == 206:
            # Partial content — server supports resume
            content_range = resp.headers.get("Content-Range", "")
            # Content-Range: bytes 1234-5678/9999
            try:
                if "/" in content_range:
                    total = int(content_range.split("/")[1])
                else:
                    total = existing_size + int(resp.headers.get("Content-Length", 0))
            except (ValueError, IndexError):
                total = existing_size + int(resp.headers.get("Content-Length", 0))
            dl["total_bytes"] = total
            dl["downloaded_bytes"] = existing_size
            mode = "ab"  # append
        else:
            total = int(resp.headers.get("Content-Length", 0))
            dl["total_bytes"] = total
            existing_size = 0  # server didn't support range, start over
            mode = "wb"
        with open(tmp_dest, mode) as f:
            while not dl.get("cancelled"):
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                dl["downloaded_bytes"] = dl.get("downloaded_bytes", 0) + len(chunk)
        resp.close()
        if dl.get("cancelled"):
            # Keep .tmp file for resume — don't delete partial downloads
            dl["done"] = True
            dl["error"] = "Cancelled"
            return
        # Verify download completed (size matches if known)
        if total > 0:
            actual = os.path.getsize(tmp_dest)
            if actual != total:
                os.remove(tmp_dest)
                dl["done"] = True
                dl["error"] = f"Size mismatch: expected {total}, got {actual}"
                return
        # Atomic rename: tmp → final
        os.replace(tmp_dest, dl["dest"])
        log.info(f"Download complete: {dl['filename']}, refreshing library")
        # Remove older versions of the same ZIM
        base = re.match(r'^(.+?)_\d{4}-\d{2}\.zim$', dl["filename"])
        if base:
            prefix = base.group(1)
            for f in os.listdir(ZIM_DIR):
                if f.startswith(prefix + "_") and f.endswith(".zim") and f != dl["filename"]:
                    try:
                        os.remove(os.path.join(ZIM_DIR, f))
                        log.info(f"Removed old version: {f}")
                    except OSError:
                        pass
        with _zim_lock:
            load_cache(force=True)
        _search_cache_clear()
        _suggest_cache_clear()
        _clean_stale_title_indexes()
        dl["done"] = True
        # Cache ZIM metadata in history so entries survive deletion
        zim_info = {}
        try:
            for z in (_zim_list_cache or []):
                if z.get("file") == dl["filename"]:
                    zim_info = {"title": z.get("title", ""), "name": z.get("name", ""), "has_icon": z.get("has_icon", False)}
                    break
        except Exception:
            pass
        event_type = "updated" if dl.get("is_update") else "download"
        _append_history({"event": event_type, "ts": time.time(), "filename": dl["filename"],
                         "size_bytes": dl.get("total_bytes", 0), **zim_info})
    except Exception as e:
        # Keep .tmp for resume on transient network errors; delete on validation failures
        is_transient = isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError, OSError))
        if not is_transient:
            try:
                os.remove(tmp_dest)
            except OSError:
                pass
        dl["done"] = True
        dl["error"] = str(e)
        if not dl.get("cancelled"):
            _append_history({"event": "download_failed", "ts": time.time(), "filename": dl["filename"],
                             "error": str(e)})


def _start_download(url):
    """Start a background download via urllib. Returns download ID."""
    global _download_counter
    # Validate URL — only allow Kiwix official downloads
    if not url.startswith("https://download.kiwix.org/"):
        return None, "URL must be from download.kiwix.org"

    # OPDS catalog provides .meta4 metalink URLs — strip to get direct .zim URL
    if url.endswith(".meta4"):
        url = url[:-len(".meta4")]

    filename = url.split("/")[-1]
    # Prevent path traversal and validate filename
    filename = os.path.basename(filename)
    if not filename or ".." in filename:
        return None, "Invalid filename in URL"
    if not filename.endswith(".zim"):
        return None, "Only .zim files can be downloaded"
    # Reject filenames with suspicious characters
    if not re.match(r'^[\w.\-]+$', filename):
        return None, "Invalid characters in filename"
    dest = os.path.join(ZIM_DIR, filename)

    # Detect if this replaces an existing ZIM (update vs fresh download)
    name_prefix = re.sub(r'_\d{4}-\d{2}\.zim$', '', filename)
    is_update = any(
        f != filename and f.endswith('.zim') and re.sub(r'_\d{4}-\d{2}\.zim$', '', f) == name_prefix
        for f in os.listdir(ZIM_DIR) if os.path.isfile(os.path.join(ZIM_DIR, f))
    ) if os.path.isdir(ZIM_DIR) else False

    with _download_lock:
        _download_counter += 1
        dl_id = str(_download_counter)
        dl = {
            "id": dl_id,
            "url": url,
            "filename": filename,
            "dest": dest,
            "started": time.time(),
            "done": False,
            "error": None,
            "is_update": is_update,
        }
        _active_downloads[dl_id] = dl
        t = threading.Thread(target=_download_thread, args=(dl,), daemon=True)
        t.start()
    return dl_id, None


def _start_import(url):
    """Start a background download from any HTTPS URL. Returns download ID."""
    global _download_counter
    if not url.startswith("https://"):
        return None, "URL must use HTTPS"

    # Strip query string and fragment before extracting filename
    clean_url = url.split("?")[0].split("#")[0]
    filename = clean_url.split("/")[-1]
    filename = os.path.basename(filename)
    if not filename or ".." in filename:
        return None, "Invalid filename in URL"
    if not filename.endswith(".zim"):
        return None, "Only .zim files can be imported"
    if not re.match(r'^[\w.\-]+$', filename):
        return None, "Invalid characters in filename"
    dest = os.path.join(ZIM_DIR, filename)

    with _download_lock:
        _download_counter += 1
        dl_id = str(_download_counter)
        dl = {
            "id": dl_id,
            "url": url,
            "filename": filename,
            "dest": dest,
            "started": time.time(),
            "done": False,
            "error": None,
            "is_update": False,
        }
        _active_downloads[dl_id] = dl
        t = threading.Thread(target=_download_thread, args=(dl,), daemon=True)
        t.start()
    return dl_id, None


def _get_downloads():
    """Get status of all active/completed downloads."""
    results = []
    with _download_lock:
        to_remove = []
        for dl_id, dl in _active_downloads.items():
            done = dl.get("done", False)
            error = dl.get("error")
            size = 0
            try:
                if os.path.exists(dl["dest"]):
                    size = os.path.getsize(dl["dest"])
            except OSError:
                pass
            total = dl.get("total_bytes", 0)
            downloaded = dl.get("downloaded_bytes", 0)
            pct = round(downloaded / total * 100, 1) if total > 0 else 0
            results.append({
                "id": dl_id,
                "filename": dl["filename"],
                "url": dl["url"],
                "size_bytes": size,
                "total_bytes": total,
                "downloaded_bytes": downloaded,
                "percent": pct,
                "done": done,
                "error": error,
                "elapsed": round(time.time() - dl["started"], 1),
                "is_update": dl.get("is_update", False),
            })
            # Clean up completed downloads older than 1 hour
            if done and (time.time() - dl["started"]) > 3600:
                to_remove.append(dl_id)
        for dl_id in to_remove:
            del _active_downloads[dl_id]
    return results


# ── HTTP API ──

class ZimHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30  # seconds — prevents slow-client DoS on POST bodies

    def do_HEAD(self):
        """Handle HEAD requests (Traefik health checks)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()

    # IPs allowed to set X-Forwarded-For (reverse proxies)
    _TRUSTED_PROXIES = {"127.0.0.1", "::1", "172.17.0.1", "172.18.0.1"}

    def _client_ip(self):
        """Get client IP, respecting X-Forwarded-For only from trusted proxies."""
        direct_ip = self.client_address[0]
        if direct_ip in self._TRUSTED_PROXIES:
            xff = self.headers.get("X-Forwarded-For")
            if xff:
                return xff.split(",")[0].strip()
        return direct_ip

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        def param(key, default=None):
            return params.get(key, [default])[0]

        # Rate limit API endpoints (not UI, static, or manage)
        rate_limited_paths = ("/search", "/read", "/suggest", "/snippet", "/random")
        if parsed.path in rate_limited_paths:
            retry_after = _check_rate_limit(self._client_ip())
            if retry_after > 0:
                with _metrics_lock:
                    _metrics["rate_limited"] += 1
                self.send_response(429)
                self.send_header("Retry-After", str(retry_after))
                self.send_header("Content-Type", "application/json")
                msg = json.dumps({"error": "rate limited", "retry_after": retry_after}).encode()
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return

        try:
            if parsed.path == "/search":
                q = param("q")
                if not q:
                    return self._json(400, {"error": "missing ?q= parameter"})
                try:
                    limit = max(1, min(int(param("limit", "5")), MAX_SEARCH_LIMIT))
                except (ValueError, TypeError):
                    limit = 5
                zim_param = param("zim")
                collection = param("collection")
                # Resolve collection → zim list
                if collection:
                    cdata = _load_collections()
                    coll = cdata.get("collections", {}).get(collection)
                    if not coll:
                        return self._json(400, {"error": f"Collection '{collection}' not found"})
                    filter_zim = coll.get("zims", []) or None
                elif zim_param:
                    filter_zim = [z.strip() for z in zim_param.split(",") if z.strip()]
                    if len(filter_zim) == 1:
                        filter_zim = filter_zim[0]
                else:
                    filter_zim = None
                fast = param("fast") == "1"
                zim_scope_str = ",".join(sorted(filter_zim)) if isinstance(filter_zim, list) else (filter_zim or "")
                cache_key = (q.lower().strip(), zim_scope_str, limit, fast)
                cached = _search_cache_get(cache_key)
                if cached is not None:
                    _record_metric("/search", 0)
                    _record_usage("search")
                    return self._json(200, cached)
                t0 = time.time()
                if fast:
                    # Fast path uses _suggest_pool internally, no _zim_lock needed
                    result = search_all(q, limit=limit, filter_zim=filter_zim, fast=True)
                else:
                    # FTS path uses _fts_pool (per-ZIM locks), no _zim_lock needed
                    result = search_all(q, limit=limit, filter_zim=filter_zim)
                dt = time.time() - t0
                _search_cache_put(cache_key, result)
                _record_metric("/search", dt)
                _record_usage("search")
                zim_label = ",".join(filter_zim) if isinstance(filter_zim, list) else (filter_zim or "all")
                log.info("search q=%r limit=%d zim=%s fast=%s %.1fs", q, limit, zim_label, fast, dt)
                return self._json(200, result)

            elif parsed.path == "/read":
                zim = param("zim")
                path = param("path")
                if not zim or not path:
                    return self._json(400, {"error": "missing ?zim= and ?path= parameters"})
                try:
                    max_len = min(int(param("max_length", str(MAX_CONTENT_LENGTH))), READ_MAX_LENGTH)
                except ValueError:
                    max_len = MAX_CONTENT_LENGTH
                t0 = time.time()
                with _zim_lock:
                    result = read_article(zim, path, max_length=max_len)
                _record_metric("/read", time.time() - t0)
                _record_usage("read", zim)
                return self._json(200, result)

            elif parsed.path == "/suggest":
                q = param("q")
                if not q:
                    return self._json(400, {"error": "missing ?q= parameter"})
                try:
                    limit = max(1, min(int(param("limit", "10")), MAX_SEARCH_LIMIT))
                except (ValueError, TypeError):
                    limit = 10
                zim_param = param("zim")
                collection = param("collection")
                # Resolve collection → zim list
                if collection:
                    cdata = _load_collections()
                    coll = cdata.get("collections", {}).get(collection)
                    zim_names = coll.get("zims", []) if coll else None
                elif zim_param:
                    zim_names = [z.strip() for z in zim_param.split(",") if z.strip()]
                else:
                    zim_names = None
                t0 = time.time()
                # Use the fast search path (parallel, FTS5 title indexes)
                # then reformat to suggest's {zim: [{path, title}, ...]} shape
                filter_zim = ",".join(zim_names) if zim_names else None
                search_result = search_all(q, fast=True, limit=limit, filter_zim=filter_zim)
                result = {}
                for r in search_result.get("results", []):
                    zn = r["zim"]
                    if zn not in result:
                        result[zn] = []
                    result[zn].append({"path": r["path"], "title": r["title"]})
                _record_metric("/suggest", time.time() - t0)
                return self._json(200, result)

            elif parsed.path == "/list":
                result = list_zims()
                return self._json(200, result)

            elif parsed.path == "/catalog":
                zim = param("zim")
                if not zim:
                    return self._json(400, {"error": "missing ?zim= parameter"})
                with _zim_lock:
                    result = get_catalog(zim)
                return self._json(200, result)

            elif parsed.path == "/snippet":
                zim = param("zim")
                path = param("path")
                if not zim or not path:
                    return self._json(400, {"error": "missing ?zim= and ?path= parameters"})
                t0 = time.time()
                snippet = ""
                thumbnail = None
                with _zim_lock:
                    archive = get_archive(zim)
                    if archive is None:
                        return self._json(404, {"error": f"ZIM '{zim}' not found"})
                    try:
                        entry = archive.get_entry_by_path(path)
                        item = entry.get_item()
                        if item.size > MAX_CONTENT_BYTES:
                            _record_metric("/snippet", time.time() - t0)
                            return self._json(200, {"snippet": ""})
                        # Read first 15KB — enough for <head> meta tags + initial content
                        raw = bytes(item.content)[:15360]
                        text = raw.decode("UTF-8", errors="replace")
                        # Prefer meta description (skips nav/header boilerplate)
                        for desc_pat in [
                            r'<meta\s+(?:name|property)=["\'](?:og:)?description["\']\s+content=["\']([^"\']{20,})["\']',
                            r'<meta\s+content=["\']([^"\']{20,})["\']\s+(?:name|property)=["\'](?:og:)?description["\']',
                        ]:
                            desc_m = re.search(desc_pat, text[:8000], re.IGNORECASE)
                            if desc_m:
                                snippet = strip_html(desc_m.group(1))[:300].strip()
                                break
                        # Fallback: extract from <main> or <article> body (skip nav boilerplate)
                        if not snippet:
                            for tag in ['main', 'article']:
                                tag_m = re.search(r'<' + tag + r'[\s>]', text, re.IGNORECASE)
                                if tag_m:
                                    plain = strip_html(text[tag_m.start():])
                                    snippet = plain[:300].strip()
                                    break
                        # Last resort: full page text
                        if not snippet:
                            snippet = strip_html(text)[:300].strip()
                        # Lightweight thumbnail: og:image / twitter:image from <head>
                        for img_pat in [
                            r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
                            r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:image["\']',
                            r'<meta\s+name=["\']twitter:image["\']\s+content=["\']([^"\']+)["\']',
                            r'<meta\s+content=["\']([^"\']+)["\']\s+name=["\']twitter:image["\']',
                        ]:
                            img_m = re.search(img_pat, text[:8000], re.IGNORECASE)
                            if img_m:
                                src = img_m.group(1)
                                if not src.startswith(("http", "//", "data:")) and not src.lower().endswith(".svg"):
                                    resolved = _resolve_img_path(archive, path, src)
                                    if resolved:
                                        thumbnail = f"/w/{zim}/{resolved}"
                                        break
                        # Fallback: best <img> in content — skip icons/badges, prefer larger images
                        if not thumbnail:
                            _skip_img = re.compile(r'icon|badge|logo|arrow|button|sprite|spacer|1x1|pixel|emoji|flag.*\.svg', re.IGNORECASE)
                            best_img = None
                            best_area = 0
                            for img_m2 in re.finditer(r'<img\b([^>]*)>', text[:15000], re.IGNORECASE):
                                attrs = img_m2.group(1)
                                src_m = re.search(r'src=["\']([^"\']+)["\']', attrs)
                                if not src_m:
                                    continue
                                src = src_m.group(1)
                                if src.startswith(("data:", "http", "//")) or src.lower().endswith(".svg"):
                                    continue
                                if _skip_img.search(src) or _skip_img.search(attrs):
                                    continue
                                w_m = re.search(r'width=["\']?(\d+)', attrs)
                                h_m = re.search(r'height=["\']?(\d+)', attrs)
                                w = int(w_m.group(1)) if w_m else 0
                                h = int(h_m.group(1)) if h_m else 0
                                # Skip explicitly tiny images
                                if (w > 0 and w < 60) or (h > 0 and h < 40):
                                    continue
                                area = (w or 200) * (h or 150)
                                if area > best_area:
                                    resolved = _resolve_img_path(archive, path, src)
                                    if resolved:
                                        best_img = f"/w/{zim}/{resolved}"
                                        best_area = area
                                        if area >= 200 * 150:
                                            break  # Good enough — stop scanning
                            if best_img:
                                thumbnail = best_img
                    except (KeyError, Exception):
                        pass
                _record_metric("/snippet", time.time() - t0)
                result = {"snippet": snippet}
                if thumbnail:
                    result["thumbnail"] = thumbnail
                return self._json(200, result)

            elif parsed.path == "/collections":
                data = _load_collections()
                return self._json(200, data)

            elif parsed.path == "/health":
                zim_count = len(get_zim_files())
                return self._json(200, {"status": "ok", "version": ZIMI_VERSION, "zim_count": zim_count, "pdf_support": HAS_PYMUPDF})

            elif parsed.path == "/random":
                zim = param("zim")  # optional: scope to specific ZIM
                if zim:
                    if zim not in get_zim_files():
                        return self._json(404, {"error": f"ZIM '{zim}' not found"})
                    pick_name = zim
                else:
                    eligible = [z for z in (_zim_list_cache or []) if isinstance(z.get("entries"), int) and z["entries"] > 100]
                    if not eligible:
                        return self._json(200, {"error": "no ZIMs available"})
                    pick_name = _random.choice(eligible)["name"]
                want_thumb = param("thumb") == "1"
                require_thumb = param("require_thumb") == "1"
                is_wiktionary = "wiktionary" in pick_name.lower()
                max_tries = 50 if is_wiktionary else (5 if require_thumb else 1)
                t0 = time.time()
                with _zim_lock:
                    archive = get_archive(pick_name)
                    if archive is None:
                        return self._json(200, {"error": "archive not available"})
                date_param = param("date")  # MMDD format
                seed_param = param("seed")  # For deterministic daily picks
                rng = None
                if seed_param:
                    import hashlib
                    seed_val = int(hashlib.md5((pick_name + seed_param).encode()).hexdigest()[:8], 16)
                    rng = _random.Random(seed_val)
                best_result = None
                best_preview = None
                for _try in range(max_tries):
                    with _zim_lock:
                        result = None
                        if date_param and len(date_param) == 4 and _try == 0:
                            result = _get_dated_entry(archive, pick_name, date_param, rng=rng)
                        if not result:
                            result = random_entry(archive, rng=rng)
                        if not result:
                            continue
                        preview = None
                        if want_thumb:
                            preview = _extract_preview(archive, pick_name, result["path"])
                    # Skip non-English or boring wiktionary entries (retry)
                    if is_wiktionary and preview and (preview.get("non_english") or preview.get("boring")):
                        if best_result is None:
                            best_result = result
                            best_preview = preview
                        continue
                    # Wiktionary: accept interesting English entry even without thumbnail
                    if is_wiktionary and preview and not preview.get("non_english") and not preview.get("boring"):
                        best_result = result
                        best_preview = preview
                        break
                    if not require_thumb or (preview and preview["thumbnail"]):
                        best_result = result
                        best_preview = preview
                        break
                    # Keep first result as fallback even without image
                    if best_result is None:
                        best_result = result
                        best_preview = preview
                if not best_result:
                    return self._json(200, {"error": "no articles found"})
                dt = time.time() - t0
                chosen = {"zim": pick_name, "path": best_result["path"], "title": best_result["title"]}
                if best_preview:
                    # Use extracted title if the entry title looks like a slug
                    if best_preview.get("title"):
                        chosen["title"] = best_preview["title"]
                    if best_preview["thumbnail"]:
                        chosen["thumbnail"] = best_preview["thumbnail"]
                    if best_preview["blurb"]:
                        chosen["blurb"] = best_preview["blurb"]
                    if best_preview.get("attribution"):
                        chosen["attribution"] = best_preview["attribution"]
                    if best_preview.get("speaker"):
                        chosen["speaker"] = best_preview["speaker"]
                    if best_preview.get("author"):
                        chosen["author"] = best_preview["author"]
                    if best_preview.get("part_of_speech"):
                        chosen["part_of_speech"] = best_preview["part_of_speech"]
                # XKCD date lookup from archive page (available for clients that want it)
                # Must hold _zim_lock — _xkcd_date_lookup reads ZIM entries via libzim C API
                if "xkcd" in pick_name.lower() and param("with_date") == "1":
                    with _zim_lock:
                        xkcd_date = _xkcd_date_lookup(archive, best_result["path"])
                    if xkcd_date:
                        chosen["date"] = xkcd_date
                _record_metric("/random", dt)
                log.info("random zim=%s title=%r %.1fs", pick_name, best_result["title"], dt)
                return self._json(200, chosen)

            elif parsed.path == "/resolve":
                # Cross-ZIM URL resolution: given an external URL, find matching ZIM + path
                # Also serves the domain map when ?domains=1 is set
                if param("domains") == "1":
                    return self._json(200, _domain_zim_map)
                url_param = param("url")
                if not url_param:
                    return self._json(400, {"error": "missing ?url= parameter"})
                with _zim_lock:
                    result = _resolve_url_to_zim(url_param)
                if result:
                    # Track cross-ZIM reference if source ZIM provided
                    from_zim = param("from")
                    if from_zim and from_zim != result["zim"]:
                        key = (from_zim, result["zim"])
                        with _xzim_refs_lock:
                            _xzim_refs[key] = _xzim_refs.get(key, 0) + 1
                    return self._json(200, {"found": True, **result})
                return self._json(200, {"found": False})

            elif parsed.path.startswith("/manage/"):
                if not ZIMI_MANAGE:
                    return self._json(404, {"error": "Library management is disabled. Set ZIMI_MANAGE=1 to enable."})
                # has-password is public so the UI knows whether to prompt
                if parsed.path == "/manage/has-password":
                    return self._json(200, {"has_password": bool(_get_manage_password_hash())})
                if _check_manage_auth(self):
                    return self._json(401, {"error": "unauthorized", "needs_password": True})

                if parsed.path == "/manage/status":
                    zim_count = len(get_zim_files())
                    total_gb = sum(z.get("size_gb", 0) for z in (_zim_list_cache or []))
                    linked_zims = len(set(_domain_zim_map.values()))
                    return self._json(200, {
                        "zim_count": zim_count,
                        "total_size_gb": round(total_gb, 1),
                        "manage_enabled": True,
                        "linked_zims": linked_zims,
                        "domain_count": len(_domain_zim_map),
                        "auto_update": {
                            "enabled": _auto_update_enabled,
                            "frequency": _auto_update_freq,
                            "locked": _auto_update_env_locked,
                        },
                    })

                elif parsed.path == "/manage/stats":
                    metrics = _get_metrics()
                    disk = _get_disk_usage()
                    auto_update = {
                        "enabled": _auto_update_enabled,
                        "frequency": _auto_update_freq,
                        "last_check": _auto_update_last_check,
                    }
                    title_index = _get_title_index_stats()
                    with _xzim_refs_lock:
                        xzim_refs = sorted(
                            [{"from": k[0], "to": k[1], "count": v} for k, v in _xzim_refs.items()],
                            key=lambda x: x["count"], reverse=True
                        )
                    linked_zims = len(set(_domain_zim_map.values()))
                    zim_count = len(get_zim_files())
                    return self._json(200, {"metrics": metrics, "disk": disk, "auto_update": auto_update, "title_index": title_index, "cross_zim_refs": xzim_refs, "linked_zims": linked_zims, "zim_count": zim_count, "domain_count": len(_domain_zim_map)})

                elif parsed.path == "/manage/usage":
                    return self._json(200, _get_usage_stats())

                elif parsed.path == "/manage/catalog":
                    query = param("q", "")
                    lang = param("lang", "")
                    try:
                        count = min(int(param("count", "20")), 500)
                    except (ValueError, TypeError):
                        count = 20
                    try:
                        start = max(int(param("start", "0")), 0)
                    except (ValueError, TypeError):
                        start = 0
                    total, items, err = _fetch_kiwix_catalog(query, lang, count, start)
                    if err:
                        return self._json(502, {"error": f"Kiwix catalog fetch failed: {err}"})
                    return self._json(200, {"total": total, "items": items})

                elif parsed.path == "/manage/check-updates":
                    updates = _check_updates()
                    return self._json(200, {"updates": updates, "count": len(updates)})

                elif parsed.path == "/manage/downloads":
                    return self._json(200, {"downloads": _get_downloads()})

                elif parsed.path == "/manage/history":
                    return self._json(200, {"history": _load_history()})

                else:
                    return self._json(404, {"error": "not found"})

            elif parsed.path.startswith("/static/"):
                return self._serve_static(parsed.path[8:])  # strip "/static/"

            elif parsed.path in ("/favicon.ico", "/favicon.png"):
                return self._serve_favicon()

            elif parsed.path == "/apple-touch-icon.png":
                return self._serve_apple_touch_icon()

            elif parsed.path == "/":
                return self._serve_index()

            elif parsed.path.startswith("/w/"):
                # /w/<zim_name>/<entry_path> — serve raw ZIM content
                rest = parsed.path[3:]  # strip "/w/"
                slash = rest.find("/")
                if slash == -1:
                    zim_name, entry_path = unquote(rest), ""
                else:
                    zim_name = unquote(rest[:slash])
                    entry_path = unquote(rest[slash + 1:])
                # Top-level browser navigation (reload/bookmark) → serve SPA shell
                # so client-side router can handle the deep link.
                # ?raw=1 bypasses SPA shell (used for PDF new-tab opening).
                # ?view=1 forces SPA shell (used in pushState URLs for PDFs so CDN
                # caching of the raw PDF doesn't break reload).
                qs = parse_qs(parsed.query)
                is_raw = "raw" in qs
                is_view = "view" in qs
                fetch_dest = self.headers.get("Sec-Fetch-Dest", "")
                if is_view or ((fetch_dest == "document" or not entry_path) and not is_raw and not entry_path.lower().endswith(".epub")):
                    return self._serve_index(vary="Sec-Fetch-Dest")
                # Track iframe article loads
                if fetch_dest == "iframe":
                    _record_usage("iframe", zim_name)
                return self._serve_zim_content(zim_name, entry_path)

            else:
                return self._json(404, {"error": "not found", "endpoints": ["/search", "/read", "/suggest", "/list", "/catalog", "/health", "/w/"]})

        except Exception as e:
            traceback.print_exc()
            return self._json(500, {"error": str(e)})

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            content_len = int(self.headers.get("Content-Length", "0"))
            if content_len > MAX_POST_BODY:
                return self._json(413, {"error": f"Request body too large (max {MAX_POST_BODY} bytes)"})
            body = self.rfile.read(content_len) if content_len > 0 else b"{}"
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = {}

            if parsed.path.startswith("/manage/"):
                if not ZIMI_MANAGE:
                    return self._json(404, {"error": "Library management is disabled."})
                # set-password: special — requires current password if one exists
                if parsed.path == "/manage/set-password":
                    stored = _get_manage_password_hash()
                    if stored:
                        cur = data.get("current", "")
                        if not cur or _hash_pw(cur) != stored:
                            return self._json(401, {"error": "Current password is incorrect"})
                    new_pw = data.get("password", "").strip()
                    _set_manage_password(new_pw)
                    return self._json(200, {"status": "password set" if new_pw else "password cleared"})
                if _check_manage_auth(self):
                    return self._json(401, {"error": "unauthorized", "needs_password": True})

            if parsed.path == "/resolve":
                retry_after = _check_rate_limit(self._client_ip())
                if retry_after > 0:
                    with _metrics_lock:
                        _metrics["rate_limited"] += 1
                    return self._json(429, {"error": "rate limited", "retry_after": retry_after})
                # Batch cross-ZIM URL resolution: POST {"urls": [...]} → {"results": {...}}
                urls = data.get("urls", [])
                if not isinstance(urls, list) or len(urls) > 100:
                    return self._json(400, {"error": "'urls' must be a list (max 100)"})
                results = {}
                with _zim_lock:
                    for url_str in urls:
                        if not isinstance(url_str, str):
                            continue
                        resolved = _resolve_url_to_zim(url_str)
                        if resolved:
                            results[url_str] = {"found": True, "zim": resolved["zim"], "path": resolved["path"]}
                        else:
                            results[url_str] = {"found": False}
                return self._json(200, {"results": results})

            elif parsed.path == "/collections":
                # Auth: only enforce password when manage mode is on (collections are
                # user-facing features that work without manage mode enabled)
                if ZIMI_MANAGE and _check_manage_auth(self):
                    return self._json(401, {"error": "unauthorized", "needs_password": True})
                name = data.get("name", "").strip()[:64]
                label = data.get("label", "").strip()[:128]
                # Auto-generate name from label if not provided
                if not name and label:
                    name = re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')[:64]
                if not name:
                    return self._json(400, {"error": "missing 'name' or 'label' field"})
                if not label:
                    label = name
                zim_list = data.get("zims", [])
                if not isinstance(zim_list, list) or len(zim_list) > 200:
                    return self._json(400, {"error": "'zims' must be a list (max 200 items)"})
                with _collections_lock:
                    cdata = _load_collections()
                    cdata["collections"][name] = {"label": label or name, "zims": zim_list}
                    _save_collections(cdata)
                return self._json(200, {"status": "ok", "collection": name})

            elif parsed.path == "/favorites":
                # Auth: same as collections — only when manage mode is on
                if ZIMI_MANAGE and _check_manage_auth(self):
                    return self._json(401, {"error": "unauthorized", "needs_password": True})
                zim_name = data.get("zim", "").strip()
                if not zim_name:
                    return self._json(400, {"error": "missing 'zim' field"})
                if zim_name not in get_zim_files():
                    return self._json(400, {"error": f"ZIM '{zim_name}' not found"})
                with _collections_lock:
                    cdata = _load_collections()
                    favs = cdata.get("favorites", [])
                    if zim_name in favs:
                        favs.remove(zim_name)
                        action = "removed"
                    elif len(favs) >= 100:
                        return self._json(400, {"error": "Favorites list is full (max 100)"})
                    else:
                        favs.append(zim_name)
                        action = "added"
                    cdata["favorites"] = favs
                    _save_collections(cdata)
                return self._json(200, {"status": action, "zim": zim_name, "favorites": cdata["favorites"]})

            elif parsed.path == "/manage/download" and ZIMI_MANAGE:
                url = data.get("url", "")
                if not url:
                    return self._json(400, {"error": "missing 'url' in request body"})
                dl_id, err = _start_download(url)
                if err:
                    return self._json(400, {"error": err})
                return self._json(200, {"status": "started", "id": dl_id})

            elif parsed.path == "/manage/import" and ZIMI_MANAGE:
                url = data.get("url", "")
                if not url:
                    return self._json(400, {"error": "missing 'url' in request body"})
                dl_id, err = _start_import(url)
                if err:
                    return self._json(400, {"error": err})
                return self._json(200, {"status": "started", "id": dl_id})

            elif parsed.path == "/manage/cancel" and ZIMI_MANAGE:
                dl_id = data.get("id", "")
                with _download_lock:
                    dl = _active_downloads.get(dl_id)
                    if not dl:
                        return self._json(404, {"error": "Download not found"})
                    if dl.get("done"):
                        return self._json(400, {"error": "Download already finished"})
                    dl["cancelled"] = True
                return self._json(200, {"status": "cancelling", "id": dl_id})

            elif parsed.path == "/manage/clear-downloads" and ZIMI_MANAGE:
                with _download_lock:
                    to_remove = [k for k, v in _active_downloads.items() if v.get("done")]
                    for k in to_remove:
                        del _active_downloads[k]
                return self._json(200, {"status": "cleared", "removed": len(to_remove)})

            elif parsed.path == "/manage/refresh" and ZIMI_MANAGE:
                # Re-scan ZIM directory and rebuild cache without full restart
                log.info("Library refresh triggered")
                with _zim_lock:
                    load_cache(force=True)
                    count = len(_zim_list_cache or [])
                _search_cache_clear()
                _suggest_cache_clear()
                _clean_stale_title_indexes()
                return self._json(200, {"status": "refreshed", "zim_count": count})

            elif parsed.path == "/manage/build-fts" and ZIMI_MANAGE:
                zim_name = data.get("name", "")
                if not zim_name:
                    return self._json(400, {"error": "Missing 'name' parameter"})
                try:
                    result = _build_fts_for_index(zim_name)
                    return self._json(200, result)
                except FileNotFoundError as e:
                    return self._json(404, {"error": str(e)})
                except Exception as e:
                    return self._json(500, {"error": f"FTS5 build failed: {e}"})

            elif parsed.path == "/manage/delete" and ZIMI_MANAGE:
                filename = data.get("filename", "")
                if not filename or ".." in filename or "/" in filename:
                    return self._json(400, {"error": "Invalid filename"})
                if not filename.endswith(".zim"):
                    return self._json(400, {"error": "Only .zim files can be deleted"})
                filepath = os.path.join(ZIM_DIR, filename)
                if not os.path.exists(filepath):
                    return self._json(404, {"error": f"File not found: {filename}"})
                try:
                    file_size = 0
                    try:
                        file_size = os.path.getsize(filepath)
                    except OSError:
                        pass
                    # Cache ZIM info before deletion so history shows proper title/icon
                    zim_info = {}
                    try:
                        for z in (_zim_list_cache or []):
                            if z.get("file") == filename:
                                zim_info = {"title": z.get("title", ""), "name": z.get("name", ""), "has_icon": z.get("has_icon", False)}
                                break
                    except Exception:
                        pass
                    os.remove(filepath)
                    log.info(f"Deleted ZIM: {filename}")
                    _append_history({"event": "deleted", "ts": time.time(), "filename": filename,
                                     "size_bytes": file_size, **zim_info})
                    with _zim_lock:
                        load_cache(force=True)
                    _search_cache_clear()
                    _suggest_cache_clear()
                    _clean_stale_title_indexes()
                    return self._json(200, {"status": "deleted", "filename": filename})
                except OSError as e:
                    return self._json(500, {"error": f"Failed to delete: {e}"})

            elif parsed.path == "/manage/update" and ZIMI_MANAGE:
                # Trigger manual update: check for updates and start downloads
                updates = _check_updates()
                started = []
                for upd in updates:
                    url = upd.get("download_url")
                    if url:
                        dl_id, err = _start_download(url)
                        if not err:
                            started.append({"name": upd.get("name", "?"), "id": dl_id})
                return self._json(200, {"status": "started", "count": len(started), "downloads": started})

            elif parsed.path == "/manage/auto-update" and ZIMI_MANAGE:
                if _auto_update_env_locked:
                    return self._json(403, {"error": "Auto-update is controlled by ZIMI_AUTO_UPDATE env var"})
                global _auto_update_enabled, _auto_update_freq, _auto_update_thread
                enabled = data.get("enabled", _auto_update_enabled)
                freq = data.get("frequency", _auto_update_freq)
                if freq not in _FREQ_SECONDS:
                    return self._json(400, {"error": f"Invalid frequency. Use: {', '.join(_FREQ_SECONDS.keys())}"})
                _auto_update_freq = freq
                if enabled and not _auto_update_enabled:
                    _auto_update_enabled = True
                    if _auto_update_thread and _auto_update_thread.is_alive():
                        log.info("Auto-update thread still running, reusing it")
                    else:
                        _auto_update_thread = threading.Thread(
                            target=_auto_update_loop, kwargs={"initial_delay": 30}, daemon=True)
                        _auto_update_thread.start()
                    log.info("Auto-update enabled: %s (first check in 30s)", freq)
                elif not enabled and _auto_update_enabled:
                    _auto_update_enabled = False
                    log.info("Auto-update disabled")
                _save_auto_update_config(_auto_update_enabled, _auto_update_freq)
                return self._json(200, {"enabled": _auto_update_enabled, "frequency": _auto_update_freq})

            else:
                return self._json(404, {"error": "not found"})

        except Exception as e:
            traceback.print_exc()
            return self._json(500, {"error": str(e)})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        # Rate limit write endpoints
        retry_after = _check_rate_limit(self._client_ip())
        if retry_after > 0:
            with _metrics_lock:
                _metrics["rate_limited"] += 1
            return self._json(429, {"error": "rate limited", "retry_after": retry_after})
        try:
            if parsed.path == "/collections":
                name = params.get("name", [None])[0]
                if not name:
                    return self._json(400, {"error": "missing ?name= parameter"})
                if ZIMI_MANAGE and _check_manage_auth(self):
                    return self._json(401, {"error": "unauthorized", "needs_password": True})
                with _collections_lock:
                    cdata = _load_collections()
                    if name not in cdata.get("collections", {}):
                        return self._json(404, {"error": f"Collection '{name}' not found"})
                    del cdata["collections"][name]
                    _save_collections(cdata)
                return self._json(200, {"status": "deleted", "collection": name})
            else:
                return self._json(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            return self._json(500, {"error": str(e)})

    def _serve_zim_icon(self, zim_name, archive):
        """Serve the ZIM's 48x48 illustration as a PNG."""
        try:
            icon_data = bytes(archive.get_metadata("Illustration_48x48@1"))
        except Exception:
            self.send_response(404)
            self.end_headers()
            return
        etag = f'"icon-{zim_name}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Cache-Control", "public, max-age=604800, immutable")
        self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(icon_data)))
        self.end_headers()
        self.wfile.write(icon_data)

    def _serve_zim_content(self, zim_name, entry_path):
        """Serve raw ZIM content with correct MIME type for the /w/ endpoint.

        Manages _zim_lock internally — holds lock only during libzim reads,
        releases before writing to the socket (important for large video streams).
        """
        # Phase 1: Read from ZIM under lock
        with _zim_lock:
            archive = get_archive(zim_name)
            if archive is None:
                return self._json(404, {"error": f"ZIM '{zim_name}' not found"})

            # Serve ZIM icon from metadata
            if entry_path == "-/icon":
                return self._serve_zim_icon(zim_name, archive)

            try:
                entry = archive.get_entry_by_path(entry_path)
            except KeyError:
                entry = None
            if entry is None:
                # Old namespace fallback: try stripping or adding A/, I/, C/, -/ prefixes
                for alt in _namespace_fallbacks(entry_path):
                    try:
                        entry = archive.get_entry_by_path(alt)
                        break
                    except KeyError:
                        continue
            if entry is None:
                return self._json(404, {"error": f"Entry '{entry_path}' not found in {zim_name}"})

            # ZIM redirects → HTTP 302 so browser URL updates to canonical path
            if entry.is_redirect:
                target = entry.get_redirect_entry()
                target_path = target.path
                self.send_response(302)
                self.send_header("Location", f"/w/{zim_name}/{target_path}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            item = entry.get_item()
            total_size = item.size
            mimetype = item.mimetype or ""

            ext = os.path.splitext(entry_path)[1].lower()
            if not mimetype:
                mimetype = MIME_FALLBACK.get(ext, "application/octet-stream")
            # Bare MIME fix: some ZIMs store "mp4" instead of "video/mp4"
            if mimetype and "/" not in mimetype:
                guessed = MIME_FALLBACK.get("." + mimetype.lower())
                mimetype = guessed if guessed else "application/octet-stream"
            # Fix ZIM packaging bugs: media files stored with wrong mimetype (e.g. text/html)
            # Trust the file extension for known media/binary types over the ZIM metadata
            ext_mime = MIME_FALLBACK.get(ext)
            if ext_mime and mimetype == "text/html" and ext not in (".html", ".htm"):
                mimetype = ext_mime
            # Force EPUB download (browsers can't render EPUB inline)
            is_epub = entry_path.lower().endswith(".epub") or mimetype in ("application/epub+zip", "application/epub")
            if is_epub:
                mimetype = "application/epub+zip"
                epub_filename = os.path.basename(entry_path)
                if not epub_filename.endswith(".epub"):
                    epub_filename += ".epub"
                self.send_response(200)
                self.send_header("Content-Type", mimetype)
                self.send_header("Content-Length", str(total_size))
                self.send_header("Content-Disposition", f'attachment; filename="{epub_filename}"')
                self.end_headers()
                self.wfile.write(bytes(item.content))
                return

            # Streamable types support Range requests (no size limit)
            is_streamable = any(mimetype.startswith(t) for t in ("video/", "audio/", "application/ogg"))

            range_start = range_end = None
            if is_streamable:
                range_header = self.headers.get("Range")
                if range_header:
                    range_start, range_end = self._parse_range(range_header, total_size)
                if range_start is not None and range_end is not None:
                    content = bytes(item.content[range_start:range_end + 1])
                else:
                    content = bytes(item.content)
            else:
                if total_size > MAX_SERVE_BYTES:
                    self.send_response(413)
                    self.send_header("Content-Type", "text/plain")
                    msg = f"Entry too large ({total_size // (1024*1024)} MB). Max: {MAX_SERVE_BYTES // (1024*1024)} MB.".encode()
                    self.send_header("Content-Length", str(len(msg)))
                    self.end_headers()
                    self.wfile.write(msg)
                    return
                content = bytes(item.content)
        # Lock released — safe to do slow I/O

        # Strip <base> tags from HTML
        if mimetype.startswith("text/html"):
            text = content.decode("UTF-8", errors="replace")
            text = re.sub(r'<base\s[^>]*>', '', text, flags=re.IGNORECASE)
            content = text.encode("UTF-8")

        # ETag for caching
        etag = '"' + hashlib.md5(f"{zim_name}/{entry_path}".encode()).hexdigest()[:16] + '"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.end_headers()
            return

        if range_start is not None and range_end is not None:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {range_start}-{range_end}/{total_size}")
        else:
            self.send_response(200)

        self.send_header("Content-Type", mimetype)
        self.send_header("Cache-Control", "public, max-age=86400, immutable")
        self.send_header("Vary", "Sec-Fetch-Dest")
        self.send_header("ETag", etag)

        if is_streamable:
            self.send_header("Accept-Ranges", "bytes")

        # Sandbox ZIM HTML: allow inline styles/scripts (ZIM content uses them)
        # but block external requests and prevent framing outside Zimi
        if mimetype.startswith("text/html"):
            self.send_header("Content-Security-Policy",
                "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; "
                "frame-ancestors 'self'")

        # Gzip text-based content only (images/PDFs are already compressed)
        compressible = any(mimetype.startswith(t) or mimetype == t for t in COMPRESSIBLE_TYPES)
        if compressible and self._accepts_gzip() and len(content) > 256:
            content = gzip.compress(content, compresslevel=4)
            self.send_header("Content-Encoding", "gzip")

        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _accepts_gzip(self):
        return "gzip" in self.headers.get("Accept-Encoding", "")

    @staticmethod
    def _parse_range(header, total_size):
        """Parse HTTP Range header. Returns (start, end) or (None, None)."""
        if not header or not header.startswith("bytes="):
            return None, None
        range_spec = header[6:].strip()
        if "," in range_spec:
            return None, None  # multi-range not supported
        if range_spec.startswith("-"):
            # Suffix range: last N bytes
            suffix = int(range_spec[1:])
            start = max(0, total_size - suffix)
            return start, total_size - 1
        parts = range_spec.split("-", 1)
        start = int(parts[0])
        end = int(parts[1]) if parts[1] else total_size - 1
        end = min(end, total_size - 1)
        if start > end or start >= total_size:
            return None, None
        return start, end

    def _send(self, code, body_bytes, content_type, vary=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        if vary:
            self.send_header("Vary", vary)
        if self._accepts_gzip() and len(body_bytes) > 256:
            body_bytes = gzip.compress(body_bytes, compresslevel=4)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    # ── Static file serving ──
    # In-memory cache for static files (vendor files like pdf.js are immutable)
    _static_cache = {}

    @staticmethod
    def _static_base_dir():
        """Resolve the static/ directory, checking PyInstaller bundle first."""
        candidates = [
            os.path.join(getattr(sys, '_MEIPASS', ''), "static") if getattr(sys, '_MEIPASS', None) else "",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"),
        ]
        for d in candidates:
            if d and os.path.isdir(d):
                return d
        return None

    def _serve_static(self, rel_path):
        """Serve a file from the static/ directory with caching and security."""
        # Path traversal protection
        if not rel_path or ".." in rel_path.split("/"):
            return self._json(400, {"error": "invalid path"})
        # Normalize and reject absolute paths
        rel_path = rel_path.lstrip("/")
        if os.path.isabs(rel_path):
            return self._json(400, {"error": "invalid path"})

        # Check cache first, then read from disk
        cached = ZimHandler._static_cache.get(rel_path)
        if cached:
            body, content_type = cached
        else:
            base = ZimHandler._static_base_dir()
            if not base:
                return self._json(404, {"error": "static directory not found"})
            file_path = os.path.normpath(os.path.join(base, rel_path))
            # Ensure resolved path is still inside the static dir
            if not file_path.startswith(os.path.normpath(base) + os.sep) and file_path != os.path.normpath(base):
                return self._json(403, {"error": "forbidden"})
            if not os.path.isfile(file_path):
                return self._json(404, {"error": "not found"})
            ext = os.path.splitext(file_path)[1].lower()
            content_type = MIME_FALLBACK.get(ext, "application/octet-stream")
            with open(file_path, "rb") as f:
                body = f.read()
            # Cache in memory (vendor files are immutable, ~8MB total for pdf.js)
            ZimHandler._static_cache[rel_path] = (body, content_type)

        # Compress text-based static files (viewer.mjs, viewer.css, etc.)
        ct_base = content_type.split(";")[0]
        compressible = any(ct_base.startswith(t) or ct_base == t for t in COMPRESSIBLE_TYPES)
        if self._accepts_gzip() and compressible and len(body) > 256:
            body = gzip.compress(body, compresslevel=4)
            is_gzipped = True
        else:
            is_gzipped = False
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("Access-Control-Allow-Origin", "*")
        if is_gzipped:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(body)

    _favicon_data = None

    def _serve_favicon(self):
        if ZimHandler._favicon_data is None:
            # Serve 32x32 favicon for browser tabs (not the full 256px icon)
            icon_paths = [
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "favicon.png"),
                os.path.join(getattr(sys, '_MEIPASS', ''), "assets", "favicon.png") if getattr(sys, '_MEIPASS', None) else "",
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon.png"),
                os.path.join(getattr(sys, '_MEIPASS', ''), "assets", "icon.png") if getattr(sys, '_MEIPASS', None) else "",
            ]
            for p in icon_paths:
                if p and os.path.exists(p):
                    with open(p, "rb") as f:
                        ZimHandler._favicon_data = f.read()
                    break
            if not ZimHandler._favicon_data:
                # Fallback: extract from HTML template's base64 data URI
                import re as _re
                m = _re.search(r'data:image/png;base64,([A-Za-z0-9+/=]+)', SEARCH_UI_HTML)
                ZimHandler._favicon_data = base64.b64decode(m.group(1)) if m else b''
        if not ZimHandler._favicon_data:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(ZimHandler._favicon_data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(ZimHandler._favicon_data)

    _apple_touch_icon_data = None

    def _serve_apple_touch_icon(self):
        if ZimHandler._apple_touch_icon_data is None:
            icon_paths = [
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "apple-touch-icon.png"),
                os.path.join(getattr(sys, '_MEIPASS', ''), "assets", "apple-touch-icon.png") if getattr(sys, '_MEIPASS', None) else "",
            ]
            for p in icon_paths:
                if p and os.path.exists(p):
                    with open(p, "rb") as f:
                        ZimHandler._apple_touch_icon_data = f.read()
                    break
            if not ZimHandler._apple_touch_icon_data:
                return self._serve_favicon()  # fallback to regular favicon
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(ZimHandler._apple_touch_icon_data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(ZimHandler._apple_touch_icon_data)

    def _serve_index(self, vary=None):
        return self._html(200, SEARCH_UI_HTML, vary=vary)

    def _html(self, code, content, vary=None):
        self._send(code, content.encode(), "text/html; charset=utf-8", vary=vary)

    def _json(self, code, data):
        self._send(code, json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode(), "application/json")

    def log_message(self, format, *args):
        # Light logging: errors + slow requests. Suppress 200/304 noise.
        if len(args) >= 2 and str(args[1]) in ("200", "304"):
            return
        log.info(format, *args)


# ── CLI ──

def main():
    parser = argparse.ArgumentParser(description="ZIM Knowledge Base Reader")
    sub = parser.add_subparsers(dest="command")

    p_search = sub.add_parser("search", help="Full-text search across ZIM files")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--limit", type=int, default=5)
    p_search.add_argument("--zim", help="Search specific ZIM only")

    p_read = sub.add_parser("read", help="Read an article")
    p_read.add_argument("zim", help="ZIM short name")
    p_read.add_argument("path", help="Article path within ZIM")
    p_read.add_argument("--max-length", type=int, default=MAX_CONTENT_LENGTH)

    p_suggest = sub.add_parser("suggest", help="Title autocomplete")
    p_suggest.add_argument("query")
    p_suggest.add_argument("--zim", help="Specific ZIM")
    p_suggest.add_argument("--limit", type=int, default=10)

    sub.add_parser("list", help="List available ZIM files")

    p_serve = sub.add_parser("serve", help="Start HTTP API server")
    p_serve.add_argument("--port", type=int, default=8899)
    p_serve.add_argument("--ui", action="store_true", help="Open in a native desktop window (requires pywebview)")

    sub.add_parser("desktop", help="Start server and open in a native desktop window (requires pywebview)")

    args = parser.parse_args()

    if args.command == "search":
        results = search_all(args.query, limit=args.limit, filter_zim=args.zim)
        print(json.dumps(results, indent=2, ensure_ascii=False))

    elif args.command == "read":
        result = read_article(args.zim, args.path, max_length=args.max_length)
        if "error" in result:
            print(json.dumps(result, indent=2), file=sys.stderr)
            sys.exit(1)
        # Print content directly for LLM consumption
        print(f"# {result['title']}")
        print(f"Source: {result['zim']} / {result['path']}")
        if result["truncated"]:
            print(f"(Showing {args.max_length} of {result['full_length']} chars)")
        print()
        print(result["content"])

    elif args.command == "suggest":
        results = suggest(args.query, zim_name=args.zim, limit=args.limit)
        print(json.dumps(results, indent=2, ensure_ascii=False))

    elif args.command == "list":
        load_cache()
        zims = list_zims()
        for z in zims:
            entries = z['entries'] if isinstance(z['entries'], int) else 0
            print(f"  {z['name']:40s} {z['size_gb']:>8.1f} GB  {entries:>10} entries  ({z['file']})")

    elif args.command == "desktop" or (args.command == "serve" and args.ui):
        try:
            from zimi_desktop import main as desktop_main
        except ImportError:
            print("Desktop mode requires pywebview: pip install pywebview", file=sys.stderr)
            sys.exit(1)
        desktop_main()

    elif args.command == "serve":
        print(f"ZIM Reader API starting on port {args.port}")
        print(f"ZIM directory: {ZIM_DIR}")
        load_cache()
        # Clean up stale partial downloads (>24h old)
        for tmp in glob.glob(os.path.join(ZIM_DIR, "*.zim.tmp")):
            try:
                age = time.time() - os.path.getmtime(tmp)
                if age > 86400:
                    os.remove(tmp)
                    log.info("Cleaned up stale partial download: %s", os.path.basename(tmp))
                else:
                    log.info("Partial download found (resumable): %s", os.path.basename(tmp))
            except OSError:
                pass
        # Pre-warm all archive handles so first search is fast
        zims = get_zim_files()
        log.info("Pre-warming %d archives...", len(zims))
        for name in zims:
            try:
                get_archive(name)
            except Exception as e:
                log.warning("Skipping %s: %s", name, e)
        log.info("All archives ready")
        # Pre-warm suggestion indexes in background (loads B-tree pages into OS cache).
        # Uses throwaway Archive handles so it never holds _suggest_op_lock — user
        # fast searches can proceed immediately even while warm-up is running.
        def _warm_suggest_indexes():
            from concurrent.futures import ThreadPoolExecutor
            zim_files = get_zim_files()
            warmed = [0]
            count_lock = threading.Lock()

            def _warm_one(name, path):
                try:
                    # Pre-open suggest pool handle (fast, no index I/O)
                    _get_suggest_archive(name)
                    # Warm B-tree pages into OS page cache via throwaway handle
                    archive = open_archive(path)
                    ss = SuggestionSearcher(archive)
                    s = ss.suggest("a")
                    s.getResults(0, 1)
                    with count_lock:
                        warmed[0] += 1
                except Exception:
                    pass

            # Parallel warmup — 4 workers keeps disk busy without
            # overwhelming spinning disk seek capacity
            with ThreadPoolExecutor(max_workers=4) as pool:
                for name, path in zim_files.items():
                    pool.submit(_warm_one, name, path)
            log.info("Suggestion indexes warmed: %d/%d", warmed[0], len(zim_files))
        threading.Thread(target=_warm_suggest_indexes, daemon=True).start()
        # Pre-warm FTS pool in background (opens per-ZIM Archive handles for parallel Xapian search)
        def _warm_fts_pool():
            zim_files = get_zim_files()
            for name in zim_files:
                try:
                    _get_fts_archive(name)
                except Exception:
                    pass
            log.info("FTS pool warmed: %d archives", len(_fts_pool))
        threading.Thread(target=_warm_fts_pool, daemon=True).start()
        # Build SQLite title indexes in background (one-time per ZIM, enables <10ms title search)
        threading.Thread(target=_build_all_title_indexes, daemon=True).start()
        # Pre-warm title index SQLite connections (opens DB handles, no heavy I/O)
        def _warm_title_indexes():
            zim_files = get_zim_files()
            opened = 0
            for name in zim_files:
                if _get_title_db(name) is not None:
                    opened += 1
            log.info("Title indexes opened: %d/%d", opened, len(zim_files))
        threading.Thread(target=_warm_title_indexes, daemon=True).start()
        # Restore suggest cache from disk (instant warm queries after restart)
        loaded = _suggest_cache_restore()
        if loaded:
            log.info("Suggest cache restored: %d entries", loaded)
        # Start auto-update thread if enabled
        if _auto_update_enabled:
            _auto_update_thread = threading.Thread(target=_auto_update_loop, daemon=True)
            _auto_update_thread.start()
        print(f"Endpoints: /search, /read, /suggest, /list, /health")
        server = ThreadingHTTPServer(("0.0.0.0", args.port), ZimHandler)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            _suggest_cache_persist()
            log.info("Suggest cache saved to disk")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
