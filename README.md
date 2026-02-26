# Zimi

The offline internet — searchable, browsable, and self-updating.

Kiwix packages the world's knowledge into ZIM files — compressed offline copies of Wikipedia, Stack Overflow, dev docs, and more. Zimi is a modern server that makes them feel like the real thing.

**What you get:**

- **Cross-source search** — two-stage search: instant title matches first, then deep full-text results across all ZIMs in parallel. Results ranked by title relevance, source authority, and position — with thumbnails and smart snippets.
- **Discover** — daily highlights from your installed sources: Picture of the Day, On This Day, Quote of the Day, Country of the Day, and more. Content rotates daily.
- **Cross-source links** — reading Wikipedia and it links to Wiktionary? Or Stack Overflow to a GitHub repo? If you have both installed, the link lights up — click and stay in Zimi.
- **Bookmarks & history** — save articles while reading, search your history, pick up where you left off.
- **Catalog browser** — visual gallery of 1,000+ Kiwix archives across 10+ categories. One-click install with flavor picker (Mini / No images / Full).
- **Article reader** — clean dark-theme reader with navigation history, PDF viewer, and cross-ZIM link resolution.
- **Library management** — auto-update on a schedule, password protection, download queue with progress tracking.
- **Collections** — group sources into named sets for scoped search and homepage sections.
- **JSON API** — every feature accessible programmatically for scripts, bots, and integrations.
- **MCP server** — plug into Claude Code and other AI agents as a knowledge tool.
- **Desktop app** — native macOS window with system tray, configurable ZIM folder, and browser access.
- **Runs anywhere** — Homebrew, Snap, Docker, pip, AppImage, or standalone binary.

## Screenshots

| Homepage | Search Results |
|----------|---------------|
| ![Homepage](screenshots/homepage.png) | ![Search](screenshots/search.png) |

| Article Reader | Catalog |
|----------------|---------|
| ![Reader](screenshots/reader.png) | ![Catalog](screenshots/browse-library.png) |

## Install

### macOS

```bash
brew tap epheterson/zimi && brew install --cask zimi
```

Or download directly from [GitHub Releases](https://github.com/epheterson/Zimi/releases).

### Linux

```bash
sudo snap install zimi
```

Or download the [AppImage](https://github.com/epheterson/Zimi/releases).

### Docker

```bash
docker run -v ./zims:/zims -p 8899:8899 epheterson/zimi
```

Open http://localhost:8899. Starting fresh? Browse and download ZIMs from the built-in catalog.

### Python (any platform)

```bash
pip install zimi
zimi serve --port 8899
```

## API

| Endpoint | Description |
|----------|-------------|
| `GET /search?q=...&limit=5&zim=...&fast=1` | Full-text search (cross-ZIM or scoped). `fast=1` returns title matches only. |
| `GET /read?zim=...&path=...&max_length=8000` | Read article as plain text |
| `GET /suggest?q=...&limit=10&zim=...` | Title autocomplete |
| `GET /list` | List all ZIM sources with metadata |
| `GET /catalog?zim=...` | PDF catalog for zimgit-style ZIMs |
| `GET /snippet?zim=...&path=...` | Short text snippet |
| `GET /random?zim=...` | Random article |
| `GET /collections` | List all collections |
| `POST /collections` | Create/update a collection |
| `DELETE /collections?name=...` | Delete a collection |
| `GET /resolve?url=...` | Resolve external URL to ZIM source + path |
| `POST /resolve` | Batch resolve: `{"urls": [...]}` → `{"results": {...}}` |
| `GET /health` | Health check (includes version) |
| `GET /w/<zim>/<path>` | Serve raw ZIM content (HTML, images) |

### Examples

```bash
# Search across all sources
curl "http://localhost:8899/search?q=python+asyncio&limit=5"

# Read an article
curl "http://localhost:8899/read?zim=wikipedia&path=A/Water_purification"

# Title autocomplete
curl "http://localhost:8899/suggest?q=pytho&limit=5"
```

## MCP Server

Zimi includes an MCP server for AI agents like Claude Code.

```json
{
  "mcpServers": {
    "zimi": {
      "command": "python3",
      "args": ["-m", "zimi.mcp_server"],
      "env": { "ZIM_DIR": "/path/to/zims" }
    }
  }
}
```

For Docker on a remote host, use SSH:

```json
{
  "mcpServers": {
    "zimi": {
      "command": "ssh",
      "args": ["your-server", "docker", "exec", "-i", "zimi", "python3", "-m", "zimi.mcp_server"]
    }
  }
}
```

Tools: `search`, `read`, `suggest`, `list_sources`, `random`

## Docker Compose

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
| `ZIM_DIR` | `/zims` | Path to ZIM files |
| `ZIMI_MANAGE` | `1` | Library manager. Set to `0` to disable. |
| `ZIMI_MANAGE_PASSWORD` | _(none)_ | Protect library management |
| `ZIMI_AUTO_UPDATE` | `0` | Auto-update ZIMs (`1` to enable) |
| `ZIMI_UPDATE_FREQ` | `weekly` | `daily`, `weekly`, or `monthly` |
| `ZIMI_RATE_LIMIT` | `60` | API rate limit (requests/min/IP). `0` to disable. |

## Zimi vs kiwix-serve

[kiwix-serve](https://github.com/kiwix/kiwix-tools) is the official ZIM server from the Kiwix project. Both serve ZIM files over HTTP — here's how they differ:

| | Zimi | kiwix-serve |
|---|---|---|
| **Search API** | JSON responses | HTML responses |
| **Search performance** | Parallel Xapian FTS (per-ZIM threads) | Sequential |
| **Cross-source search** | Unified results with relevance ranking | Per-ZIM or combined unranked |
| **Library management** | Built-in catalog browser, downloads, updates | Separate CLI tool (kiwix-manage) |
| **AI integration** | MCP server for Claude Code | None |
| **Desktop app** | Native macOS app | None |
| **Runtime** | Python (~4,200 lines) | C++ (libkiwix) |
| **Memory** | Higher (Python + SQLite indexes) | Lower (native C++) |

**Use kiwix-serve** for lightweight, proven ZIM serving on low-memory devices. **Use Zimi** for JSON APIs, cross-source search, library management, AI integration, or a desktop app.

## Tests

```bash
python3 tests/test_unit.py                          # Unit tests
python3 -m pytest tests/test_server.py -v           # Integration tests
python3 tests/test_unit.py --perf                   # Performance tests (requires running server)
```

## License

[MIT](LICENSE)
