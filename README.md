# 1TamilMV search plugin for qBittorrent

A [qBittorrent search engine plugin](https://github.com/qbittorrent/search-plugins/wiki/How-to-write-a-search-plugin)
that searches **1TamilMV** (the community forum at `https://1tamilmv.fi`,
which redirects to the current live mirror) and returns its hosted
`.torrent` files / `magnet:` links as search results.

## How it works

1TamilMV runs on Invision Community forum software. Its search-result page
shows **topics**, but does **not** include the download links in the result
list — they live inside the topic posts. So the plugin:

1. queries the forum's quick-search endpoint for *topics*
   (`/index.php?/search/&q=...&type=forums_topic`),
2. fetches the first page of each matching topic,
3. extracts every hosted `.torrent` attachment and `magnet:` link from the
   posts and prints one result per *release* (e.g. the 1080p, 720p and
   450 MB variants of the same movie each become their own row).

Result fields:

| field | value |
|---|---|
| `link` | the hosted `.torrent` file URL when present, otherwise the `magnet:` URI |
| `name` | cleaned release name (site-name prefix and `.torrent` suffix removed) |
| `size` | parsed from the filename (e.g. `2GB` → bytes), `-1` if unknown |
| `seeds` / `leech` | `-1` (the forum has no seed/leech counters) |
| `engine_url` | `https://1tamilmv.fi` |
| `desc_link` | the topic page on the forum |
| `pub_date` | topic timestamp |

## Installation

1. Download `tamilmv.py` (this repo).
2. In qBittorrent open the **Search** tab.
3. Click **Search engines...** → **Install a new one** → select `tamilmv.py`.
4. The engine shows up as **1TamilMV**.

qBittorrent names the engine after the plugin *file* and then looks up a class
of the same name, so `tamilmv.py` is the canonical file name. Saving it as
`1tamilmv.py`, `TamilMV.py` or `1TamilMV.py` also works — the plugin
registers aliases for those spellings — but any other name (say `tamil.py`)
is rejected with *"Plugin tamil is not supported."*.

Requires Python ≥ 3.7. Only the Python standard library is used (plus the
`helpers` / `novaprinter` runtime modules that ship with qBittorrent).

### Updating the plugin

qBittorrent only auto-updates the plugins listed in the *official* source
(`qbittorrent/search-plugins`, `master` branch). A plugin like this one is
installed by hand, so bump `# VERSION:` before reinstalling: qBittorrent
refuses a file whose version is not newer than the installed one
(*"A more recent version of this plugin is already installed."*). The version
must have exactly two numeric components (`1.1`, not `1` or `1.1.2`).

Installing from a URL works too (local file picker → *URL*, or the WebUI's
*Install a search plugin* box): append the plugin name to a raw link, e.g.
`https://raw.githubusercontent.com/<you>/<repo>/refs/heads/main/tamilmv.py`.
The plugin name is taken from the last path segment minus `.py`, so the URL
must end in `/tamilmv.py` (or one of the accepted variants).

### "Plugin <name> is not supported."

qBittorrent reports this whenever the engine does not appear in the search
runtime's capability list, which happens for **every** plugin when its
bundled `nova3` runtime cannot run (qBittorrent ≥ 5.2 needs a recent Python 3,
and it must be on qBittorrent's `PATH`). To tell the two cases apart, run the
probe qBittorrent itself runs:

```bash
python3 -I -X utf8 "<data-dir>/nova3/nova2.py" --capabilities --names
```

`tamilmv` missing from that list means an import/runtime problem — check the
qBittorrent log for the line `Error occurred when fetching search engine
capabilities. Error: "..."`, it contains the underlying Python error.

## Categories

1TamilMV is primarily a movie/Web-series site, so the plugin advertises:

* `all`, `movies`, `tv`, `music`, `software`, `games`, `books`

Category filtering is done **client-side**: the whole site is searched and
results whose forum does not belong to the selected category are dropped.
Forum ids are mapped in the `_FORUMS_*` class attributes at the top of the
plugin. **If the site reorganizes its boards, update those sets.**

## Tuning

The plugin is deliberately conservative about request volume. Edit the class
attributes to change behaviour:

```python
_MAX_SEARCH_PAGES = 3   # search result pages to scan
_MAX_TOPICS      = 15   # matching topics to open (1 HTTP GET each)
_MAX_RESULTS     = 120  # hard cap on printed results
_TIMEOUT         = 30.0 # per-request timeout (seconds)
_RETRIES         = 2    # extra attempts when a request comes back empty
_RETRY_DELAY     = 2.0  # base backoff between attempts (seconds)
_MIRRORS         = (...)  # fallback domains, first healthy one wins
```

`_MIRRORS` is only used to find a host that serves search pages; the result
links themselves come from the page markup, so they always point at the
canonical host. A mirror is accepted only when its answer contains the IPS
result-list marker (`resultsContents`), so dead domains and JS anti-bot
interstitials are skipped instead of being mistaken for a working site.

Empty responses are retried `_RETRIES` times with a growing backoff, which
covers the origin's intermittent Cloudflare *error 525* replies (they surface
as an empty body through `retrieve_url`, since it swallows every transport
error).

Note: if a topic is DDL-only (no torrent/magnet in the posts, which is
common for "Watch online" releases), it simply contributes no results.

## Development

### Offline tests (no network)

```bash
python3 tests/test_offline.py          # or: python3 -m pytest tests -q
```

The tests replay real saved 1TamilMV pages from `tests/fixtures/` and stub
out the qBittorrent runtime modules.

### Live test with the official nova3 harness

qBittorrent's search plugins are normally tested with the `nova3` folder of
the qBittorrent source tree (`helpers.py`, `novaprinter.py`, `nova2.py`,
`socks.py`). Copy `tamilmv.py` into its `engines/` folder and run:

```bash
python3 nova2.py tamilmv movies "jailer 2023"
# or
python3 nova2.py tamilmv all "leo 2023"
```

## Caveats

* The domain is unstable (it is a piracy-indexed site). Almost every mirror
  in `_MIRRORS` now redirects to `www.1tamilmv.capital`, which is listed
  first; if the site moves again, add the current mirror there (and keep an
  eye on `resultsContents`, which the mirror probe relies on).
* `www.1tamilmv.art` is intentionally **not** listed: it answers HTTP 200 with
  a JS anti-bot interstitial instead of a search page.
* Short search terms (≤ 3 characters) return no results because of the
  forum's own search index minimums.
* The site is frequently slow; keep `_MAX_TOPICS` low for snappier searches.
