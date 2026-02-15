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
  python3 zimi.py search "water purification" --limit 10
  python3 zimi.py read stackoverflow "Questions/12345"
  python3 zimi.py list
  python3 zimi.py suggest "pytho"

Usage (HTTP API):
  python3 zimi.py serve --port 8899

  GET /search?q=...&limit=5&zim=...   Full-text search (cross-ZIM or scoped)
  GET /read?zim=...&path=...           Read article as plaintext
  GET /w/<zim>/<path>                  Serve raw ZIM content (HTML, images)
  GET /suggest?q=...&limit=10          Title autocomplete
  GET /snippet?zim=...&path=...        Short text snippet
  GET /list                            List all ZIM sources with metadata
  GET /catalog?zim=...                 PDF catalog for zimgit-style ZIMs
  GET /random                          Random article
  GET /health                          Health check
"""

import argparse
import ast
import gzip
import glob
import hashlib
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
import urllib.error
import urllib.request

from libzim.reader import Archive
from libzim.search import Query, Searcher
from libzim.suggestion import SuggestionSearcher

try:
    import fitz  # PyMuPDF — for reading PDFs embedded in ZIM files
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False

ZIMI_VERSION = "1.1.0"

log = logging.getLogger("zimi")
logging.basicConfig(format="%(asctime)s %(message)s", datefmt="%H:%M:%S", level=logging.INFO)

ZIM_DIR = os.environ.get("ZIM_DIR", "/zims")
ZIMI_MANAGE = os.environ.get("ZIMI_MANAGE", "0") == "1"
def _password_file():
    """Password file path — try ZIM_DIR first, fall back to script directory."""
    for d in [ZIM_DIR, os.path.dirname(os.path.abspath(__file__))]:
        pf = os.path.join(d, ".zimi_password")
        if os.path.exists(pf) or os.access(d, os.W_OK):
            return pf
    return os.path.join(ZIM_DIR, ".zimi_password")  # last resort

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
    if auth.startswith("Bearer ") and _hash_pw(auth[7:]) == stored:
        return None  # valid
    return True  # unauthorized
MAX_CONTENT_LENGTH = 8000  # chars returned per article, keeps responses manageable for LLMs
READ_MAX_LENGTH = 50000    # longer limit for the web UI reader
MAX_SEARCH_LIMIT = 50      # upper bound for search results per ZIM to prevent resource exhaustion
MAX_CONTENT_BYTES = 10 * 1024 * 1024  # 10 MB — skip snippet extraction for entries larger than this
MAX_SERVE_BYTES = 50 * 1024 * 1024    # 50 MB — refuse to serve entries larger than this (prevents OOM)
MAX_POST_BODY = 4096                  # max bytes accepted in POST requests

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
            "disk_total_gb": round(total / (1024**3), 1),
            "disk_free_gb": round(free / (1024**3), 1),
            "disk_used_gb": round(used / (1024**3), 1),
            "disk_pct": round(used / total * 100, 1) if total > 0 else 0,
            "zim_size_gb": round(zim_size / (1024**3), 1),
        }
    except (OSError, AttributeError):
        return {}

# ── Auto-Update ──
_auto_update_enabled = os.environ.get("ZIMI_AUTO_UPDATE", "0") == "1"
_auto_update_freq = os.environ.get("ZIMI_UPDATE_FREQ", "weekly")  # daily, weekly, monthly
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
_search_cache = {}       # {(query, zim, limit): (result, timestamp)}
_search_cache_lock = threading.Lock()
SEARCH_CACHE_MAX = 100
SEARCH_CACHE_TTL = 300   # 5 minutes

def _search_cache_get(key):
    """Get cached search result if still valid."""
    with _search_cache_lock:
        entry = _search_cache.get(key)
        if entry and time.time() - entry[1] < SEARCH_CACHE_TTL:
            return entry[0]
        if entry:
            del _search_cache[key]
    return None

def _search_cache_put(key, result):
    """Store search result in cache, evicting oldest if full."""
    with _search_cache_lock:
        if len(_search_cache) >= SEARCH_CACHE_MAX:
            # Evict oldest entry
            oldest_key = min(_search_cache, key=lambda k: _search_cache[k][1])
            del _search_cache[oldest_key]
        _search_cache[key] = (result, time.time())

# MIME type fallback for ZIM entries with empty mimetype
MIME_FALLBACK = {
    ".html": "text/html", ".htm": "text/html", ".css": "text/css",
    ".js": "application/javascript", ".json": "application/json",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
    ".ico": "image/x-icon", ".pdf": "application/pdf",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
    ".eot": "application/vnd.ms-fontobject", ".otf": "font/otf",
    ".xml": "application/xml", ".txt": "text/plain",
}

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


MAX_SEARCH_SOURCES = 20     # stop after results from this many ZIMs
MAX_SEARCH_TOTAL = 200      # stop after this many total results
MAX_SEARCH_SECONDS = 4.0    # time budget for a full search across all ZIMs

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


def search_all(query_str, limit=5, filter_zim=None):
    """Search across all ZIM files (or a specific one).

    Returns unified ranked format:
    {
      "results": [{"zim": ..., "path": ..., "title": ..., "snippet": ..., "score": ...}],
      "by_source": {"zim_name": count, ...},
      "total": N,
      "elapsed": seconds
    }

    When searching all ZIMs: searches smallest first (fast results from focused
    ZIMs) and skips huge ZIMs when time budget is low to keep response times fast.
    """
    zims = get_zim_files()
    cache_meta = {z["name"]: (z.get("entries") if isinstance(z.get("entries"), int) else 0) for z in (_zim_list_cache or [])}
    scoped = bool(filter_zim)

    if filter_zim:
        if filter_zim in zims:
            target_names = [filter_zim]
        else:
            return {"results": [], "by_source": {}, "total": 0, "elapsed": 0,
                    "error": f"ZIM '{filter_zim}' not found"}
    else:
        target_names = sorted(zims.keys(), key=lambda n: cache_meta.get(n, 0))

    # Clean query for Xapian (strip stop words for global search)
    cleaned = _clean_query(query_str) if not scoped else query_str
    query_words = [w.lower() for w in cleaned.split() if w.lower() not in STOP_WORDS] or [w.lower() for w in query_str.split()]
    # How many to fetch per ZIM
    per_zim = limit if scoped else 20

    raw_results = []
    by_source = {}
    timings = []
    search_start = time.time()
    sources_hit = 0

    # Junk path patterns (SE tag index pages, etc.)
    _junk_re = re.compile(r'questions/tagged/|/tags$|/tags/page')

    for name in target_names:
        # Skip huge ZIMs when time budget is running low
        elapsed_so_far = time.time() - search_start
        remaining = MAX_SEARCH_SECONDS - elapsed_so_far
        entry_count = cache_meta.get(name, 0)
        if not scoped and remaining < 2.0 and entry_count > 1_000_000:
            timings.append(f"{name}=skipped({entry_count // 1_000_000}M)")
            continue

        try:
            t0 = time.time()
            archive = get_archive(name)
            if archive is None:
                archive = open_archive(zims[name])
            results = search_zim(archive, cleaned, limit=per_zim, snippets=scoped)
            dt = time.time() - t0
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
                sources_hit += 1
                if not scoped and sources_hit >= MAX_SEARCH_SOURCES:
                    break
                if not scoped and len(raw_results) >= MAX_SEARCH_TOTAL:
                    break
                if not scoped and (time.time() - search_start) > MAX_SEARCH_SECONDS:
                    break
        except Exception:
            pass

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
_RANDOM_SEEDS = [
    "water", "history", "city", "river", "mountain", "island", "language",
    "music", "science", "animal", "plant", "country", "ocean", "bridge",
    "school", "hospital", "library", "garden", "forest", "desert", "lake",
    "village", "temple", "church", "castle", "museum", "airport", "railway",
    "highway", "harbor", "stadium", "university", "market", "palace", "tower",
    "valley", "canyon", "volcano", "glacier", "peninsula", "archipelago",
    "republic", "kingdom", "province", "district", "county", "territory",
    "century", "dynasty", "revolution", "treaty", "empire", "colony",
    "protein", "molecule", "element", "mineral", "crystal", "fossil",
    "galaxy", "planet", "asteroid", "comet", "nebula", "satellite",
    "symphony", "opera", "poetry", "novel", "painting", "sculpture",
    "football", "cricket", "baseball", "tennis", "swimming", "marathon",
    "tiger", "eagle", "whale", "dolphin", "elephant", "butterfly",
    "algorithm", "database", "network", "protocol", "compiler", "kernel",
    "vitamin", "bacteria", "vaccine", "surgery", "therapy", "diagnosis",
    "climate", "earthquake", "hurricane", "tsunami", "drought", "monsoon",
    "democracy", "constitution", "parliament", "election", "treaty", "alliance",
    "copper", "silver", "diamond", "granite", "marble", "limestone",
    "chocolate", "coffee", "cotton", "silk", "rubber", "petroleum",
    "cathedral", "monastery", "pyramid", "lighthouse", "reservoir", "canal",
    "bicycle", "automobile", "aircraft", "submarine", "rocket", "telescope",
]

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


def random_entry(archive, max_attempts=6):
    """Pick a random article. Tries FTS first (fast), falls back to title suggestions.

    FTS (Xapian Searcher) is much faster on large ZIMs (Wikipedia: ~1-2s vs 7-15s)
    because inverted indexes allow O(log n) lookups vs sequential prefix scans.
    But some ZIMs lack FTS indexes, so we fall back to SuggestionSearcher.
    """
    # Phase 1: FTS with random seed words (fast on large ZIMs)
    seeds = _random.sample(_RANDOM_SEEDS, min(max_attempts, len(_RANDOM_SEEDS)))
    for seed in seeds:
        try:
            searcher = Searcher(archive)
            query = Query().set_query(seed)
            search = searcher.search(query)
            count = search.getEstimatedMatches()
            if count == 0:
                continue
            paths = list(search.getResults(0, min(count, 30)))
            result = _pick_html_entry(archive, paths)
            if result:
                return result
        except Exception:
            break  # FTS not available — fall through to suggestions

    # Phase 2: SuggestionSearcher fallback (works on ZIMs without FTS index)
    chars = "abcdefghijklmnopqrstuvwxyz"
    for _ in range(max_attempts):
        prefix = _random.choice(chars) + _random.choice(chars)
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
    return os.path.join(ZIM_DIR, ".zimi_cache.json")


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
        with urllib.request.urlopen(req, timeout=15) as resp:
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
            resp = urllib.request.urlopen(req, timeout=600)
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
            try:
                os.remove(tmp_dest)
            except OSError:
                pass
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
        dl["done"] = True
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

    def do_HEAD(self):
        """Handle HEAD requests (Traefik health checks)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()

    def _client_ip(self):
        """Get client IP, respecting X-Forwarded-For for reverse proxies."""
        xff = self.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0]

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
                zim = param("zim")
                cache_key = (q.lower().strip(), zim or "", limit)
                cached = _search_cache_get(cache_key)
                if cached is not None:
                    _record_metric("/search", 0)
                    return self._json(200, cached)
                t0 = time.time()
                with _zim_lock:
                    result = search_all(q, limit=limit, filter_zim=zim)
                dt = time.time() - t0
                _search_cache_put(cache_key, result)
                _record_metric("/search", dt)
                log.info("search q=%r limit=%d zim=%s %.1fs", q, limit, zim or "all", dt)
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
                return self._json(200, result)

            elif parsed.path == "/suggest":
                q = param("q")
                if not q:
                    return self._json(400, {"error": "missing ?q= parameter"})
                try:
                    limit = max(1, min(int(param("limit", "10")), MAX_SEARCH_LIMIT))
                except (ValueError, TypeError):
                    limit = 10
                zim = param("zim")
                t0 = time.time()
                with _zim_lock:
                    result = suggest(q, zim_name=zim, limit=limit)
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
                with _zim_lock:
                    archive = get_archive(zim)
                    if archive is None:
                        return self._json(404, {"error": f"ZIM '{zim}' not found"})
                    try:
                        entry = archive.get_entry_by_path(path)
                        item = entry.get_item()
                        if item.size > MAX_CONTENT_BYTES:
                            return self._json(200, {"snippet": ""})
                        # Only read first 10KB for snippet extraction
                        raw = bytes(item.content)[:10240]
                        text = raw.decode("UTF-8", errors="replace")
                        plain = strip_html(text)
                        snippet = plain[:300].strip()
                    except (KeyError, Exception):
                        snippet = ""
                _record_metric("/snippet", time.time() - t0)
                return self._json(200, {"snippet": snippet})

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
                t0 = time.time()
                with _zim_lock:
                    archive = get_archive(pick_name)
                    if archive is None:
                        return self._json(200, {"error": "archive not available"})
                    result = random_entry(archive)
                if not result:
                    return self._json(200, {"error": "no articles found"})
                dt = time.time() - t0
                chosen = {"zim": pick_name, "path": result["path"], "title": result["title"]}
                _record_metric("/random", dt)
                log.info("random zim=%s title=%r %.1fs", pick_name, result["title"], dt)
                return self._json(200, chosen)

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
                    return self._json(200, {
                        "zim_count": zim_count,
                        "total_size_gb": round(total_gb, 1),
                        "manage_enabled": True,
                    })

                elif parsed.path == "/manage/stats":
                    metrics = _get_metrics()
                    disk = _get_disk_usage()
                    auto_update = {
                        "enabled": _auto_update_enabled,
                        "frequency": _auto_update_freq,
                        "last_check": _auto_update_last_check,
                    }
                    return self._json(200, {"metrics": metrics, "disk": disk, "auto_update": auto_update})

                elif parsed.path == "/manage/catalog":
                    query = param("q", "")
                    lang = param("lang", "eng")
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

                else:
                    return self._json(404, {"error": "not found"})

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
                # Iframe/fetch requests get raw ZIM content as before.
                fetch_dest = self.headers.get("Sec-Fetch-Dest", "")
                if fetch_dest == "document" or not entry_path:
                    return self._serve_index()
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

            if parsed.path == "/manage/download" and ZIMI_MANAGE:
                url = data.get("url", "")
                if not url:
                    return self._json(400, {"error": "missing 'url' in request body"})
                dl_id, err = _start_download(url)
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
                return self._json(200, {"status": "refreshed", "zim_count": count})

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
                    os.remove(filepath)
                    log.info(f"Deleted ZIM: {filename}")
                    with _zim_lock:
                        load_cache(force=True)
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
                global _auto_update_enabled, _auto_update_freq, _auto_update_thread
                enabled = data.get("enabled", _auto_update_enabled)
                freq = data.get("frequency", _auto_update_freq)
                if freq not in _FREQ_SECONDS:
                    return self._json(400, {"error": f"Invalid frequency. Use: {', '.join(_FREQ_SECONDS.keys())}"})
                _auto_update_freq = freq
                if enabled and not _auto_update_enabled:
                    _auto_update_enabled = True
                    _auto_update_thread = threading.Thread(
                        target=_auto_update_loop, kwargs={"initial_delay": 30}, daemon=True)
                    _auto_update_thread.start()
                    log.info("Auto-update enabled: %s (first check in 30s)", freq)
                elif not enabled and _auto_update_enabled:
                    _auto_update_enabled = False
                    log.info("Auto-update disabled")
                return self._json(200, {"enabled": _auto_update_enabled, "frequency": _auto_update_freq})

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
                if entry.is_redirect:
                    entry = entry.get_redirect_entry()
            except KeyError:
                return self._json(404, {"error": f"Entry '{entry_path}' not found in {zim_name}"})

            item = entry.get_item()
            total_size = item.size
            mimetype = item.mimetype or ""

            if not mimetype:
                ext = os.path.splitext(entry_path)[1].lower()
                mimetype = MIME_FALLBACK.get(ext, "application/octet-stream")
            # Fix zimgit packaging bug: PDFs stored with text/html mimetype
            if entry_path.lower().endswith(".pdf") and mimetype != "application/pdf":
                mimetype = "application/pdf"

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
        self.send_header("ETag", etag)

        if is_streamable:
            self.send_header("Accept-Ranges", "bytes")

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

    def _send(self, code, body_bytes, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        if self._accepts_gzip() and len(body_bytes) > 256:
            body_bytes = gzip.compress(body_bytes, compresslevel=4)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _serve_index(self):
        return self._html(200, SEARCH_UI_HTML)

    def _html(self, code, content):
        self._send(code, content.encode(), "text/html; charset=utf-8")

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
        # Start auto-update thread if enabled
        if _auto_update_enabled:
            _auto_update_thread = threading.Thread(target=_auto_update_loop, daemon=True)
            _auto_update_thread.start()
        print(f"Endpoints: /search, /read, /suggest, /list, /health")
        server = ThreadingHTTPServer(("0.0.0.0", args.port), ZimHandler)
        server.serve_forever()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
