# VERSION: 1.1
# AUTHORS: Your Name (you@example.com)

# LICENSING INFORMATION
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
qBittorrent search plugin for 1TamilMV (https://1tamilmv.fi).

1TamilMV is an Invision Community forum.  Its search endpoint does not expose
torrent/magnet links directly on the result page, so this plugin:

  1. queries the IPS quick-search endpoint for *topics*,
  2. fetches the first page of every matching topic,
  3. extracts the hosted ``.torrent`` attachments / ``magnet:`` links that are
     embedded in the topic's posts and prints one result per release.

The site has no seed/leech counters, therefore ``seeds`` / ``leech`` are -1.

The forum's category structure changes from time to time, keep the
``_FORUMS_*`` class attributes below in sync with reality if the site moves
its boards around.
"""

import re
import socket
import time
import urllib.parse
from typing import Dict, List, Optional, Set, Tuple

from helpers import retrieve_url
from novaprinter import prettyPrinter


class tamilmv:
    """Search engine plugin for 1TamilMV."""

    url = 'https://1tamilmv.fi'
    name = '1TamilMV'
    supported_categories: Dict[str, str] = {
        'all': 'all',
        'books': 'books',
        'games': 'games',
        'movies': 'movies',
        'music': 'music',
        'software': 'software',
        'tv': 'tv',
    }

    # Entry point + mirrors used as fallback if the first one does not answer.
    # ``www.1tamilmv.art`` is deliberately absent: it answers HTTP 200 with an
    # anti-bot interstitial instead of a search page.
    _MIRRORS: Tuple[str, ...] = (
        'https://www.1tamilmv.capital',   # current canonical host
        'https://1tamilmv.fi',            # 301 -> www.1tamilmv.capital
        'https://www.1tamilmv.meme',
        'https://www.1tamilmv.cards',
        'https://www.1tamilmv.li',
        'https://www.1tamilmv.pizza',
    )

    # ---- tuning -----------------------------------------------------------
    _MAX_SEARCH_PAGES = 3      # how many search result pages to scan
    _MAX_TOPICS = 15           # how many matching topics to open for details
    _MAX_RESULTS = 120         # hard cap on the number of printed results
    _TIMEOUT = 30.0            # per-request timeout (seconds)
    _RETRIES = 2               # extra attempts when a request comes back empty
    _RETRY_DELAY = 2.0         # base backoff between attempts (seconds)

    # ---- 1TamilMV forums -> qBittorrent categories -------------------------
    # Forum ids were read from /forums/ ; leaf boards contain the actual
    # topics.  News/chat/request/DDL-only boards are intentionally omitted.
    _FORUMS_MOVIES: Set[int] = {
        9, 10, 11, 12, 13, 14, 17,          # Tamil + Hollywood multi-audio
        22, 23, 24, 25, 26, 27, 31,         # Telugu (+ dubbed)
        34, 35, 36, 37, 38, 39, 42,         # Malayalam (+ dubbed)
        45, 46, 49, 50,                     # English
        56, 57, 58, 59, 60, 61, 64,         # Hindi (+ dubbed)
        67, 68, 69, 70, 71, 72,             # Kannada
    }
    _FORUMS_TV: Set[int] = {
        19, 33, 44, 55, 66, 77,             # WEB-SERIES / TV SHOWS
        105, 106, 108, 109,                 # WWE shows / PPV
    }
    _FORUMS_MUSIC: Set[int] = {
        15, 16, 28, 30, 40, 41, 51, 52, 62, 63, 73, 74,
    }
    _FORUMS_SOFTWARE: Set[int] = {85, 86, 87, 88, 90}
    _FORUMS_GAMES: Set[int] = {85}
    _FORUMS_BOOKS: Set[int] = {91}

    _CATEGORY_FORUMS: Dict[str, Set[int]] = {
        'movies': _FORUMS_MOVIES,
        'tv': _FORUMS_TV,
        'music': _FORUMS_MUSIC,
        'software': _FORUMS_SOFTWARE,
        'games': _FORUMS_GAMES,
        'books': _FORUMS_BOOKS,
    }

    _SIZE_RE = re.compile(
        r'(?<![\w.])(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB)(?!\w)', re.IGNORECASE)
    _SIZE_EXP = {'KB': 10, 'MB': 20, 'GB': 30, 'TB': 40}
    _DOMAIN_PREFIX_RE = re.compile(
        r'^(?:https?://)?(?:www\.)?1tamilmv\.[a-z]{2,}\s*[-]\s*',
        re.IGNORECASE)
    _BTIH_RE = re.compile(r'urn:btih:([0-9a-fA-F]{40})')
    _REPLY_RE = re.compile(r'^[\d.,]+\s*(repl\w*|comments?|views?)\s*$',
                           re.IGNORECASE)
    _TOPIC_HREF_RE = re.compile(
        r'<a\b[^>]*\bhref=[\'"]([^\'"]*?/forums/topic/[^\'"]*)[\'"][^>]*>'
        r'(.*?)</a>', re.DOTALL)
    _ANCHOR_RE = re.compile(r'<a\b([^>]*)>(.*?)</a>', re.DOTALL)
    _RAW_MAGNET_RE = re.compile(
        r'magnet:\?xt=urn:btih:[0-9a-fA-F]{40}(?:[&?][^\s"\'<>]+)?',
        re.IGNORECASE)

    def __init__(self) -> None:
        self._seen_hashes: Set[str] = set()
        self._seen_names: Set[str] = set()

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _collapse(text: str) -> str:
        return re.sub(r'\s+', ' ', text).strip()

    @staticmethod
    def _strip_tags(html: str) -> str:
        return re.sub(r'<[^>]+>', '', html)

    @staticmethod
    def _clean_name(raw: str) -> str:
        """Trim the leading site-name prefix and trailing ``.torrent`` suffix."""
        name = raw.strip()
        name = re.sub(tamilmv._DOMAIN_PREFIX_RE, '', name)
        name = re.sub(r'\.torrent\s*$', '', name, flags=re.IGNORECASE)
        # magnet dn values sometimes use NBSP (\xa0) where the attachment
        # filename uses a plain space; normalise so both deduplicate.
        name = name.replace('\xa0', ' ')
        name = re.sub(r'\s+', ' ', name)
        return name.strip()

    @classmethod
    def _parse_size(cls, name: str) -> int:
        """Extract a '2 GB' / '450 MB' token from a release name -> bytes."""
        match = cls._SIZE_RE.search(name)
        if match is None:
            return -1
        exponent = cls._SIZE_EXP[match.group(2).upper()]
        return int(float(match.group(1)) * (1 << exponent))

    # ---------------------------------------------------------------- network
    def _get(self, url: str) -> str:
        """GET ``url``, retrying a few times when the request comes back empty.

        ``retrieve_url`` swallows every transport error and returns ``''``, so
        a transient failure (the origin intermittently answers Cloudflare
        *error 525* on these hosts) is indistinguishable from a dead URL here.
        Retrying with a growing backoff recovers the pages it would otherwise
        silently drop.
        """
        for attempt in range(self._RETRIES + 1):
            html = retrieve_url(url)
            if html:
                return html
            if attempt < self._RETRIES:
                time.sleep(self._RETRY_DELAY * (attempt + 1))
        return ''

    def _first_working_base(self, term: str) -> Optional[str]:
        """Try every mirror until one serves a real search result page.

        The body is checked for the IPS result-list marker rather than just
        "not empty": dead/parked mirrors answer HTTP 200 with a JS anti-bot
        interstitial, which would otherwise be accepted and quietly yield no
        results at all.
        """
        for base in self._MIRRORS:
            if 'resultsContents' in self._get(self._search_url(base, term, 1)):
                return base
        return None

    @staticmethod
    def _search_url(base: str, term: str, page: int) -> str:
        url = '{}/index.php?/search/&q={}&type=forums_topic'.format(
            base.rstrip('/'), term)
        if page > 1:
            url += '&page={}'.format(page)
        return url

    # ------------------------------------------------------------- category
    @classmethod
    def _allowed_forums(cls, category: str) -> Optional[Set[int]]:
        if category == 'all':
            allowed: Set[int] = set()
            for forums in cls._CATEGORY_FORUMS.values():
                allowed |= forums
            return allowed
        return cls._CATEGORY_FORUMS.get(category)

    # ------------------------------------------------------- search parsing
    def _parse_search_results(self, html: str) -> List[Dict[str, object]]:
        """Parse the <ol data-role='resultsContents'> topic list."""
        items: List[Dict[str, object]] = []
        # each search result starts with <li class='ipsStreamItem ...'
        blocks = re.split(r"<li class=['\"]ipsStreamItem", html)
        for block in blocks[1:]:
            forum_match = re.search(r'/forums/forum/(\d+)-', block)
            time_match = re.search(r"data-timestamp=['\"](\d+)['\"]", block)
            title, url = self._extract_title(block)
            if not url:
                continue
            items.append({
                'url': url,
                'title': title,
                'forum_id': int(forum_match.group(1)) if forum_match else 0,
                'pub_date': int(time_match.group(1)) if time_match else -1,
            })
        return items

    def _extract_title(self, block: str) -> Tuple[str, str]:
        """Return (title, clean topic url) of one search-result item."""
        best_title = ''
        best_url = ''
        for match in self._TOPIC_HREF_RE.finditer(block):
            url = match.group(1)
            text = self._collapse(self._strip_tags(match.group(2)))
            text = urllib.parse.unquote(text)
            if not text or self._REPLY_RE.match(text):
                continue
            # first anchor with a real title wins
            return text, url.split('&do=')[0]
        # fallback: any topic link, long text
        for match in self._TOPIC_HREF_RE.finditer(block):
            text = self._collapse(self._strip_tags(match.group(2)))
            if len(text) > len(best_title):
                best_title = text
                best_url = match.group(1)
        return (urllib.parse.unquote(best_title), best_url.split('&do=')[0]
                if best_url else best_url)

    # -------------------------------------------------------- topic parsing
    def _topic_region(self, html: str) -> str:
        start = html.find("data-role='commentContent'")
        if start == -1:
            start = html.find('data-role="commentContent"')
        if start == -1:
            return ''
        end = html.find('data-role="replyArea"', start)
        if end == -1:
            end = html.find("data-role='replyArea'", start)
        if end == -1:
            end = len(html)
        return html[start:end]

    def _magnet_name(self, magnet_url: str, fallback: str = '') -> str:
        query = urllib.parse.urlparse(magnet_url).query
        dn = urllib.parse.parse_qs(query).get('dn')
        if dn and dn[0]:
            return self._clean_name(urllib.parse.unquote(dn[0]))
        return fallback

    def _parse_topic_releases(self, html: str,
                              topic_title: str) -> List[Dict[str, object]]:
        """Extract per-release rows: link (torrent file or magnet) + name."""
        region = self._topic_region(html)
        if not region:
            return []

        # magnet URIs that are proper anchors or plain text in the posts
        magnet_urls: List[str] = [m for m in
                                  self._RAW_MAGNET_RE.findall(region)]
        for match in self._ANCHOR_RE.finditer(region):
            tag = match.group(1)
            href = re.search(r"""\bhref=(['"])(.*?)\1""", tag, re.DOTALL)
            if href:
                value = href.group(2).strip()
                if value.lower().startswith('magnet:'):
                    magnet_urls.append(value)

        # torrent attachments anchored to the forum's attachment.php
        attachments: List[Tuple[str, str]] = []  # (url, name)
        for match in self._ANCHOR_RE.finditer(region):
            tag = match.group(1)
            href = re.search(r"""\bhref=(['"])(.*?)\1""", tag, re.DOTALL)
            if not href:
                continue
            url = href.group(2).strip()
            low = url.lower()
            if 'attachment.php' not in low and not low.endswith('.torrent'):
                continue
            text = self._collapse(self._strip_tags(match.group(2)))
            if not text:
                title = re.search(r"""\btitle=(['"])(.*?)\1""", tag,
                                  re.DOTALL)
                if title:
                    text = self._collapse(title.group(2))
            attachments.append((url, text))

        fallback_name = self._clean_name(topic_title)
        releases: List[Dict[str, object]] = []

        # 1) hosted .torrent files first (they carry trackers and exact size)
        for url, raw_name in attachments:
            name = self._clean_name(raw_name) or fallback_name
            key = name.lower()
            if key in self._seen_names or not name:
                continue
            self._seen_names.add(key)
            releases.append({'link': url, 'name': name,
                             'size': self._parse_size(name)})

        # 2) magnet links that do not duplicate a torrent file above
        for magnet in magnet_urls:
            if '&amp;' in magnet:
                magnet = magnet.replace('&amp;', '&')
            hash_match = self._BTIH_RE.search(magnet)
            btih = hash_match.group(1).lower() if hash_match else ''
            name = self._magnet_name(magnet, fallback_name)
            key = name.lower()
            if not name or key in self._seen_names or btih in self._seen_hashes:
                continue
            if btih:
                self._seen_hashes.add(btih)
            self._seen_names.add(key)
            releases.append({'link': magnet, 'name': name,
                             'size': self._parse_size(name)})
        return releases

    # ------------------------------------------------------------------ main
    def search(self, query: str, category: str = 'all') -> None:
        """
        Search 1TamilMV topics, then extract torrent/magnet links from each
        matching topic's first page.

        `query` is a string with the search tokens, already escaped by
        nova2 (e.g. "Leo+2023").
        """
        socket.setdefaulttimeout(self._TIMEOUT)
        cat = category if category in self.supported_categories else 'all'
        allowed = self._allowed_forums(cat)
        if allowed is None:
            return

        term = query.replace('+', '%20')
        base = self._first_working_base(term)
        if base is None:
            return

        # Collect candidate topics (bounded)
        candidates: List[Dict[str, object]] = []
        for page in range(1, self._MAX_SEARCH_PAGES + 1):
            html = self._get(self._search_url(base, term, page))
            for item in self._parse_search_results(html):
                if int(item['forum_id']) not in allowed:
                    continue
                candidates.append(item)
                if len(candidates) >= self._MAX_TOPICS:
                    break
            if len(candidates) >= self._MAX_TOPICS or not html:
                break

        # Fetch topic pages sequentially and print results as they stream in.
        emitted = 0
        for item in candidates:
            html = self._get(str(item['url']))
            if emitted >= self._MAX_RESULTS:
                break
            for rel in self._parse_topic_releases(
                    html, str(item['title'])):
                prettyPrinter({
                    'link': str(rel['link']),
                    'name': str(rel['name']),
                    'size': int(rel['size']),
                    'seeds': -1,
                    'leech': -1,
                    'engine_url': self.url,
                    'desc_link': str(item['url']),
                    'pub_date': int(item['pub_date']),
                })  # type: ignore[arg-type]
                emitted += 1
                if emitted >= self._MAX_RESULTS:
                    break


# qBittorrent names the engine after the plugin *file* and then looks up a
# class of that name (``getattr(module, module_name)``), so the canonical file
# name is ``tamilmv.py``.  People routinely save it as ``1tamilmv.py`` instead
# (or capitalise it), which fails with "Plugin 1tamilmv is not supported."
# because a Python class cannot be named ``1tamilmv`` at all.  Register the
# plausible aliases so those file names install too.
for _alias in ('1tamilmv', 'TamilMV', '1TamilMV', 'tamilmv'):
    globals().setdefault(_alias, tamilmv)
del _alias


if __name__ == '__main__':
    # Minimal offline sanity check (no network required):
    #   python3 tamilmv.py
    import sys
    print('plugin ok', file=sys.stderr)
