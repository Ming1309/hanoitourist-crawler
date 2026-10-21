from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from urllib.robotparser import RobotFileParser

import httpx
import pytest

from hanoitourist_crawler.acquisition.collector import CaptureError, Collector
from hanoitourist_crawler.config import Config, DEFAULT_TOUR_URL
from hanoitourist_crawler.models import uid
from hanoitourist_crawler.processing.parser import parse
from tests.conftest import FIXTURES


class MemoryStorage:
    def __init__(self):
        self.docs = []
        self.run = {'capture_status':'running'}

    def append_raw(self, document): self.docs.append(document)
    def update_run(self, run_id, **values): self.run.update(deepcopy(values))


def collector(**overrides):
    cfg = replace(Config(database_url='unused', request_interval=0, navigation_timeout=2, run_timeout=45), **overrides)
    store = MemoryStorage()
    return Collector(cfg, store, uid()), store


def mock_robots(monkeypatch, text='User-agent: *\nAllow: /', status=200):
    monkeypatch.setattr(httpx.Client, 'get', lambda self, url: httpx.Response(status, text=text, request=httpx.Request('GET',url)))


class Request:
    def __init__(self, url, method='GET', resource_type='xhr', post_data=None):
        self.url, self.method, self.resource_type, self.post_data = url, method, resource_type, post_data


class FetchResponse:
    def __init__(self, url, status=200, body=b'', headers=None):
        self.url, self.status, self.content = url, status, body
        self.headers = headers or {'content-type':'text/html'}
        self.disposed = False
    def body(self): return self.content
    def dispose(self): self.disposed = True


class Route:
    def __init__(self, request): self.request, self.result = request, None
    def abort(self, **kwargs): self.result = 'blocked'
    def fetch(self, **kwargs):
        self.fetch_options = kwargs
        return FetchResponse(self.request.url)
    def fulfill(self, **kwargs): self.result = 'allowed'


@pytest.mark.parametrize('network_request', [
    Request('https://hanoitourist.vn/tour/8140/lich/1/booking'),
    Request(DEFAULT_TOUR_URL, 'POST', post_data='form_id=phone&op=submit'),
    Request('https://hanoitourist.vn/comment/reply/node/8140', 'POST'),
    Request('https://hanoitourist.vn/core/modules/statistics/statistics.php', 'POST'),
    Request('https://www.googletagmanager.com/gtag/js', resource_type='script'),
    Request('https://hanoitourist.vn/views/ajax?view_name=tour_chu_de&view_args=137'),
])
def test_network_policy_blocks_mutations_and_unrelated(network_request):
    c, _ = collector();c.node_id='8140'
    route = Route(network_request);c.route(route)
    assert route.result == 'blocked'


@pytest.mark.browser
def test_retry_instrumentation_preserves_business_snapshot(monkeypatch, chromium_available):
    from hanoitourist_crawler.workflows.retry import DiagnosticCollector
    from hanoitourist_crawler.processing.fingerprint import fingerprint
    offline_routes(monkeypatch)
    base, store = collector()
    base.collect()
    expected = parse(store.docs, base.run_id)
    diagnostic_store = MemoryStorage()
    diagnostic_store.run['manifest'] = {'retry_scope': {'aliases': [DEFAULT_TOUR_URL]}}
    diagnostic_store.get_run = lambda rid: deepcopy(diagnostic_store.run)
    diagnostic = DiagnosticCollector(base.config, diagnostic_store, uid())
    diagnostic.collect()
    actual = parse(diagnostic_store.docs, diagnostic.run_id)
    assert diagnostic_store.run['capture_status'] == store.run['capture_status']
    assert fingerprint(actual) == fingerprint(expected)
    assert [d.kind for d in diagnostic_store.docs] == [d.kind for d in store.docs]
    assert diagnostic_store.run['manifest']['diagnostics']['schema'] == 'diagnostics-v1'


@pytest.mark.browser
@pytest.mark.parametrize('diagnostic', [False, True])
@pytest.mark.parametrize('trigger', ['meta', 'script'])
def test_detail_native_booking_navigation_stays_guarded_through_close(
        monkeypatch, chromium_available, http_servers, capsys, caplog, diagnostic, trigger):
    from hanoitourist_crawler.workflows.retry import DiagnosticCollector
    markup = (b'<meta http-equiv="refresh" content="0;url=/booking">' if trigger == 'meta' else
              b'<script>location.href="/booking"</script>')
    body = markup + b'<article class="node--type-tour" data-history-node-id="8140"></article>'
    server = http_servers({('GET', '/start'): (200, {'Content-Type': 'text/html'}, body)})
    cfg = Config(database_url='unused', tour_url=server.url + '/start', request_interval=0,
                 navigation_timeout=2, run_timeout=15)
    store = MemoryStorage()
    store.run['manifest'] = {'retry_scope': {'aliases': [cfg.tour_url]}}
    store.get_run = lambda rid: deepcopy(store.run)
    capture = (DiagnosticCollector if diagnostic else Collector)(cfg, store, uid())
    capture.robots = RobotFileParser(); capture.robots.parse(['User-agent: *', 'Allow: /'])
    monkeypatch.setattr(capture, 'load_robots', lambda: None)
    monkeypatch.setattr(capture, 'data_path', lambda url: url == cfg.tour_url)
    assert capture.collect() != 'complete'
    assert server.hits and all(h['path'] == '/start' for h in server.hits)
    assert len(server.hits) <= cfg.max_retries + 1
    assert all(d.body == body for d in store.docs if d.kind == 'http_document')
    output = capsys.readouterr()
    assert 'TargetClosedError' not in output.err + caplog.text
    assert 'Error occurred in event listener' not in output.err + caplog.text


def test_verified_dialog_post_allowed():
    c,_=collector();c.node_id='8140'
    route=Route(Request('https://hanoitourist.vn/tour/8140/lich-khoi-hanh?_wrapper_format=drupal_dialog',
                        'POST',post_data='_drupal_ajax=1&js=true'))
    c.route(route)
    assert route.result == 'allowed'


def test_robots_fail_closed_and_body_cap(monkeypatch):
    c,store=collector();mock_robots(monkeypatch,'User-agent: *\nDisallow: /')
    with pytest.raises(CaptureError,match='denied'):c.load_robots()
    assert store.docs[0].kind=='robots'
    c,store=collector(max_document_bytes=3)
    with pytest.raises(CaptureError,match='limit'):c.save('dom','tour',DEFAULT_TOUR_URL,b'1234')
    assert not store.docs


def test_interrupt_retains_status(monkeypatch):
    c,store=collector()
    monkeypatch.setattr(c,'load_robots',lambda:(_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):c.collect()
    assert store.run['capture_status']=='partial'


def offline_routes(monkeypatch, *, navigation_status=200, retry_after='0', failing_dialog=False, document_html=None):
    mock_robots(monkeypatch)
    original = Collector.route
    visited = []
    def policy(self, actual):
        owner = self
        request = actual.request
        class Proxy:
            def __init__(self):self.request=request
            def abort(self, **kwargs):actual.abort(**kwargs)
            def fulfill(self, **kwargs):actual.fulfill(**kwargs)
            def fetch(self, **kwargs):
                import json
                assert kwargs['max_redirects']==kwargs['max_retries']==0
                visited.append((request.method, request.url))
                if request.resource_type == 'document':
                    html=document_html if document_html is not None else (FIXTURES/'dynamic_tour.html').read_text()
                    return FetchResponse(request.url,navigation_status,html.encode(),
                        {'content-type':'text/html','retry-after':retry_after})
                if '/lich-khoi-hanh' in request.url:
                    if failing_dialog: return FetchResponse(request.url,503,b'unavailable')
                    # Response is dynamically generated from the mock page's two UI rows.
                    html = owner.page.locator('#lichkhoihanh .view-id-lich').evaluate('(e)=>e.outerHTML')
                    return FetchResponse(request.url,200,json.dumps([{'command':'openDialog','data':html}]).encode(),
                        {'content-type':'application/json'})
                return FetchResponse(request.url)
        original(self,Proxy())
    monkeypatch.setattr(Collector,'route',policy)
    return visited


@pytest.mark.browser
def test_unsupported_empty_schedule_keeps_checkpoint_without_timeout(monkeypatch, chromium_available):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup((FIXTURES/'dynamic_tour.html').read_text(), 'html.parser')
    soup.select_one('#lichkhoihanh').clear()
    for script in soup.select('script'): script.decompose()
    offline_routes(monkeypatch, document_html=str(soup))
    capture, store = collector()
    assert capture.collect() == 'partial'
    assert any('Unsupported schedule scope' in error for error in store.run['errors'])
    assert not any('TimeoutError' in error for error in store.run['errors'])
    assert any(doc.kind == 'dom' and doc.state_key == 'tour:initial' for doc in store.docs)
    snapshot = parse(store.docs, capture.run_id)
    from hanoitourist_crawler.processing.validation import validate
    assert validate(snapshot, store.docs, store.run).status == 'partial'
    assert snapshot.departures == []
    assert store.run['manifest']['actions']['tour:initial'] == 'captured'


@pytest.mark.browser
def test_dynamic_prices_modal_and_checkpoints(monkeypatch, chromium_available):
    visited=offline_routes(monkeypatch)
    c,store=collector()
    assert c.collect()=='complete',store.run.get('errors')
    snapshot=parse(store.docs,c.run_id)
    from hanoitourist_crawler.processing.validation import validate
    assert validate(snapshot, store.docs, store.run).status == 'complete'
    assert snapshot.coverage['discovered'] == store.run['manifest']['inventory']
    assert {d.source_schedule_id for d in snapshot.departures}=={'1','2'}
    assert len(snapshot.days)==1
    assert any(d.kind=='ajax' for d in store.docs)
    assert any(d.state_key.startswith('dialog:price') or d.state_key.startswith('dialog:0:price') for d in store.docs)
    assert all('booking' not in url for _,url in visited)
    assert all(value=='captured' for value in store.run['manifest']['actions'].values())


@pytest.mark.browser
@pytest.mark.parametrize('status', [403,429])
def test_access_failure_staged_without_bypass(monkeypatch, chromium_available, status):
    visited=offline_routes(monkeypatch,navigation_status=status,retry_after='999')
    c,store=collector()
    assert c.collect()=='failed'
    assert any(d.http_status==status for d in store.docs)
    assert len([url for _,url in visited if url==DEFAULT_TOUR_URL])==1


@pytest.mark.browser
def test_state_limit_is_partial_and_preserves_raw(monkeypatch,chromium_available):
    offline_routes(monkeypatch)
    c,store=collector(max_states=1)
    assert c.collect()=='partial'
    assert len(store.docs)>=2
    assert any('State limit' in e for e in store.run['errors'])


@pytest.mark.browser
def test_missing_ajax_does_not_report_complete(monkeypatch,chromium_available):
    offline_routes(monkeypatch,failing_dialog=True)
    c,store=collector()
    assert c.collect()=='partial'
    assert any(d.http_status==503 for d in store.docs)
    assert store.run['manifest']['actions']['dialog:open']=='discovered'


@pytest.mark.browser
def test_pager_loop_and_page_limit(monkeypatch,chromium):
    c,store=collector(max_pages=2)
    page=chromium.new_page();c.page=page
    page.set_content('<div id="scope"><a rel="next" href="#">Next</a></div>')
    monkeypatch.setattr(c,'checkpoint',lambda *a,**k:None)
    monkeypatch.setattr(c,'settle',lambda *a,**k:None)
    with pytest.raises(CaptureError,match='repeated'):c.traverse(page.locator('#scope'),'schedule')
    c.config=replace(c.config,max_pages=1)
    with pytest.raises(CaptureError,match='Page limit'):c.traverse(page.locator('#scope'),'schedule')
    page.close()


@pytest.mark.browser
def test_all_finite_selections_captured(chromium):
    c,store=collector()
    page=chromium.new_page();c.page=page
    page.set_content('''<div id="scope" class="view-id-lich"><select onchange="document.querySelector('#value').textContent=this.value"><option value="1000">First day</option><option value="2000">Second day</option></select><p id="value">1000</p></div>''')
    c.select_combinations(page.locator('#scope'),'schedule',0,())
    assert set(store.run['manifest']['actions']) == {'schedule:options:1000','schedule:options:2000'}
    assert len(store.docs)==2 and b'>2000</p>' in store.docs[1].body
    page.close()


def test_missing_robots_and_total_body_limit(monkeypatch):
    c,store=collector();mock_robots(monkeypatch,'error',status=503)
    with pytest.raises(CaptureError,match='fail closed'):c.load_robots()
    assert store.docs[0].http_status==503
    c,store=collector(max_run_bytes=4)
    c.save('robots','robots',DEFAULT_TOUR_URL,b'123')
    with pytest.raises(CaptureError,match='limit'):c.save('robots','robots',DEFAULT_TOUR_URL,b'45')
    assert len(store.docs)==1 and store.docs[0].body==b'123'


def test_checkpoint_inventory_keeps_schedule_without_id():
    c, store = collector()
    html = b'<article class="node--type-tour" data-history-node-id="8140"><div id="lichkhoihanh"><table class="footable"><tbody><tr><td class="ngay-khoi-hanh">08/10/2026 12:00</td><td><a href="/unknown">Chon</a></td></tr></tbody></table></div></article>'
    doc = c.save('dom', 'tour_state', DEFAULT_TOUR_URL, html)
    assert store.docs == [doc]
    row = store.run['manifest']['inventory']['schedule_rows'][0]
    assert row['source_schedule_id'] is None and row['identity_error']
    assert row['provenance'][0]['raw_document_id'] == doc.id


@pytest.mark.browser
def test_changed_date_class_in_dynamic_dom_is_retained(monkeypatch, chromium_available):
    from hanoitourist_crawler.processing.validation import validate
    html = (FIXTURES / 'dynamic_tour.html').read_text().replace(
        'class="ngay-khoi-hanh"', 'class="${id === \'1\' ? \'date-column-new\' : \'ngay-khoi-hanh\'}"')
    visited = offline_routes(monkeypatch, document_html=html)
    c, store = collector()
    assert c.collect() == 'complete'
    snapshot = validate(parse(store.docs, c.run_id), store.docs, store.run)
    assert snapshot.status == 'partial'
    assert snapshot.coverage['discovered'] == store.run['manifest']['inventory']
    assert {d.source_schedule_id for d in snapshot.departures} == {'1', '2'}
    changed = next(d for d in snapshot.departures if d.source_schedule_id == '1')
    assert changed.departure_at is None and len(changed.price_cells) == 3
    records = [r for r in snapshot.coverage['discovered']['schedule_rows'] if r['source_schedule_id'] == '1']
    assert records and all(r['provenance'] and r['departure_raw'] == '' for r in records)
    assert any('departure date' in w for w in snapshot.warnings)
    assert all('booking' not in url for _, url in visited)


@pytest.mark.parametrize('url,state,frame,record', [
    (DEFAULT_TOUR_URL, 'initial', 'main', False),
    (DEFAULT_TOUR_URL, 'initial', 'iframe', False),
    ('https://hanoitourist.vn/tour/8140/lich-khoi-hanh', 'dialog:open', 'main', False),
])
def test_unstaged_callbacks_are_not_navigation_evidence(url,state,frame,record):
    c,store=collector(); c.node_id='8140'; c.state=state
    c.page=SimpleNamespace(main_frame='main',set_default_timeout=lambda *a:None)
    request=Request(url, resource_type='document')
    request.frame=frame
    response=SimpleNamespace(request=request,url=url,status=404,headers={},body=lambda:b'error')
    c.response(response)
    assert bool(c.manifest['navigation']) is record
    assert store.docs[0].http_status==404
    if 'lich-khoi-hanh' in url: assert store.docs[0].role=='schedule'


def test_shared_pacer_does_not_reset_between_requests(monkeypatch):
    from hanoitourist_crawler.acquisition.collector import RequestPacer
    clock=[0.0]; waits=[]
    monkeypatch.setattr('hanoitourist_crawler.acquisition.collector.time.monotonic',lambda:clock[0])
    def sleep(seconds): waits.append(seconds); clock[0]+=seconds
    monkeypatch.setattr('hanoitourist_crawler.acquisition.collector.time.sleep',sleep)
    pacer=RequestPacer(1)
    pacer.wait(10); pacer.wait(10)
    assert waits==[1.0]
    with pytest.raises(CaptureError): pacer.wait(1.5)


@pytest.mark.parametrize('status', [200, 302, 404, 410])
def test_guard_owned_fetch_marks_terminal_evidence_once(status):
    c, store = collector()
    c.page = SimpleNamespace(main_frame='main', set_default_timeout=lambda *a: None)
    c.robots = RobotFileParser(); c.robots.parse(['User-agent: *', 'Allow: /'])
    request = Request(DEFAULT_TOUR_URL, resource_type='document'); request.frame = 'main'
    c.navigation.expected = DEFAULT_TOUR_URL
    route = Route(request)
    body = b'<article data-history-node-id="8140"></article>'
    response = FetchResponse(DEFAULT_TOUR_URL, status, body, {'location': '/final-tour'})
    route.fetch = lambda **kwargs: response
    c.route(route)
    assert c.manifest['navigation'] == [{'url': DEFAULT_TOUR_URL, 'status': status,
        'raw_document_id': store.docs[0].id, 'owned_primary': True, 'terminal': status != 302}]
    c.response(SimpleNamespace(request=request, url=DEFAULT_TOUR_URL))
    assert len(store.docs) == 1 and not c.pending and response.disposed
    assert c.node_id == ('8140' if status == 200 else '')


def test_unowned_success_cannot_bind_node_id():
    c, store = collector()
    c.page = SimpleNamespace(main_frame='main', set_default_timeout=lambda *a: None)
    request = Request(DEFAULT_TOUR_URL, resource_type='document'); request.frame = 'main'
    c.pending.add(request)
    c.response(SimpleNamespace(request=request, url=DEFAULT_TOUR_URL, status=200, headers={},
                               body=lambda: b'<article data-history-node-id="9900"></article>'))
    assert c.node_id == '' and not c.manifest['navigation'] and not c.pending
    assert store.docs[0].http_status == 200 and c.errors

