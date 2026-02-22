#!/usr/bin/env python3
"""
Zimi MCP Server — Expose offline knowledge as MCP tools for AI agents.

Provides search, read, suggest, list, and random tools over ZIM files
via the Model Context Protocol (stdio transport).

Usage:
  python3 -m zimi.mcp_server

Configuration:
  ZIM_DIR    Path to directory containing *.zim files (default: /zims)

Claude Code config (local):
  {
    "mcpServers": {
      "zimi": {
        "command": "python3",
        "args": ["-m", "zimi.mcp_server"],
        "env": { "ZIM_DIR": "/path/to/zims" }
      }
    }
  }

Claude Code config (Docker via SSH):
  {
    "mcpServers": {
      "zimi": {
        "command": "ssh",
        "args": ["your-server", "docker", "exec", "-i", "zimi", "python3", "-m", "zimi.mcp_server"]
      }
    }
  }
"""

from mcp.server.fastmcp import FastMCP

from zimi import server as zimi

# Initialize: load ZIM metadata (uses persistent cache for instant startup)
zimi.load_cache()

mcp = FastMCP("zimi", instructions="Search and read articles from offline ZIM knowledge archives.")


@mcp.tool()
def search(query: str, zim: str = "", collection: str = "", limit: int = 5) -> str:
    """Full-text search across offline knowledge sources.

    Searches Wikipedia, Stack Overflow, dev docs, and other ZIM archives.
    Returns ranked results with titles and snippets.

    Args:
        query: Search query (e.g. "water purification", "Python asyncio")
        zim: Optional — scope to specific source(s), comma-separated (e.g. "wikipedia,stackoverflow")
        collection: Optional — search within a named collection (overrides zim)
        limit: Max results to return (default 5, max 50)
    """
    limit = max(1, min(limit, 50))
    filter_zim = None
    if collection:
        cdata = zimi._load_collections()
        coll = cdata.get("collections", {}).get(collection)
        if not coll:
            return f"Collection '{collection}' not found."
        filter_zim = coll.get("zims", []) or None
    elif zim:
        parts = [z.strip() for z in zim.split(",") if z.strip()]
        filter_zim = parts if len(parts) > 1 else (parts[0] if parts else None)
    with zimi._zim_lock:
        result = zimi.search_all(query, limit=limit, filter_zim=filter_zim)

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
def suggest(query: str, zim: str = "", collection: str = "", limit: int = 10) -> str:
    """Title autocomplete — find articles by title prefix.

    Faster than full-text search. Good for finding specific articles.

    Args:
        query: Title prefix (e.g. "pytho" → "Python", "Python (programming language)")
        zim: Optional — scope to specific source(s), comma-separated
        collection: Optional — suggest within a named collection (overrides zim)
        limit: Max suggestions (default 10)
    """
    limit = max(1, min(limit, 50))
    zim_names = None
    if collection:
        cdata = zimi._load_collections()
        coll = cdata.get("collections", {}).get(collection)
        if coll:
            zim_names = coll.get("zims", [])
    elif zim:
        zim_names = [z.strip() for z in zim.split(",") if z.strip()]

    with zimi._zim_lock:
        if zim_names:
            result = {}
            for zn in zim_names:
                r = zimi.suggest(query, zim_name=zn, limit=limit)
                result.update(r)
        else:
            result = zimi.suggest(query, zim_name=None, limit=limit)

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


@mcp.tool()
def list_collections() -> str:
    """List all favorites and collections.

    Shows which ZIM sources are favorited and any named collections.
    """
    data = zimi._load_collections()
    favs = data.get("favorites", [])
    colls = data.get("collections", {})

    lines = []
    if favs:
        lines.append("**Favorites:** " + ", ".join(f"`{f}`" for f in favs))
    else:
        lines.append("No favorites set.")

    if colls:
        lines.append(f"\n**Collections ({len(colls)}):**")
        for name, info in colls.items():
            label = info.get("label", name)
            zim_list = info.get("zims", [])
            lines.append(f"- **{label}** (`{name}`) — {', '.join(f'`{z}`' for z in zim_list) if zim_list else 'empty'}")
    else:
        lines.append("No collections created.")

    return "\n".join(lines)


@mcp.tool()
def manage_collection(action: str, name: str = "", label: str = "", zims: str = "") -> str:
    """Create, update, or delete a named collection of ZIM sources.

    Collections let you group ZIMs for scoped search (e.g. "dev-docs" = stackoverflow + devdocs).

    Args:
        action: "create", "update", or "delete"
        name: Collection identifier (e.g. "dev-docs"). Auto-generated from label if omitted.
        label: Display name (e.g. "Dev Docs") — used for create/update
        zims: Comma-separated ZIM names (e.g. "stackoverflow,devdocs_python") — used for create/update
    """
    import re
    # Auto-generate name from label if not provided
    if not name and label:
        name = re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')[:64]
    if not name:
        return "Error: provide 'name' or 'label'."

    with zimi._collections_lock:
        data = zimi._load_collections()

        if action == "delete":
            if name not in data.get("collections", {}):
                return f"Collection '{name}' not found."
            del data["collections"][name]
            zimi._save_collections(data)
            return f"Deleted collection '{name}'."

        if action in ("create", "update"):
            zim_list = [z.strip() for z in zims.split(",") if z.strip()] if zims else []
            data.setdefault("collections", {})[name] = {
                "label": label or name,
                "zims": zim_list,
            }
            zimi._save_collections(data)
            return f"{'Created' if action == 'create' else 'Updated'} collection '{name}' with {len(zim_list)} sources."

    return f"Unknown action '{action}'. Use create, update, or delete."


@mcp.tool()
def manage_favorites(action: str, zim: str) -> str:
    """Add or remove a ZIM source from favorites.

    Favorites appear at the top of the homepage for quick access.

    Args:
        action: "add" or "remove"
        zim: ZIM source name (e.g. "wikipedia", "stackoverflow")
    """
    with zimi._collections_lock:
        data = zimi._load_collections()
        favs = data.get("favorites", [])

        if action == "add":
            if zim in favs:
                return f"'{zim}' is already a favorite."
            favs.append(zim)
            data["favorites"] = favs
            zimi._save_collections(data)
            return f"Added '{zim}' to favorites."

        if action == "remove":
            if zim not in favs:
                return f"'{zim}' is not a favorite."
            favs.remove(zim)
            data["favorites"] = favs
            zimi._save_collections(data)
            return f"Removed '{zim}' from favorites."

    return f"Unknown action '{action}'. Use add or remove."


if __name__ == "__main__":
    mcp.run(transport="stdio")
