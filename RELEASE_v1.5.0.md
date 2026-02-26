# v1.5.0 — "Feel Like Being Online"

Zimi now feels alive. Open the app and you're greeted with fresh content — a NASA photo, a Wikipedia anniversary, a random Gutenberg book, tonight's moon phase. Click a link in one source and land in another. Save what matters, find it again later.

This is the biggest update since launch. Over 1,300 new lines across server and client — every feature designed to make offline knowledge feel like the real thing.

---

## Discover

A daily carousel on the home page, built entirely from your installed ZIM library. Every card is a door into your offline knowledge.

- **Picture of the Day** — NASA APOD's astronomy image
- **On This Day** — Wikipedia historical events for today's date
- **Word of the Day** — from Wiktionary, with part of speech and definition
- **Quote of the Day** — from Wikiquote, beautifully typeset with attribution
- **Country of the Day** — from CIA World Factbook, with locator map
- **Destination, Book, Talk, Comic of the Day** — from Wikivoyage, Gutenberg, TED, xkcd
- **Moon phase** — current lunar phase with illumination and day count
- Loading skeletons while cards fetch. Dismiss with × and bring back anytime. Content is seeded by date — same day, same picks.

![Discover carousel](https://raw.githubusercontent.com/epheterson/Zimi/main/discover-final.jpg)

## Cross-Source Links

Reading a Wikipedia article that links to Wiktionary? Or an entry that references Wikibooks? If you have both installed, the link lights up with a dotted amber underline — click it and you stay in Zimi. Works across the entire wikiverse and beyond — Stack Overflow to GitHub, Wikivoyage to Wikipedia, any source to any source.

- Links to other installed sources are automatically highlighted
- Click to navigate seamlessly across ZIM files
- Batch resolution handles articles with hundreds of links (chunked, parallel)
- Unresolved links look normal — no distracting dimming

## Bookmarks & History

- **Bookmarks**: Click the bookmark icon or press `B` while reading to save an article. View all bookmarks from the Library panel.
- **Search History**: Tap the search bar to see recent searches and articles. Type to filter.
- **Library Panel**: Press `H` to open — tabs for History and Bookmarks, with search and clear.

## Search — 10x Faster with Thumbnails

Search got a complete overhaul under the hood.

- **Thumbnails** in search results — article images appear alongside titles and snippets
- **Smarter snippets** — prefers meta descriptions over boilerplate nav text
- **Parallel full-text search** — each ZIM gets its own thread, results merge as they arrive
- **Deferred snippet extraction** — cross-ZIM search dropped from ~60s to ~1.5s on large libraries

## Other Improvements

- Manual ZIM import via URL — paste a direct link to any `.zim` file
- Catalog language picker with dark theme styling
- Catalog flavor dropdown (replaces multiple download buttons per variant)
- Better size formatting (900+ MB displays as GB)
- Discover Essentials pack — one-click download of all featured ZIM sources from the Catalog
- Daily content persistence via seeded RNG
- Scroll position preserved when navigating back to Discover
- Mobile layout polish — search bar no longer hides navigation on load
- Rate limiting on batch API endpoints
- Socket timeout protection against slow-client connections

## API

New endpoint for cross-source link resolution:

```bash
# Resolve a single URL
curl "http://localhost:8899/resolve?url=https://en.wikipedia.org/wiki/Water"

# Batch resolve
curl -X POST http://localhost:8899/resolve \
  -H "Content-Type: application/json" \
  -d '{"urls": ["https://en.wikipedia.org/wiki/Water", "https://stackoverflow.com/questions/123"]}'
```

---

## Install

**macOS (Homebrew):**
```bash
brew tap epheterson/zimi && brew install --cask zimi
```

**macOS (direct download):**
Download the DMG for your Mac from the assets below.

**Linux (Snap Store):**
```bash
sudo snap install zimi
```

**Docker:**
```bash
docker run -v ./zims:/zims -p 8899:8899 epheterson/zimi
```

**Python (any platform):**
```bash
pip install zimi
zimi serve --port 8899
```
