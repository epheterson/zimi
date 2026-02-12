#!/usr/bin/env python3
"""
Zimi MCP Server — Expose offline knowledge as MCP tools for AI agents.

Provides search, read, suggest, list, and random tools over ZIM files
via the Model Context Protocol (stdio transport).

Usage:
  python3 zimi_mcp.py

Configuration:
  ZIM_DIR    Path to directory containing *.zim files (default: /zim)

Claude Code config (local):
  {
    "mcpServers": {
      "zimi": {
        "command": "python3",
        "args": ["/path/to/zimi_mcp.py"],
        "env": { "ZIM_DIR": "/path/to/zims" }
      }
    }
  }

Claude Code config (Docker on NAS):
  {
    "mcpServers": {
      "zimi": {
        "command": "ssh",
        "args": ["nas", "docker", "exec", "-i", "zimi", "python3", "/app/zimi_mcp.py"]
      }
    }
  }
"""

import sys
import os

# Ensure zimi.py is importable from the same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

import zimi

# Initialize: load ZIM metadata (uses persistent cache for instant startup)
zimi.load_cache()

mcp = FastMCP("zimi", instructions="Search and read articles from offline ZIM knowledge archives.")


@mcp.tool()
def search(query: str, zim: str = "", limit: int = 5) -> str:
    """Full-text search across offline knowledge sources.

    Searches Wikipedia, Stack Overflow, dev docs, and other ZIM archives.
    Returns ranked results with titles and snippets.

    Args:
        query: Search query (e.g. "water purification", "Python asyncio")
        zim: Optional — scope to a specific source (e.g. "wikipedia", "stackoverflow")
        limit: Max results to return (default 5, max 50)
    """
    limit = max(1, min(limit, 50))
    with zimi._zim_lock:
        result = zimi.search_all(query, limit=limit, filter_zim=zim or None)

    items = result.get("results", [])
    if not items:
        return f"No results found for '{query}'."

    lines = [f"Found {result['total']} results in {result.get('elapsed', '?')}s:\n"]
    for r in items[:limit]:
        lines.append(f"- **{r['title']}** [{r['zim']}]")
        lines.append(f"  Path: {r['zim']}/{r['path']}")
        if r.get("snippet"):
            snippet = r["snippet"][:200]
            lines.append(f"  {snippet}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def read(zim: str, path: str, max_length: int = 8000) -> str:
    """Read an article from a ZIM source as plain text.

    Use search() first to find articles, then read() to get the full content.

    Args:
        zim: Source name (e.g. "wikipedia", "stackoverflow")
        path: Article path within the source (from search results)
        max_length: Max characters to return (default 8000, max 50000)
    """
    max_length = max(100, min(max_length, 50000))
    with zimi._zim_lock:
        result = zimi.read_article(zim, path, max_length=max_length)

    if "error" in result:
        return f"Error: {result['error']}"

    header = f"# {result['title']}\nSource: {result['zim']} / {result['path']}"
    if result.get("truncated"):
        header += f"\n(Showing {max_length} of {result['full_length']} chars)"
    return f"{header}\n\n{result['content']}"


@mcp.tool()
def suggest(query: str, zim: str = "", limit: int = 10) -> str:
    """Title autocomplete — find articles by title prefix.

    Faster than full-text search. Good for finding specific articles.

    Args:
        query: Title prefix (e.g. "pytho" → "Python", "Python (programming language)")
        zim: Optional — scope to a specific source
        limit: Max suggestions (default 10)
    """
    limit = max(1, min(limit, 50))
    with zimi._zim_lock:
        result = zimi.suggest(query, zim_name=zim or None, limit=limit)

    if not result:
        return f"No suggestions for '{query}'."

    lines = []
    for source, items in result.items():
        for item in items:
            if "error" in item:
                continue
            lines.append(f"- {item['title']} [{source}] → {source}/{item['path']}")
    return "\n".join(lines) if lines else f"No suggestions for '{query}'."


@mcp.tool()
def list_sources() -> str:
    """List all available offline knowledge sources.

    Shows every ZIM archive with article counts and sizes.
    Use source names with search() and read().
    """
    sources = zimi.list_zims()
    if not sources:
        return "No ZIM sources found. Add .zim files to the ZIM_DIR directory."

    lines = [f"{len(sources)} sources available:\n"]
    for z in sources:
        entries = z['entries'] if isinstance(z['entries'], int) else 0
        lines.append(f"- **{z.get('title', z['name'])}** (`{z['name']}`) — {entries:,} entries, {z['size_gb']} GB")
    return "\n".join(lines)


@mcp.tool()
def random(zim: str = "") -> str:
    """Get a random article from the knowledge base.

    Args:
        zim: Optional — scope to a specific source (e.g. "wikipedia")
    """
    if zim:
        if zim not in zimi.get_zim_files():
            return f"Source '{zim}' not found."
        pick_name = zim
    else:
        eligible = [z for z in (zimi._zim_list_cache or [])
                    if isinstance(z.get("entries"), int) and z["entries"] > 100]
        if not eligible:
            return "No sources available."
        import random as _random
        pick_name = _random.choice(eligible)["name"]

    with zimi._zim_lock:
        archive = zimi.get_archive(pick_name)
        if archive is None:
            return "Archive not available."
        result = zimi.random_entry(archive)

    if not result:
        return "No articles found."

    return f"**{result['title']}** [{pick_name}]\nPath: {pick_name}/{result['path']}\n\nUse read(zim=\"{pick_name}\", path=\"{result['path']}\") to read the full article."


if __name__ == "__main__":
    mcp.run(transport="stdio")
