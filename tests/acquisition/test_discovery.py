from dataclasses import replace

import pytest

from hanoitourist_crawler.acquisition.discovery import listing_page
from hanoitourist_crawler.models import RawDocument, uid
from tests.conftest import FIXTURES

URL = 'https://hanoitourist.vn/tour-trong-nuoc'


def page(name='domestic_listing_page_1.html', url=URL):
    return RawDocument(uid(), 1, 'dom', 'listing_state', 'listing:0', url,
                       (FIXTURES / name).read_bytes())


def test_scope_pager_and_identity():
    result = listing_page(page())
    assert len(result['cards']) == 2 and not result['errors']
    assert result['next_url'] == URL + '?page=1' and not result['terminal']
    assert {c['source_tour_id'] for c in result['cards']} == {'8140', '8073'}
    assert all(c['provenance'] for c in result['cards'])


def test_terminal_ignores_footer_pager():
    result = listing_page(page('domestic_listing_page_2.html', URL + '?page=1'))
    assert result['terminal'] and result['next_url'] is None


@pytest.mark.parametrize('href', ['', 'https://example.com/tour', '/tour/8140/lich/1/booking'])
def test_unresolved_card_is_not_dropped(href):
    doc = page()
    changed = replace(doc, body=doc.body.replace(b'/hnt-san-may-tren-dinh-ta-xua', href.encode()))
    result = listing_page(changed)
    assert len(result['cards']) == 2 and result['errors']
    assert result['cards'][1]['identity_error']


def test_missing_scope_is_not_empty_success():
    result = listing_page(replace(page(), body=b'<main>No listing</main>'))
    assert result['errors'] and not result['terminal']


def test_explicit_empty():
    body = b'<main><div class="view-id-tour view-display-id-page_trong_nuoc"><div class="view-empty">No tours</div></div></main>'
    result = listing_page(replace(page(), body=body))
    assert result['terminal'] and result['cards'] == [] and not result['errors']


def browser_discovery(monkeypatch, *, html=None, status=200, **overrides):
    from hanoitourist_crawler.config import Config
    from hanoitourist_crawler.acquisition.discovery import DiscoveryCollector
    from tests.acquisition.test_collector import MemoryStorage, mock_robots, FetchResponse
    mock_robots(monkeypatch)
    policy = DiscoveryCollector.route
    visited = []
    def route(self, actual):
        request = actual.request
        class Proxy:
            def __init__(self): self.request = request
            def abort(self, **kwargs): actual.abort(**kwargs)
            def fulfill(self, **kwargs): actual.fulfill(**kwargs)
            def fetch(self, **kwargs):
                assert kwargs['max_redirects']==kwargs['max_retries']==0
                visited.append(request.url)
                body = html if html is not None else (FIXTURES / (
                    'domestic_listing_page_2.html' if '?page=1' in request.url else 'domestic_listing_page_1.html')).read_text()
                return FetchResponse(request.url,status,body.encode(),{'content-type':'text/html','retry-after':'999'})
        policy(self, Proxy())
    monkeypatch.setattr(DiscoveryCollector, 'route', route)
    cfg = replace(Config(database_url='unused',request_interval=0,navigation_timeout=2,discovery_timeout=30), **overrides)
    store = MemoryStorage()
    capture = DiscoveryCollector(cfg,store,uid())
    return capture, store, visited


@pytest.mark.browser
def test_browser_paging_and_staging(monkeypatch,chromium_available):
    capture,store,visited = browser_discovery(monkeypatch)
    result=capture.collect()
    assert result.status=='complete' and result.terminal
    assert {card['url'] for card in result.cards} == {
        'https://hanoitourist.vn/kham-pha-phu-quoc-hon-ngoc-vien-dong',
        'https://hanoitourist.vn/hnt-san-may-tren-dinh-ta-xua',
        'https://hanoitourist.vn/vinh-ha-long-du-thuyen'}
    assert visited==[URL,URL+'?page=1']
    assert len(store.docs)==5 and all(v=='captured' for v in store.run['manifest']['actions'].values())


@pytest.mark.browser
def test_browser_dynamic_render(monkeypatch,chromium_available):
    capture,store,_=browser_discovery(monkeypatch,html=(FIXTURES/'dynamic_domestic_listing.html').read_text())
    result=capture.collect()
    assert result.status=='complete' and result.cards
    assert any(doc.kind=='dom' for doc in store.docs)


@pytest.mark.browser
@pytest.mark.parametrize('status',[403,429,503])
def test_browser_navigation_errors_do_not_bypass(monkeypatch,chromium_available,status):
    capture,store,visited=browser_discovery(monkeypatch,status=status)
    result=capture.collect()
    assert result.status=='failed' and not result.terminal
    assert any(doc.http_status==status for doc in store.docs)
    assert len(visited)==1


@pytest.mark.browser
@pytest.mark.parametrize('overrides',[{'max_listing_pages':1},{'max_states':1},{'max_document_bytes':100}])
def test_browser_limits_never_complete(monkeypatch,chromium_available,overrides):
    capture,store,_=browser_discovery(monkeypatch,**overrides)
    assert capture.collect().status!='complete'
    assert store.docs


@pytest.mark.browser
def test_browser_js_failure_is_not_html_fallback_success(monkeypatch,chromium_available):
    html=(FIXTURES/'domestic_listing_page_2.html').read_text()+'<script>throw new Error("broken")</script>'
    capture,store,_=browser_discovery(monkeypatch,html=html)
    result=capture.collect()
    assert result.status=='partial' and any('JavaScript' in error for error in result.errors)


@pytest.mark.browser
def test_browser_pager_loop_is_partial(monkeypatch,chromium_available):
    html=(FIXTURES/'domestic_listing_page_1.html').read_text().replace('?page=1','?page=0')
    capture,_,visited=browser_discovery(monkeypatch,html=html)
    assert capture.collect().status=='partial' and visited==[URL]


@pytest.mark.browser
def test_unresolved_source_card_is_not_hidden_by_render(monkeypatch,chromium_available):
    html=(FIXTURES/'domestic_listing_page_2.html').read_text().replace('vinh-ha-long-du-thuyen','tour-trong-nuoc')
    html+="<script>document.querySelector('.tour-title h3 a').setAttribute('href','/corrected-tour');</script>"
    capture,_,_=browser_discovery(monkeypatch,html=html)
    result=capture.collect()
    assert result.status=='partial' and not result.terminal
    assert any(card['identity_error'] for card in result.cards)


def html_routes(monkeypatch, *, pages=None, status=200, headers=None, robots='User-agent: *\nDisallow: /core/'):
    from playwright.sync_api import Route
    from tests.acquisition.test_collector import FetchResponse, mock_robots
    mock_robots(monkeypatch, robots)
    pages = pages if pages is not None else {
        URL: (FIXTURES / 'domestic_html_listing_page_1.html').read_text(),
        URL + '?page=1': (FIXTURES / 'domestic_html_listing_page_2.html').read_text()}
    fetched, responses = [], []
    def fetch(route, **options):
        assert options['max_redirects'] == options['max_retries'] == 0 and options['timeout'] > 0
        fetched.append(route.request.url)
        value = pages.get(route.request.url, '')
        code, metadata, body = value if isinstance(value, tuple) else (
            status, headers or {'content-type': 'text/html', 'retry-after': '999'}, value)
        response = FetchResponse(route.request.url, code, body.encode(), metadata)
        responses.append(response)
        return response
    monkeypatch.setattr(Route, 'fetch', fetch)
    return fetched, responses


def html_discovery(monkeypatch, *, pages=None, status=200, headers=None, robots='User-agent: *\nDisallow: /core/', **overrides):
    from hanoitourist_crawler.config import Config
    from hanoitourist_crawler.acquisition.discovery import discover_domestic
    from tests.acquisition.test_collector import MemoryStorage
    fetched, responses = html_routes(monkeypatch, pages=pages, status=status, headers=headers, robots=robots)
    cfg = replace(Config(database_url='unused', request_interval=0, navigation_timeout=2,
                         discovery_timeout=15), **overrides)
    store = MemoryStorage()
    result = discover_domestic(cfg, store, uid())
    return result, store, fetched, responses


@pytest.mark.browser
def test_html_public_discovers_source_without_js(monkeypatch, chromium_available):
    result, store, fetched, responses = html_discovery(monkeypatch)
    assert fetched == [URL, URL + '?page=1']
    assert result.status == 'partial' and result.terminal is False
    assert [card['title'] for card in result.cards] == ['Tour Phú Quốc', 'Tour mới', 'Tour Phú Quốc']
    assert [doc.kind for doc in store.docs] == ['robots', 'http_document', 'http_document']
    manifest = store.run['manifest']
    assert manifest['discovery_adapter'] == 'domestic-html-best-effort-v1'
    assert manifest['coverage_guaranteed'] is False and manifest['source_pager_terminal_observed'] is True
    assert manifest['duplicate_card_count'] == 1
    assert all(response.disposed for response in responses)
    for card in result.cards:
        assert card['provenance'][0]['raw_document_id'] in {doc.id for doc in store.docs if doc.role == 'listing'}
    assert any('coverage' in error.lower() for error in result.errors)


@pytest.mark.browser
def test_html_empty_never_claims_complete(monkeypatch, chromium_available):
    result, store, fetched, _ = html_discovery(monkeypatch, pages={
        URL: (FIXTURES / 'domestic_html_listing_empty.html').read_text()})
    assert result.status == 'partial' and result.terminal is False and result.cards == []
    assert fetched == [URL] and store.run['manifest']['source_pager_terminal_observed'] is True


@pytest.mark.browser
@pytest.mark.parametrize('status', [204, 401, 403, 404, 410, 429, 503])
def test_html_http_failures_keep_raw_without_cards(monkeypatch, chromium_available, status):
    result, store, fetched, responses = html_discovery(monkeypatch, status=status)
    assert result.status == 'failed' and result.terminal is False and not result.cards
    assert fetched == [URL] and any(d.http_status == status for d in store.docs)
    assert all(r.disposed for r in responses)


@pytest.mark.browser
@pytest.mark.parametrize('body,mime,expected', [
    ('', 'text/html', 'failed'),
    ('<main>Client shell</main>', 'text/html', 'failed'),
    ((FIXTURES/'dynamic_domestic_listing.html').read_text(), 'text/html', 'failed'),
    ((FIXTURES/'domestic_html_listing_page_1.html').read_text(), 'application/json', 'failed'),
    ((FIXTURES/'domestic_html_listing_page_1.html').read_text().replace('<main>', '<main>Access denied'), 'text/html', 'failed'),
    ((FIXTURES/'domestic_html_listing_page_1.html').read_text().replace('<main>', '<main>CAPTCHA'), 'text/html', 'failed'),
    ((FIXTURES/'domestic_html_listing_page_1.html').read_text().replace('</head>', '<meta http-equiv="refresh" content="0;url=/booking"></head>'), 'text/html', 'failed'),
    ((FIXTURES/'domestic_html_listing_page_1.html').read_text().replace('?page=1', '/tour-nuoc-ngoai'), 'text/html', 'partial'),
    ((FIXTURES/'domestic_html_listing_page_1.html').read_text().replace('<div class="view-content"', '<button>Load</button><div class="view-content"'), 'text/html', 'partial'),
    ((FIXTURES/'domestic_html_listing_page_1.html').read_text().replace('/new-tour', '/booking'), 'text/html', 'partial'),
])
def test_html_unsupported_source_is_not_rendered_or_completed(monkeypatch, chromium_available, body, mime, expected):
    result, store, fetched, _ = html_discovery(monkeypatch, pages={URL:body}, headers={'content-type':mime})
    assert result.status == expected and result.terminal is False and fetched == [URL]
    assert store.run['manifest']['source_pager_terminal_observed'] is False
    assert result.errors and not any(d.kind == 'dom' for d in store.docs)
    if expected == 'failed':
        assert not result.cards
    if '/booking' in body and 'meta' not in body:
        assert any(c['identity_error'] for c in result.cards)


@pytest.mark.browser
@pytest.mark.parametrize('robots', ['User-agent: *\nDisallow: /', 'not robots'])
def test_html_robots_fail_closed_before_navigation(monkeypatch, chromium_available, robots):
    result, store, fetched, _ = html_discovery(monkeypatch, robots=robots)
    assert result.status == 'failed' and not result.cards and not fetched
    assert [d.kind for d in store.docs] == ['robots']


@pytest.mark.browser
@pytest.mark.parametrize('overrides', [{'max_listing_pages':1}, {'max_states':1},
                                      {'max_document_bytes':100}, {'max_run_bytes':100}])
def test_html_limits_retain_checkpoints_and_no_terminal(monkeypatch, chromium_available, overrides):
    result, store, _, responses = html_discovery(monkeypatch, **overrides)
    assert result.status != 'complete' and result.terminal is False
    assert store.docs and not store.run['manifest']['source_pager_terminal_observed']
    assert all(r.disposed for r in responses)


@pytest.mark.browser
@pytest.mark.parametrize('loop', ['url', 'inventory'])
def test_html_repeated_pager_or_inventory_is_partial(monkeypatch, chromium_available, loop):
    first = (FIXTURES/'domestic_html_listing_page_1.html').read_text()
    pages = {URL:first.replace('?page=1', '?page=0')} if loop == 'url' else {URL:first, URL+'?page=1':first}
    result, store, fetched, _ = html_discovery(monkeypatch, pages=pages)
    assert result.status == 'partial' and not result.terminal
    assert len(fetched) == (1 if loop == 'url' else 2)
    assert 'repeated' in store.run['manifest']['stop_reason']


@pytest.mark.browser
def test_html_later_http_failure_preserves_source_cards(monkeypatch, chromium_available):
    first = (FIXTURES/'domestic_html_listing_page_1.html').read_text()
    result, store, fetched, _ = html_discovery(monkeypatch, pages={
        URL:first, URL+'?page=1':(403, {'content-type':'text/html'}, 'Forbidden')})
    assert result.status == 'partial' and len(result.cards) == 2 and len(fetched) == 2
    assert not result.terminal and store.docs[-1].http_status == 403


@pytest.mark.parametrize('invalid', ['owned_missing', 'owned_false', 'owned_numeric', 'terminal_missing',
    'terminal_false', 'terminal_string', 'run', 'kind', 'role', 'method', 'status', 'url', 'ref', 'response'])
def test_html_source_authority_requires_exact_owned_evidence(invalid):
    from types import SimpleNamespace
    from hanoitourist_crawler.acquisition.collector import CaptureError
    from hanoitourist_crawler.config import Config
    from hanoitourist_crawler.acquisition.discovery import HTMLDiscoveryCollector
    from tests.acquisition.test_collector import MemoryStorage
    run_id = uid()
    capture = HTMLDiscoveryCollector(Config(database_url='unused'), MemoryStorage(), run_id)
    raw = RawDocument(run_id, 1, 'http_document', 'listing', 'listing:0', URL, b'<main>Source</main>', http_status=200)
    entry = {'owned_primary':True,'terminal':True,'raw_document_id':raw.id,'url':URL,'status':200}
    if invalid == 'owned_missing': del entry['owned_primary']
    elif invalid.startswith('owned_'): entry['owned_primary'] = False if invalid == 'owned_false' else 1
    elif invalid == 'terminal_missing': del entry['terminal']
    elif invalid.startswith('terminal_'): entry['terminal'] = False if invalid == 'terminal_false' else 'true'
    elif invalid == 'run': raw = replace(raw, run_id=uid())
    elif invalid == 'kind': raw = replace(raw, kind='dom')
    elif invalid == 'role': raw = replace(raw, role='tour')
    elif invalid == 'method': raw = replace(raw, method='POST')
    elif invalid == 'status': entry['status'] = 404
    elif invalid == 'url': entry['url'] = URL+'?page=1'
    elif invalid == 'ref': entry['raw_document_id'] = uid()
    capture.manifest['navigation'] = [entry]
    capture.navigation.last_document = raw
    response = SimpleNamespace(url=URL, status=404 if invalid == 'response' else 200)
    with pytest.raises(CaptureError, match='evidence'):
        capture.source_document(response)


@pytest.mark.parametrize('failure', ['context', 'page', 'capture', 'interrupt', 'interrupt_and_close'])
def test_html_lifecycle_closes_context_before_browser_and_preserves_failure(monkeypatch, failure):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from hanoitourist_crawler.config import Config
    from hanoitourist_crawler.acquisition.discovery import HTMLDiscoveryCollector
    from hanoitourist_crawler.acquisition.collector import CaptureError
    from tests.acquisition.test_collector import MemoryStorage
    calls = []
    def raise_error():
        if failure.startswith('interrupt'):
            raise KeyboardInterrupt
        raise CaptureError('original capture failure')
    class Context:
        def route(self, *args): pass
        def new_page(self):
            if failure == 'page': raise_error()
            return SimpleNamespace(on=lambda *args: None)
        def unroute_all(self, **kwargs): calls.append('unroute_live')
        def close(self):
            calls.append('context_close')
            if failure == 'interrupt_and_close': raise ValueError('private close text')
    class Browser:
        def new_context(self, **kwargs):
            if failure == 'context': raise_error()
            return Context()
        def close(self): calls.append('browser_close')
    @contextmanager
    def playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs: Browser()))
    monkeypatch.setattr('hanoitourist_crawler.acquisition.discovery.sync_playwright', playwright)
    store = MemoryStorage()
    capture = HTMLDiscoveryCollector(Config(database_url='unused'), store, uid())
    monkeypatch.setattr(capture, 'load_robots', lambda: None)
    monkeypatch.setattr(capture, 'check', lambda: None)
    monkeypatch.setattr(capture, 'navigate', lambda *args: raise_error())
    if failure.startswith('interrupt'):
        with pytest.raises(KeyboardInterrupt): capture.collect()
        assert store.run['capture_status'] == 'partial'
        assert store.run['manifest']['stop_reason'] == 'interrupted'
    else:
        assert capture.collect().status == 'failed'
        assert any('original capture failure' in e for e in store.run['errors'])
    assert calls == (['browser_close'] if failure == 'context' else ['context_close', 'browser_close'])
    assert 'private close text' not in str(store.run)


@pytest.mark.parametrize('broken', ['context', 'browser'])
def test_html_cleanup_failure_without_capture_error_is_not_hidden(broken):
    from types import SimpleNamespace
    from hanoitourist_crawler.config import Config
    from hanoitourist_crawler.acquisition.discovery import HTMLDiscoveryCollector
    from tests.acquisition.test_collector import MemoryStorage
    calls = []
    def close(name):
        calls.append(name)
        if broken == name:
            raise ValueError('close error')
    capture = HTMLDiscoveryCollector(Config(database_url='unused'), MemoryStorage(), uid())
    with pytest.raises(ValueError, match='close error'):
        capture._close_browser(SimpleNamespace(close=lambda: close('context')),
                               SimpleNamespace(close=lambda: close('browser')))
    assert calls == ['context', 'browser']


def test_html_interrupt_persists_partial_without_network(monkeypatch):
    from hanoitourist_crawler.config import Config
    from hanoitourist_crawler.acquisition.discovery import HTMLDiscoveryCollector
    from tests.acquisition.test_collector import MemoryStorage
    store = MemoryStorage()
    capture = HTMLDiscoveryCollector(Config(database_url='unused'), store, uid())
    monkeypatch.setattr(capture, 'load_robots', lambda:(_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt): capture.collect()
    assert store.run['capture_status'] == 'partial' and store.run['manifest']['stop_reason'] == 'interrupted'
