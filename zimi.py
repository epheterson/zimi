#!/usr/bin/env python3
"""
Zimi -- Offline Knowledge Viewer & API

Search and read articles from Kiwix ZIM files. Provides both a CLI and an
HTTP server with JSON API + web UI for browsing offline knowledge archives.

Requires: libzim (pip install libzim)
Optional: PyMuPDF (pip install PyMuPDF) for PDF-in-ZIM text extraction

Configuration:
  ZIM_DIR      Path to directory containing *.zim files (default: /zim)
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
import urllib.request

from libzim.reader import Archive
from libzim.search import Query, Searcher
from libzim.suggestion import SuggestionSearcher

try:
    import fitz  # PyMuPDF — for reading PDFs embedded in ZIM files
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False

log = logging.getLogger("zimi")
logging.basicConfig(format="%(asctime)s %(message)s", datefmt="%H:%M:%S", level=logging.INFO)

ZIM_DIR = os.environ.get("ZIM_DIR", "/zim")
ZIMI_MANAGE = os.environ.get("ZIMI_MANAGE", "0") == "1"
MAX_CONTENT_LENGTH = 8000  # chars returned per article, keeps responses manageable for LLMs
READ_MAX_LENGTH = 50000    # longer limit for the web UI reader
MAX_SEARCH_LIMIT = 50      # upper bound for search results per ZIM to prevent resource exhaustion
MAX_CONTENT_BYTES = 10 * 1024 * 1024  # 10 MB — skip snippet extraction for entries larger than this
MAX_SERVE_BYTES = 50 * 1024 * 1024    # 50 MB — refuse to serve entries larger than this (prevents OOM)
MAX_POST_BODY = 4096                  # max bytes accepted in POST requests

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
            or (n.startswith("zimgit-") and any(k in n for k in ("water", "food", "disaster", "knots")))):
        return "Medical"
    # Stack Exchange — check before Wikimedia (some SEs have wiki-adjacent names)
    if n in ("stackoverflow", "askubuntu", "superuser", "serverfault") or "stackexchange" in n:
        return "Stack Exchange"
    # Dev Docs
    if n.startswith("devdocs_") or n == "freecodecamp":
        return "Dev Docs"
    # Education
    if (n.startswith("ted_") or n.startswith("phzh_")
            or n in ("crashcourse", "phet", "appropedia", "artofproblemsolving", "edutechwiki")):
        return "Education"
    # How-To — before Wikimedia so wikihow doesn't match wiki*
    if n in ("wikihow", "ifixit", "explainxkcd") or "off-the-grid" in n:
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
    # Position within source (rank 0 = 20, rank 5 = 3.3)
    rank_score = 20 / (rank + 1)
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

    When searching all ZIMs: searches largest first (most likely to have results)
    and stops early once we have enough sources/results to keep response times fast.
    """
    zims = get_zim_files()
    cache_meta = {z["name"]: z.get("entries", 0) for z in (_zim_list_cache or [])}
    scoped = bool(filter_zim)

    if filter_zim:
        if filter_zim in zims:
            target_names = [filter_zim]
        else:
            return {"results": [], "by_source": {}, "total": 0, "elapsed": 0,
                    "error": f"ZIM '{filter_zim}' not found"}
    else:
        target_names = sorted(zims.keys(), key=lambda n: cache_meta.get(n, 0), reverse=True)

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

    for name in target_names:
        try:
            t0 = time.time()
            archive = get_archive(name)
            if archive is None:
                archive = open_archive(zims[name])
            results = search_zim(archive, cleaned, limit=per_zim, snippets=scoped)
            dt = time.time() - t0
            if dt > 0.3:
                timings.append(f"{name}={dt:.1f}s")
            valid = [r for r in results if "error" not in r]
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

        if item.mimetype == "application/pdf":
            # Extract text from embedded PDF
            plain = extract_pdf_text(raw, max_length=max_length)
            # Try to find a better title from the catalog
            title = entry.title
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
            "title": entry.title if item.mimetype != "application/pdf" else title,
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
            "size_gb": round(size_gb, 1),
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


def _extract_zim_metadata(name, path):
    """Open a ZIM archive and extract its metadata. Returns (info_dict, archive)."""
    size_gb = os.path.getsize(path) / (1024 ** 3)
    meta_title = name
    meta_desc = ""
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
    info = {
        "name": name,
        "file": os.path.basename(path),
        "size_gb": round(size_gb, 1),
        "entries": entry_count,
        "title": meta_title,
        "description": meta_desc,
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
                "size_gb": cached.get("size_gb", round(size / (1024 ** 3), 1)),
                "entries": cached.get("entries", "?"),
                "title": cached.get("title", name),
                "description": cached.get("description", ""),
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

    # Total results from opensearch
    total_el = root.find(".//{http://a9.com/-/spec/opensearch/1.1/}totalResults")
    try:
        total = int(total_el.text) if total_el is not None else 0
    except (ValueError, TypeError):
        total = 0

    local_names = set(get_zim_files().keys())
    items = []
    for entry in root.findall("atom:entry", ns):
        name = ""
        title = ""
        summary = ""
        language = ""
        article_count = 0
        size_bytes = 0
        download_url = ""
        icon_url = ""

        name_el = entry.find("atom:name", ns)
        if name_el is not None:
            name = name_el.text or ""
        title_el = entry.find("atom:title", ns)
        if title_el is not None:
            title = title_el.text or ""
        summary_el = entry.find("atom:summary", ns)
        if summary_el is not None:
            summary = summary_el.text or ""
        lang_el = entry.find("dc:language", ns)
        if lang_el is not None:
            language = lang_el.text or ""
        count_el = entry.find("atom:articleCount", ns)
        if count_el is not None:
            try:
                article_count = int(count_el.text)
            except (ValueError, TypeError):
                pass

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
                icon_url = href

        # Determine if installed by matching name prefix against local names
        installed = any(name and ln.startswith(name.split("_")[0]) for ln in local_names) if name else False

        items.append({
            "name": name,
            "title": title,
            "summary": summary,
            "language": language,
            "article_count": article_count,
            "size_bytes": size_bytes,
            "download_url": download_url,
            "icon_url": icon_url,
            "installed": installed,
        })

    return total, items, None


def _extract_zim_date(filename):
    """Extract the date portion from a ZIM filename. Returns (base_name, date_str) or (base_name, None)."""
    m = re.search(r'_(\d{4}-\d{2})\.zim$', filename)
    if m:
        base = filename[:m.start()]
        return base, m.group(1)
    return filename.replace('.zim', ''), None


def _check_updates():
    """Compare installed ZIMs against Kiwix catalog to find available updates.

    Fetches a large batch from the catalog and matches by base name.
    Returns list of {name, installed_date, latest_date, download_url}.
    """
    zims = get_zim_files()
    # Build lookup: base_name → (short_name, installed_date, filename)
    installed = {}
    for name, path in zims.items():
        filename = os.path.basename(path)
        base, date = _extract_zim_date(filename)
        if date:
            installed[base.lower()] = {"name": name, "date": date, "filename": filename}

    if not installed:
        return []

    # Fetch a large batch from Kiwix catalog (no query = all, sorted by popularity)
    total, items, err = _fetch_kiwix_catalog(query="", lang="eng", count=200, start=0)
    if err:
        return []

    updates = []
    for item in items:
        dl_url = item.get("download_url", "")
        if not dl_url:
            continue
        dl_filename = dl_url.split("/")[-1]
        cat_base, cat_date = _extract_zim_date(dl_filename)
        if not cat_date:
            continue
        # Match against installed
        key = cat_base.lower()
        if key in installed and cat_date > installed[key]["date"]:
            updates.append({
                "name": installed[key]["name"],
                "installed_date": installed[key]["date"],
                "latest_date": cat_date,
                "download_url": dl_url,
                "title": item.get("title", ""),
            })

    return updates


def _start_download(url):
    """Start a background download via curl. Returns download ID."""
    global _download_counter
    # Validate URL — only allow Kiwix official downloads
    if not url.startswith("https://download.kiwix.org/"):
        return None, "URL must be from download.kiwix.org"

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
        proc = subprocess.Popen(
            ["curl", "-L", "-o", dest, "-C", "-", "--progress-bar", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _active_downloads[dl_id] = {
            "id": dl_id,
            "url": url,
            "filename": filename,
            "dest": dest,
            "pid": proc.pid,
            "proc": proc,
            "started": time.time(),
        }
    return dl_id, None


def _get_downloads():
    """Get status of all active/completed downloads."""
    results = []
    with _download_lock:
        to_remove = []
        for dl_id, dl in _active_downloads.items():
            proc = dl["proc"]
            done = proc.poll() is not None
            size = 0
            try:
                if os.path.exists(dl["dest"]):
                    size = os.path.getsize(dl["dest"])
            except OSError:
                pass
            results.append({
                "id": dl_id,
                "filename": dl["filename"],
                "url": dl["url"],
                "size_bytes": size,
                "done": done,
                "exit_code": proc.returncode if done else None,
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
    def do_HEAD(self):
        """Handle HEAD requests (Traefik health checks)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        def param(key, default=None):
            return params.get(key, [default])[0]

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
                t0 = time.time()
                with _zim_lock:
                    result = search_all(q, limit=limit, filter_zim=zim)
                log.info("search q=%r limit=%d zim=%s %.1fs", q, limit, zim or "all", time.time() - t0)
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
                with _zim_lock:
                    result = read_article(zim, path, max_length=max_len)
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
                with _zim_lock:
                    result = suggest(q, zim_name=zim, limit=limit)
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
                return self._json(200, {"snippet": snippet})

            elif parsed.path == "/health":
                zim_count = len(get_zim_files())
                return self._json(200, {"status": "ok", "zim_count": zim_count, "pdf_support": HAS_PYMUPDF})

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
                chosen = {"zim": pick_name, "path": result["path"], "title": result["title"]}
                log.info("random zim=%s title=%r %.1fs", pick_name, result["title"], time.time() - t0)
                return self._json(200, chosen)

            elif parsed.path == "/manage/status" and ZIMI_MANAGE:
                zim_count = len(get_zim_files())
                total_gb = sum(z.get("size_gb", 0) for z in (_zim_list_cache or []))
                return self._json(200, {
                    "zim_count": zim_count,
                    "total_size_gb": round(total_gb, 1),
                    "manage_enabled": True,
                })

            elif parsed.path == "/manage/catalog" and ZIMI_MANAGE:
                query = param("q", "")
                lang = param("lang", "eng")
                try:
                    count = min(int(param("count", "20")), 50)
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

            elif parsed.path == "/manage/check-updates" and ZIMI_MANAGE:
                updates = _check_updates()
                return self._json(200, {"updates": updates, "count": len(updates)})

            elif parsed.path == "/manage/downloads" and ZIMI_MANAGE:
                return self._json(200, {"downloads": _get_downloads()})

            elif parsed.path.startswith("/manage/") and not ZIMI_MANAGE:
                return self._json(404, {"error": "Library management is disabled. Set ZIMI_MANAGE=1 to enable."})

            elif parsed.path == "/":
                return self._html(200, SEARCH_UI_HTML)

            elif parsed.path.startswith("/w/"):
                # /w/<zim_name>/<entry_path> — serve raw ZIM content
                rest = parsed.path[3:]  # strip "/w/"
                slash = rest.find("/")
                if slash == -1:
                    zim_name, entry_path = unquote(rest), ""
                else:
                    zim_name = unquote(rest[:slash])
                    entry_path = unquote(rest[slash + 1:])
                # Empty path → serve SPA (source mode handled client-side)
                if not entry_path:
                    return self._html(200, SEARCH_UI_HTML)
                with _zim_lock:
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

            if not ZIMI_MANAGE and parsed.path.startswith("/manage/"):
                return self._json(404, {"error": "Library management is disabled."})

            if parsed.path == "/manage/download" and ZIMI_MANAGE:
                url = data.get("url", "")
                if not url:
                    return self._json(400, {"error": "missing 'url' in request body"})
                dl_id, err = _start_download(url)
                if err:
                    return self._json(400, {"error": err})
                return self._json(200, {"status": "started", "id": dl_id})

            elif parsed.path == "/manage/refresh" and ZIMI_MANAGE:
                # Re-scan ZIM directory and rebuild cache without full restart
                log.info("Library refresh triggered")
                with _zim_lock:
                    load_cache(force=True)
                    count = len(_zim_list_cache or [])
                return self._json(200, {"status": "refreshed", "zim_count": count})

            elif parsed.path == "/manage/update" and ZIMI_MANAGE:
                # Trigger the kiwix-zim.sh updater (fire-and-forget)
                updater = os.path.join(ZIM_DIR, "kiwix-zim", "kiwix-zim.sh")
                if not os.path.exists(updater):
                    return self._json(404, {"error": f"Updater not found at {updater}"})
                subprocess.Popen(
                    ["nohup", updater, "-d", ZIM_DIR],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return self._json(200, {"status": "started"})

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
        """Serve raw ZIM content with correct MIME type for the /w/ endpoint."""
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

        # Guard against huge entries (video, large PDFs) that could OOM the container
        if item.size > MAX_SERVE_BYTES:
            self.send_response(413)
            self.send_header("Content-Type", "text/plain")
            msg = f"Entry too large ({item.size // (1024*1024)} MB). Max: {MAX_SERVE_BYTES // (1024*1024)} MB.".encode()
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return

        content = bytes(item.content)
        mimetype = item.mimetype or ""

        # Fallback MIME from extension if empty
        if not mimetype:
            ext = os.path.splitext(entry_path)[1].lower()
            mimetype = MIME_FALLBACK.get(ext, "application/octet-stream")

        # Strip <base> tags from HTML — they point to the original online URL
        # and break relative resource resolution within the ZIM
        if mimetype.startswith("text/html"):
            text = content.decode("UTF-8", errors="replace")
            text = re.sub(r'<base\s[^>]*>', '', text, flags=re.IGNORECASE)
            content = text.encode("UTF-8")

        # ETag for caching — hash of zim name + path (content is immutable in ZIMs)
        etag = '"' + hashlib.md5(f"{zim_name}/{entry_path}".encode()).hexdigest()[:16] + '"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", mimetype)
        self.send_header("Cache-Control", "public, max-age=86400, immutable")
        self.send_header("ETag", etag)

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
        print(f"Endpoints: /search, /read, /suggest, /list, /health")
        server = ThreadingHTTPServer(("0.0.0.0", args.port), ZimHandler)
        server.serve_forever()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
