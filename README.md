# Zimi

Search and read 100M+ articles offline. API-first knowledge server for [ZIM files](https://wiki.openzim.org/wiki/ZIM_file_format).

[Kiwix](https://kiwix.org) packages the world's knowledge into ZIM files — offline archives of Wikipedia, Stack Overflow, dev docs, and thousands of other sources. Zimi makes them useful:

- **JSON API** for AI agents — search, read, and browse articles programmatically
- **MCP server** for Claude Code and other AI tool integrations
- **Web UI** for humans — dark theme, cross-source search, in-browser reader
- **Library manager** — browse the Kiwix catalog and download ZIMs from the UI
- **Cross-ZIM search** with relevance ranking, deduplication, and time budgets
- **Persistent cache** — instant startup on subsequent runs

## Screenshots

| Homepage | Search Results |
|----------|---------------|
| ![Homepage](screenshots/homepage.png) | ![Search](screenshots/search.png) |

| Article Reader | Library Manager |
|----------------|-----------------|
| ![Reader](screenshots/reader.png) | ![Library](screenshots/library.png) |

## Quick Start (Docker)

**Have ZIM files already?** Mount them and go:

```bash
docker run -v /path/to/zims:/zims -p 8899:8899 epheterson/zimi
```

**Starting fresh?** Run with an empty directory — the built-in library manager lets you browse and download ZIMs directly:

```bash
mkdir zims
docker run -v ./zims:/zims -p 8899:8899 epheterson/zimi
```

Open http://localhost:8899, click the gear icon, and browse the Kiwix catalog to download your first ZIM.

Hit the API directly:

```bash
curl "http://localhost:8899/search?q=water+purification&limit=3"
```

## API

| Endpoint | Description |
|----------|-------------|
| `GET /search?q=...&limit=5&zim=...` | Full-text search (cross-ZIM or scoped) |
| `GET /read?zim=...&path=...&max_length=8000` | Read article as plain text |
| `GET /suggest?q=...&limit=10&zim=...` | Title autocomplete |
| `GET /list` | List all ZIM sources with metadata |
| `GET /catalog?zim=...` | PDF catalog for zimgit-style ZIMs |
| `GET /snippet?zim=...&path=...` | Short text snippet |
| `GET /random?zim=...` | Random article |
| `GET /health` | Health check (includes version) |
| `GET /w/<zim>/<path>` | Serve raw ZIM content (HTML, images) |

### Examples

```bash
# Search across all sources
curl "http://localhost:8899/search?q=python+asyncio&limit=5"

# Search within a specific source
curl "http://localhost:8899/search?q=linked+list&zim=stackoverflow&limit=10"

# Read an article
curl "http://localhost:8899/read?zim=wikipedia&path=A/Water_purification"

# Title autocomplete
curl "http://localhost:8899/suggest?q=pytho&limit=5"

# List all sources
curl "http://localhost:8899/list"

# Random article
curl "http://localhost:8899/random"
```

## MCP Server

Zimi includes an MCP (Model Context Protocol) server that exposes search/read tools to AI agents.

### Claude Code (local)

Add to your Claude Code MCP settings:

```json
{
  "mcpServers": {
    "zimi": {
      "command": "python3",
      "args": ["/path/to/zimi_mcp.py"],
      "env": { "ZIM_DIR": "/path/to/zims" }
    }
  }
}
```

### Claude Code (Docker on remote host)

```json
{
  "mcpServers": {
    "zimi": {
      "command": "ssh",
      "args": ["your-server", "docker", "exec", "-i", "zimi", "python3", "/app/zimi_mcp.py"]
    }
  }
}
```

### Available Tools

| Tool | Description |
|------|-------------|
| `search` | Full-text search across all ZIM sources |
| `read` | Read an article as plain text |
| `suggest` | Title autocomplete |
| `list_sources` | List all available sources |
| `random` | Random article |

## CLI

```bash
# Search
python3 zimi.py search "water purification" --limit 10

# Read an article
python3 zimi.py read wikipedia "A/Water_purification"

# List sources
python3 zimi.py list

# Title autocomplete
python3 zimi.py suggest "pytho"

# Start HTTP server
python3 zimi.py serve --port 8899
```

## Docker

### Docker Compose

```yaml
services:
  zimi:
    image: epheterson/zimi
    container_name: zimi
    restart: unless-stopped
    ports:
      - "8899:8899"
    volumes:
      - ./zims:/zims
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ZIM_DIR` | `/zims` | Path to directory containing ZIM files |
| `ZIMI_MANAGE` | `1` | Library manager (browse/download ZIMs). Set to `0` to disable. |
| `ZIMI_MANAGE_PASSWORD` | _(none)_ | Password to protect library management. Can also be set from the UI. |
| `ZIMI_AUTO_UPDATE` | `0` | Auto-update ZIMs. Set to `1` to enable. |
| `ZIMI_UPDATE_FREQ` | `weekly` | Auto-update frequency: `daily`, `weekly`, or `monthly`. |
| `ZIMI_RATE_LIMIT` | `60` | API rate limit (requests/minute per IP). Set to `0` to disable. |

**Forgot your password?** Delete `.zimi_password` from your ZIM directory and restart the container.

## Library Manager

The built-in library manager is enabled by default (gear icon in the web UI). You can:

- Browse the Kiwix catalog
- Download ZIMs directly
- Check for updates to installed ZIMs
- Refresh the library cache

Management API endpoints:

| Endpoint | Description |
|----------|-------------|
| `GET /manage/status` | Library status (count, total size) |
| `GET /manage/catalog?q=...` | Browse Kiwix catalog |
| `GET /manage/check-updates` | Check for ZIM updates |
| `GET /manage/downloads` | Active download status |
| `POST /manage/download` | Start a ZIM download |
| `POST /manage/refresh` | Re-scan and rebuild cache |
| `GET /manage/stats` | Server metrics (requests, latency, cache) |
| `POST /manage/auto-update` | Configure auto-update (enable/disable, frequency) |

## Getting ZIM Files

ZIM files are offline archives of websites. Download them from:

- **[Kiwix Library](https://library.kiwix.org)** — Browse and download ZIMs
- **[download.kiwix.org](https://download.kiwix.org/zim/)** — Direct downloads

Popular ZIMs:

| Source | Size | Articles |
|--------|------|----------|
| Wikipedia (English, all) | ~100 GB | 6.8M |
| Stack Overflow | ~28 GB | 24M |
| Wikipedia (English, top) | ~12 GB | 200K |
| DevDocs | ~0.5 GB each | varies |
| WikiHow | ~4 GB | 240K |

Place `.zim` files in your ZIM directory and restart Zimi (or use the refresh endpoint).

## Architecture

- **`zimi.py`** — HTTP server + CLI + core library (search, read, suggest, random)
- **`zimi_mcp.py`** — MCP server wrapping core functions for AI agent integration
- **`templates/index.html`** — Single-page web UI (vanilla JS, no build step)

Zimi uses Python's built-in `http.server.ThreadingHTTPServer` with a global lock around all libzim operations (the C library is not thread-safe). Non-ZIM endpoints remain responsive under concurrent load.

## License

[MIT](LICENSE)
