"""
Offline tests for the 1TamilMV qBittorrent search plugin.

These tests run without any network access.  They inject minimal stand-ins for
the qBittorrent runtime modules (``helpers`` / ``novaprinter``) and drive the
plugin with saved copies of real 1TamilMV pages stored in ``fixtures/``.

Run with pytest or directly::

    python -m pytest tests -v
    python tests/test_offline.py
"""

import html
import importlib.util
import io
import pathlib
import re
import sys
import types
from contextlib import redirect_stdout

FIXTURES = pathlib.Path(__file__).parent / 'fixtures'


# ---- minimal qBittorrent runtime stand-ins --------------------------------
helpers = types.ModuleType('helpers')
helpers.retrieve_url = lambda url: ''  # never used by the offline tests
novaprinter = types.ModuleType('novaprinter')


def _fake_pretty_printer(item):
    out = '|'.join((
        item['link'],
        item['name'].replace('|', ' '),
        str(item['size']),
        str(item['seeds']),
        str(item['leech']),
        item['engine_url'],
        item.get('desc_link', ''),
        str(item.get('pub_date', -1)),
    ))
    print(out)


novaprinter.prettyPrinter = _fake_pretty_printer
sys.modules['helpers'] = helpers
sys.modules['novaprinter'] = novaprinter

_engine_path = pathlib.Path(__file__).resolve().parent.parent / 'tamilmv.py'
_spec = importlib.util.spec_from_file_location('tamilmv_under_test',
                                               str(_engine_path))
ENGINE = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(ENGINE)


def _page(fixture_name):
    with open(str(FIXTURES / fixture_name), encoding='utf-8',
              errors='replace') as handle:
        return html.unescape(handle.read())


def make_engine():
    """Engine whose network calls are served by fixture pages."""
    eng = ENGINE.tamilmv()

    def fake_get(url):
        if '/search/' in url:
            if 'q=leo' in url:
                return '<html><body>no results</body></html>'
            return _page('search_1080p.html')
        if 'topic/199651' in url:
            return _page('topic_undisputed.html')
        if 'topic/199676' in url:
            return _page('topic_kaththi_ddl.html')
        if 'topic/199652' in url:
            return _page('topic_biggboss.html')
        return ''

    eng._get = fake_get
    eng._first_working_base = lambda term: 'https://1tamilmv.fi'
    return eng


def test_parse_search_results():
    eng = make_engine()
    items = eng._parse_search_results(_page('search_1080p.html'))
    assert len(items) == 25
    first = items[0]
    assert first['forum_id'] == 17
    assert first['title'].startswith('Undisputed III')
    assert '/topic/' in str(first['url'])
    assert '&do=' not in str(first['url'])


def test_parse_topic_undisputed():
    eng = make_engine()
    rels = eng._parse_topic_releases(_page('topic_undisputed.html'),
                                     'Undisputed III (2010)')
    assert len(rels) == 3
    assert all(str(r['link']).startswith('https://www.1tamilmv.meme/')
               for r in rels)
    assert all(int(r['size']) > 0 for r in rels)
    assert '1080p' in str(rels[0]['name'])


def test_parse_topic_biggboss():
    eng = make_engine()
    rels = eng._parse_topic_releases(_page('topic_biggboss.html'),
                                     'BIGG BOSS Tamil')
    # 4 episodes x 3 qualities -> 12 releases
    assert len(rels) == 12


def test_ddl_only_topic_has_no_releases():
    eng = make_engine()
    rels = eng._parse_topic_releases(_page('topic_kaththi_ddl.html'),
                                     'Kaththi Sandai (2016)')
    assert rels == []


def test_magnet_only_releases():
    eng = make_engine()
    page = _page('topic_biggboss.html')
    # strip every hosted .torrent anchor -> only magnet links remain
    page = re.sub(r'<a[^>]*attachment\.php[^>]*>.*?</a>', '', page,
                  flags=re.DOTALL)
    rels = eng._parse_topic_releases(page, 'BIGG BOSS Tamil')
    assert len(rels) == 12
    assert all(str(r['link']).startswith('magnet:') for r in rels)


def test_magnet_and_torrent_dedup_prefers_torrent_file():
    eng = make_engine()
    sample = ("<div data-role='commentContent'>"
              "<a href='https://x/applications/core/interface/file/"
              "attachment.php?id=1&amp;key=k'>"
              "www.1TamilMV.meme - Movie 2024 1080p 2GB.mkv.torrent</a> "
              "<a href='magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef"
              "01234567&amp;dn=www.1TamilMV.meme%20-%20Movie%202024%201080p"
              "%202GB.mkv'>MAGNET</a></div>")
    sample = html.unescape(sample)
    rels = eng._parse_topic_releases(sample, 'Movie')
    assert len(rels) == 1
    assert 'attachment.php' in str(rels[0]['link'])
    assert int(rels[0]['size']) == 2 * 2**30


def test_plain_text_magnet():
    eng = make_engine()
    text_only = ("<div data-role='commentContent'>"
                 "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
                 "&dn=Test%20Movie%202024%201080p%202GB.mkv</div>")
    rels = eng._parse_topic_releases(text_only, 'Test Movie')
    assert len(rels) == 1
    assert rels[0]['name'].startswith('Test Movie')


def test_full_search_movies():
    eng = make_engine()
    eng._MAX_TOPICS = 25  # ensure every fixture topic is visited
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        eng.search('1080p', 'movies')
    lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
    # only the movie-forum fixtures yield releases: Undisputed (3)
    assert len(lines) == 3
    assert all(line.count('|') == 7 for line in lines)
    first = lines[0].split('|')
    assert first[1].startswith('Undisputed')
    assert first[3] == '-1' and first[4] == '-1'  # seeds/leech unknown


def test_first_working_base_skips_interstitials():
    eng = ENGINE.tamilmv()
    eng._RETRY_DELAY = 0.0
    interstitial = ('<html><head><title>Loading...</title></head><body>'
                    "<script>window.location.replace('/?ch=1&js=eyJ')"
                    '</script></body></html>')
    pages = {
        'https://www.1tamilmv.capital': interstitial,
        'https://1tamilmv.fi': '',
        'https://www.1tamilmv.meme': _page('search_1080p.html'),
    }
    eng._get = lambda url: pages.get(url.split('/index.php')[0], '')
    assert eng._first_working_base('x') == 'https://www.1tamilmv.meme'


def test_first_working_base_none_when_all_broken():
    eng = ENGINE.tamilmv()
    eng._RETRY_DELAY = 0.0
    eng._get = lambda url: '<html>Loading...</html>'
    assert eng._first_working_base('x') is None


def test_get_retries_empty_response_then_succeeds():
    original = ENGINE.retrieve_url
    calls = []

    def flaky(url):
        calls.append(url)
        return '' if len(calls) < 3 else '<html>ok</html>'

    try:
        ENGINE.retrieve_url = flaky
        eng = ENGINE.tamilmv()
        eng._RETRY_DELAY = 0.0
        assert eng._get('http://example.invalid') == '<html>ok</html>'
        assert len(calls) == 3  # two failures, then the retry that succeeds
    finally:
        ENGINE.retrieve_url = original


def test_get_gives_up_after_retries():
    original = ENGINE.retrieve_url
    calls = []

    def always_fail(url):
        calls.append(url)
        return ''

    try:
        ENGINE.retrieve_url = always_fail
        eng = ENGINE.tamilmv()
        eng._RETRY_DELAY = 0.0
        assert eng._get('http://example.invalid') == ''
        assert len(calls) == eng._RETRIES + 1
    finally:
        ENGINE.retrieve_url = original


if __name__ == '__main__':
    test_parse_search_results()
    test_parse_topic_undisputed()
    test_parse_topic_biggboss()
    test_ddl_only_topic_has_no_releases()
    test_magnet_only_releases()
    test_magnet_and_torrent_dedup_prefers_torrent_file()
    test_plain_text_magnet()
    test_full_search_movies()
    test_first_working_base_skips_interstitials()
    test_first_working_base_none_when_all_broken()
    test_get_retries_empty_response_then_succeeds()
    test_get_gives_up_after_retries()
    print('ALL OFFLINE TESTS PASSED')
