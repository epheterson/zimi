# Zimi

Search and read 100M+ articles offline. Wikipedia, Stack Overflow, dev docs, WikiHow, and thousands more — all on your machine, no internet required.

[Kiwix](https://kiwix.org) packages the world's knowledge into [ZIM files](https://wiki.openzim.org/wiki/ZIM_file_format) — compressed offline archives of entire websites. Zimi is the fastest way to search and read them.

**Three ways to run it:**

- **Docker** — self-host on a NAS, server, or anywhere with one command.
- **Desktop app** (macOS) — native window with built-in catalog browser. [Download here.](https://github.com/epheterson/Zimi/releases)
- **Python CLI** — run directly if you already have Python installed.

**What you get:**

- **Catalog browser** — visual gallery of 1,000+ available ZIM archives across 10 categories. One-click install.
- **Cross-source search** — search across all your sources at once, with sub-second title matches.
- **Article reader** — clean dark-theme reader with embedded PDF viewer and navigation history.
- **JSON API** — every feature accessible programmatically for scripts, bots, and integrations.
- **MCP server** — plug into Claude Code and other AI agents as a knowledge tool.
- **Collections** — group sources into named sets for scoped search (e.g. "Dev Docs", "Medical").

## Screenshots

| Homepage | Search Results |
|----------|---------------|
| ![Homepage](screenshots/homepage.png) | ![Search](screenshots/search.png) |

| Article Reader | Browse Library |
|----------------|----------------|
| ![Reader](screenshots/reader.png) | ![Browse Library](screenshots/browse-library.png) |

## Install

### Docker

```bash
docker run -v /path/to/zims:/zims -p 8899:8899 epheterson/zimi
```

Starting fresh? Run with an empty directory — browse and download ZIMs from the built-in catalog:

```bash
mkdir zims && docker run -v ./zims:/zims -p 8899:8899 epheterson/zimi
```

Open http://localhost:8899, click the gear icon, and browse the Kiwix catalog.

### macOS Desktop App

**Homebrew:**

```bash
brew tap epheterson/zimi
brew install --cask zimi
```

**Direct download:** [GitHub Releases](https://github.com/epheterson/Zimi/releases) — Apple Silicon and Intel DMGs, signed and notarized.

### Python

```bash
pip install -r requirements.txt
python3 zimi.py serve --port 8899
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
      "args": ["/path/to/zimi_mcp.py"],
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
      "args": ["your-server", "docker", "exec", "-i", "zimi", "python3", "/app/zimi_mcp.py"]
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
| **Cross-source search** | Unified results with relevance ranking | Per-ZIM or combined unranked |
| **Library management** | Built-in catalog browser, downloads, updates | Separate CLI tool (kiwix-manage) |
| **AI integration** | MCP server for Claude Code | None |
| **Desktop app** | Native macOS app | None |
| **Runtime** | Python (~1,600 lines) | C++ (libkiwix) |
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
