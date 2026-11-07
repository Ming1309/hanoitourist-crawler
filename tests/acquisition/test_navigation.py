from dataclasses import replace
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import pytest

from hanoitourist_crawler.acquisition.collector import Collector
from hanoitourist_crawler.config import Config
from hanoitourist_crawler.models import uid
from tests.acquisition.test_collector import MemoryStorage


def local_capture(chromium, server, *, paths=('/start','/allowed','/final'), cls=Collector):
    url=server.url+'/start'
    cfg=Config(database_url='unused',tour_url=url,request_interval=0,navigation_timeout=2,run_timeout=15)
    capture=cls(cfg,MemoryStorage(),uid())
    capture.robots=RobotFileParser(); capture.robots.parse(['User-agent: *','Allow: /'])
    def valid(target):
        parsed=urlsplit(target)
        return parsed.netloc==urlsplit(server.url).netloc and parsed.path in paths
    capture.data_path=valid
    def redirect(location, source):
        target=urljoin(source,location)
        if not valid(target) or not capture.robots.can_fetch(capture.config.user_agent,target):
            raise ValueError('Test redirect outside authority or denied by robots')
        return target
    capture.redirect_target=redirect
    context=chromium.new_context(service_workers='block')
    context.route('**/*',capture.route)
    capture.page=context.new_page()
    capture.page.on('response',capture.response)
    capture.page.on('requestfailed',capture.failed_request)
    return capture,context


def navigate(capture,url):
    # Before the fix, exercise the existing native navigation, not an absent API.
    opener=getattr(capture,'navigate',None)
    return opener(url) if opener else capture.page.goto(url,wait_until='domcontentloaded')


@pytest.mark.browser
@pytest.mark.parametrize('status',[301,302,303,307,308])
@pytest.mark.parametrize('target',['/booking','/admin/secret','/comment/reply/node/1'])
def test_native_forbidden_redirect_never_hits_target(chromium,http_servers,status,target):
    server=http_servers({('GET','/start'):(status,{'Location':target},b'redirect source')})
    capture,context=local_capture(chromium,server)
    try:
        try: navigate(capture,server.url+'/start')
        except Exception: pass
        assert [hit['path'] for hit in server.hits]==['/start']
    finally: context.close()


@pytest.mark.browser
def test_native_cross_authority_redirect_never_hits_second_server(chromium,http_servers):
    other=http_servers()
    server=http_servers({('GET','/start'):(302,{'Location':other.url+'/target'},b'redirect')})
    capture,context=local_capture(chromium,server)
    try:
        try: navigate(capture,server.url+'/start')
        except Exception: pass
        assert other.hits==[]
    finally: context.close()


@pytest.mark.browser
def test_allowed_then_forbidden_chain_stops_before_target(chromium,http_servers):
    server=http_servers({
        ('GET','/start'):(302,{'Location':'/allowed'},b'first'),
        ('GET','/allowed'):(307,{'Location':'/booking'},b'second'),
    })
    capture,context=local_capture(chromium,server)
    try:
        try: navigate(capture,server.url+'/start')
        except Exception: pass
        assert [hit['path'] for hit in server.hits]==['/start','/allowed']
    finally: context.close()


@pytest.mark.browser
def test_allowed_chain_stages_once_and_preserves_final_url(chromium,http_servers):
    terminal=b'<title>terminal</title><article class="node--type-tour" data-history-node-id="8140"></article>'
    server=http_servers({
        ('GET','/start'):(302,{'Location':'allowed'},b'first'),
        ('GET','/allowed'):(301,{'Location':'/final'},b'second'),
        ('GET','/final'):(200,{'Content-Type':'text/html'},terminal),
    })
    capture,context=local_capture(chromium,server)
    try:
        response=navigate(capture,server.url+'/start')
        assert response.status==200 and capture.page.url==server.url+'/final'
        assert capture.page.title()=='terminal' and capture.node_id=='8140'
        assert [d.http_status for d in capture.storage.docs]==[302,301,200]
        assert [d.body for d in capture.storage.docs]==[b'first',b'second',terminal]
        assert len(capture.manifest['navigation'])==3 and not capture.errors and not capture.fatal
        assert all(n['owned_primary'] is True for n in capture.manifest['navigation'])
        assert [n['terminal'] for n in capture.manifest['navigation']] == [False, False, True]
        assert all(d.id==n['raw_document_id'] for d,n in zip(capture.storage.docs,capture.manifest['navigation']))
        assert [h['path'] for h in server.hits]==['/start','/allowed','/final']
    finally: context.close()


@pytest.mark.browser
def test_gzip_bytes_decoded_once_and_cookies_preserved(chromium,http_servers):
    import gzip
    body=b'<title>decoded</title>'
    server=http_servers({('GET','/start'):(200,{
        'Content-Type':'text/html','Content-Encoding':'gzip','Set-Cookie':'test=yes; Path=/'},gzip.compress(body))})
    capture,context=local_capture(chromium,server)
    try:
        assert navigate(capture,server.url+'/start').status==200
        assert capture.page.title()=='decoded' and capture.storage.docs[0].body==body
        assert any(cookie['name']=='test' and cookie['value']=='yes' for cookie in context.cookies())
        assert len(server.hits)==len(capture.storage.docs)==1
    finally: context.close()


@pytest.mark.browser
@pytest.mark.parametrize('status',[200,404,410,403,429,503])
def test_native_terminal_status_is_not_faked(chromium,http_servers,status):
    server=http_servers({('GET','/start'):(status,{},b'terminal')})
    capture,context=local_capture(chromium,server)
    try:
        if status==403:
            with pytest.raises(Exception): navigate(capture,server.url+'/start')
        else:
            assert navigate(capture,server.url+'/start').status==status
        assert capture.storage.docs[0].http_status==status
        assert capture.manifest['navigation'][-1]['status']==status
        assert capture.manifest['navigation'][-1]['owned_primary'] is True
        assert capture.manifest['navigation'][-1]['terminal'] is True
        assert len(capture.storage.docs)==len(server.hits)==1
    finally: context.close()


@pytest.mark.browser
@pytest.mark.parametrize('kind',['loop','limit','missing'])
def test_redirect_loop_cap_and_missing_location_fail_closed(chromium,http_servers,kind):
    paths=['/start']+[f'/hop{i}' for i in range(1,7)]
    if kind=='limit':
        routes={('GET',path):(302,{'Location':paths[i+1]},b'hop') for i,path in enumerate(paths[:-1])}
    else:
        routes={('GET','/start'):(302,{'Location':'/start'} if kind=='loop' else {},b'hop')}
    server=http_servers(routes)
    capture,context=local_capture(chromium,server,paths=paths)
    try:
        with pytest.raises(Exception): navigate(capture,server.url+'/start')
        assert capture.fatal and capture.navigation.expected is None
        assert len(server.hits)==(6 if kind=='limit' else 1)
        assert not any(h['path']=='/hop6' for h in server.hits)
    finally: context.close()


@pytest.mark.browser
@pytest.mark.parametrize('resource',['script','iframe','fetch'])
def test_non_primary_redirect_never_hits_destination(chromium,http_servers,resource):
    markup={'script':'<script src="/allowed"></script>',
            'iframe':'<iframe src="/allowed"></iframe>',
            'fetch':'<script>fetch("/allowed").catch(()=>{});</script>'}[resource]
    server=http_servers({('GET','/start'):(200,{'Content-Type':'text/html'},markup.encode()),
                         ('GET','/allowed'):(302,{'Location':'/final'},b'nonprimary')})
    capture,context=local_capture(chromium,server)
    try:
        try: navigate(capture,server.url+'/start')
        except Exception: pass
        capture.page.wait_for_timeout(200)
        assert [h['path'] for h in server.hits]==['/start','/allowed']
        assert capture.fatal and capture.manifest['navigation'][-1]['status']==200
        assert capture.manifest['redirects'][-1]['reason']=='blocked'
    finally: context.close()


@pytest.mark.browser
def test_post_redirect_does_not_convert_or_forward_body(chromium,http_servers):
    other=http_servers()
    html=b'<script>fetch("/allowed",{method:"POST",body:"_drupal_ajax=1&token=private"}).catch(()=>{});</script>'
    server=http_servers({('GET','/start'):(200,{'Content-Type':'text/html'},html),
                         ('POST','/allowed'):(303,{'Location':other.url+'/target'},b'redirect')})
    capture,context=local_capture(chromium,server)
    def policy(route):
        if route.request.method=='POST' and route.request.url==server.url+'/allowed':
            capture.navigation.fetch(route)  # Local test-only readonly POST authority.
        else: capture.route(route)
    context.route('**/*',policy)
    try:
        try: navigate(capture,server.url+'/start')
        except Exception: pass
        capture.page.wait_for_timeout(200)
        assert other.hits==[] and len(server.hits)==2
        assert server.hits[-1]['method']=='POST' and server.hits[-1]['body'].endswith(b'token=private')
        assert capture.storage.docs[-1].method=='POST' and capture.storage.docs[-1].http_status==303
        assert capture.manifest['navigation'][-1]['status']==200 and capture.fatal
    finally: context.close()


@pytest.mark.browser
def test_discovery_redirect_cannot_escape_its_listing_authority(chromium,http_servers):
    from hanoitourist_crawler.acquisition.discovery import HTMLDiscoveryCollector
    server=http_servers({('GET','/start'):(302,{'Location':'/tour-nuoc-ngoai'},b'redirect')})
    capture,context=local_capture(chromium,server,cls=HTMLDiscoveryCollector)
    try:
        with pytest.raises(Exception): navigate(capture,server.url+'/start')
        assert [h['path'] for h in server.hits]==['/start']
        assert capture.storage.docs[0].role=='listing' and capture.fatal
    finally: context.close()


@pytest.mark.browser
def test_robots_denied_redirect_is_never_fetched(chromium,http_servers):
    server=http_servers({('GET','/start'):(302,{'Location':'/allowed'},b'redirect')})
    capture,context=local_capture(chromium,server)
    capture.robots=RobotFileParser()
    capture.robots.parse(['User-agent: *','Disallow: /allowed'])
    try:
        with pytest.raises(Exception): navigate(capture,server.url+'/start')
        assert [h['path'] for h in server.hits]==['/start'] and capture.fatal
    finally: context.close()


@pytest.mark.browser
def test_single_hop_timeout_does_not_follow_or_hide_failure(chromium,http_servers):
    import time
    def slow(request):
        time.sleep(0.2)
        return 302,{'Location':'/allowed'},b'late'
    server=http_servers({('GET','/start'):slow})
    capture,context=local_capture(chromium,server)
    capture.config=replace(capture.config,navigation_timeout=0.05)
    try:
        with pytest.raises(Exception): navigate(capture,server.url+'/start')
        assert not any(h['path']=='/allowed' for h in server.hits)
        assert capture.navigation.expected is None and capture.navigation.next_target is None
    finally: context.close()


@pytest.mark.browser
def test_redirect_raw_limit_never_truncates_and_follows(chromium,http_servers):
    server=http_servers({('GET','/start'):(302,{'Location':'/allowed'},b'x'*1000)})
    capture,context=local_capture(chromium,server)
    capture.config=replace(capture.config,max_document_bytes=100)
    try:
        with pytest.raises(Exception): navigate(capture,server.url+'/start')
        assert capture.storage.docs==[] and [h['path'] for h in server.hits]==['/start']
        assert capture.fatal
    finally: context.close()


@pytest.mark.browser
@pytest.mark.postgres
@pytest.mark.parametrize('terminal',[302,404,410])
@pytest.mark.parametrize('recover', [False, True])
def test_guarded_redirect_publication_preserves_or_marks_only_primary(storage,capture_history,chromium,http_servers,tmp_path,terminal,recover):
    from hanoitourist_crawler.config import DEFAULT_TOUR_URL
    from hanoitourist_crawler.models import now
    _,old=capture_history()
    batch=storage.create_batch(); state=storage.register_tour(DEFAULT_TOUR_URL,'Tour',batch)
    prior=state['last_successful_check_at']
    run=storage.create_run(DEFAULT_TOUR_URL); storage.begin_attempt(batch,state['id'],run,'missing_listing')
    server=http_servers({('GET','/start'):(302,{'Location':'/allowed'},b'first'),
                         ('GET','/allowed'):(terminal,{'Location':'/booking'} if terminal==302 else {},b'terminal')})
    capture,context=local_capture(chromium,server)
    capture.storage, capture.run_id=storage,run
    try:
        try: navigate(capture,server.url+'/start')
        except Exception: pass
        storage.update_run(run,capture_status='failed',finished_at=now(),manifest=capture.manifest,
                           errors=[capture.fatal or 'Non-success navigation'])
        if recover:
            from hanoitourist_crawler.workflows.incremental import recover_batches
            recover_batches(storage, tmp_path)
            before = storage.batch_tours(batch)
            recover_batches(storage, tmp_path)
            assert storage.batch_tours(batch) == before
        else:
            storage.apply_attempt(batch,state['id'],run)
        current=storage.get_tour(state['id'])
        assert current['current_snapshot_id']==old.id and current['last_successful_check_at']==prior
        assert current['availability']==('available' if terminal==302 else 'unavailable')
        assert [h['path'] for h in server.hits]==['/start','/allowed']
        changes=storage.batch_tours(batch)[0]['change_kinds']
        assert changes==([] if terminal==302 else ['unavailable'])
    finally: context.close()


def test_unrelated_timeout_cannot_be_accepted_from_url_text():
    from types import SimpleNamespace
    from playwright.sync_api import Error
    from hanoitourist_crawler.acquisition.navigation import GuardedNavigation
    calls=[]
    def goto(url, **kwargs):
        calls.append(url)
        guard.next_target='https://hanoitourist.vn/next-tour'
        raise Error('Page.goto: Timeout exceeded at '+url)
    page=SimpleNamespace(goto=goto)
    guard=GuardedNavigation(page=lambda:page,check=lambda:None,timeout=lambda:1000,
        stage=lambda *a:None,redirect_target=lambda *a:None,approve=lambda *a:None,
        record_redirect=lambda *a:None,block=lambda *a:None,finish=lambda *a:None,fail=lambda *a:None)
    url='https://hanoitourist.vn/tour?utm_source=net::ERR_ABORTED'
    with pytest.raises(Error,match='Timeout'): guard.navigate(url)
    assert calls==[url] and guard.next_target is None and guard.expected is None


def native_html_capture(monkeypatch, server, *, robots='User-agent: *\nDisallow: /core/', **overrides):
    from urllib.parse import parse_qsl
    from hanoitourist_crawler.acquisition.discovery import HTMLDiscoveryCollector
    from tests.acquisition.test_collector import mock_robots
    root = server.url + '/tour-trong-nuoc'
    def listing(value, base=root):
        target = urljoin(base, value)
        parsed = urlsplit(target)
        query = parse_qsl(parsed.query)
        if (parsed.scheme != 'http' or parsed.netloc != urlsplit(root).netloc
                or parsed.path != '/tour-trong-nuoc' or parsed.fragment or len(query) > 1
                or (query and (query[0][0] != 'page' or not query[0][1].isdigit()))):
            raise ValueError('Local test listing authority rejected')
        return root + ('?page='+str(int(query[0][1])) if query and int(query[0][1]) else '')
    def redirect(self, location, source):
        target = urljoin(source, location)
        listing(target)
        if not location or not self.robots.can_fetch(self.config.user_agent, target):
            raise ValueError('Local test robots or Location rejected')
        return target
    def valid(self, value):
        try: listing(value); return True
        except ValueError: return False
    mock_robots(monkeypatch, robots)
    monkeypatch.setattr('hanoitourist_crawler.acquisition.discovery.listing_url', listing)
    monkeypatch.setattr(HTMLDiscoveryCollector, 'data_path', valid)
    monkeypatch.setattr(HTMLDiscoveryCollector, 'redirect_target', redirect)
    cfg = replace(Config(database_url='unused', domestic_listing_url=root, request_interval=0,
                         navigation_timeout=2, discovery_timeout=15), **overrides)
    store = MemoryStorage()
    return HTMLDiscoveryCollector(cfg, store, uid()), store


def native_listing_body(name):
    from tests.conftest import FIXTURES
    return (FIXTURES / name).read_bytes().replace(b'href="/kham-', b'href="https://hanoitourist.vn/kham-').replace(
        b'href="/new-tour"', b'href="https://hanoitourist.vn/new-tour"')


@pytest.mark.browser
def test_html_native_source_only_zero_subresources_and_clean_close(monkeypatch, chromium_available, http_servers, capsys, caplog):
    server = http_servers({
        ('GET','/tour-trong-nuoc'):(200,{'Content-Type':'text/html'},native_listing_body('domestic_html_listing_page_1.html')),
        ('GET','/tour-trong-nuoc?page=1'):(200,{'Content-Type':'text/html'},native_listing_body('domestic_html_listing_page_2.html'))})
    for _ in range(3):
        capture, store = native_html_capture(monkeypatch, server)
        result = capture.collect()
        assert result.status == 'partial' and not result.terminal and len(result.cards) == 3
        assert capture.manifest['source_pager_terminal_observed'] is True
        assert [d.kind for d in store.docs] == ['robots','http_document','http_document']
        assert all(n['owned_primary'] is True and n['terminal'] is True for n in capture.manifest['navigation'])
        assert all(n['raw_document_id'] == d.id for n,d in zip(capture.manifest['navigation'], store.docs[1:]))
        assert not any('failed' in error.lower() for error in result.errors)
    assert [hit['path'] for hit in server.hits] == ['/tour-trong-nuoc','/tour-trong-nuoc?page=1'] * 3
    output = capsys.readouterr()
    assert 'TargetClosedError' not in output.err + caplog.text
    assert 'Error occurred in event listener' not in output.err + caplog.text


@pytest.mark.browser
@pytest.mark.parametrize('status',[301,302,303,307,308])
@pytest.mark.parametrize('target',['/booking','/admin/secret','/comment/reply/node/1','/tour-nuoc-ngoai'])
def test_html_native_forbidden_redirect_zero_hits(monkeypatch, chromium_available, http_servers, status, target):
    server = http_servers({('GET','/tour-trong-nuoc'):(status,{'Location':target},b'redirect')})
    capture, store = native_html_capture(monkeypatch, server)
    result = capture.collect()
    assert result.status == 'failed' and not result.cards and not result.terminal
    assert [hit['path'] for hit in server.hits] == ['/tour-trong-nuoc']
    assert store.docs[-1].http_status == status and store.docs[-1].body == b'redirect'


@pytest.mark.browser
def test_html_native_cross_host_and_robots_redirects_never_hit(monkeypatch, chromium_available, http_servers):
    other = http_servers()
    server = http_servers({('GET','/tour-trong-nuoc'):(302,{'Location':other.url+'/tour-trong-nuoc'},b'cross')})
    capture, _ = native_html_capture(monkeypatch, server)
    assert capture.collect().status == 'failed' and other.hits == []
    server.routes[('GET','/tour-trong-nuoc')] = (302,{'Location':'?page=1'},b'robots')
    capture, _ = native_html_capture(monkeypatch, server, robots='User-agent: *\nDisallow: /tour-trong-nuoc?page=1')
    assert capture.collect().status == 'failed'
    assert [h['path'] for h in server.hits] == ['/tour-trong-nuoc'] * 2


@pytest.mark.browser
def test_html_native_allowed_redirect_gzip_cookies_staged_once(monkeypatch, chromium_available, http_servers):
    import gzip
    body = native_listing_body('domestic_html_listing_page_2.html')
    def terminal(request):
        assert 'listing=yes' in request.headers.get('Cookie','')
        return 200,{'Content-Type':'text/html','Content-Encoding':'gzip'},gzip.compress(body)
    server = http_servers({('GET','/tour-trong-nuoc'):(302,{'Location':'?page=1','Set-Cookie':'listing=yes; Path=/'},b'hop'),
                           ('GET','/tour-trong-nuoc?page=1'):terminal})
    capture, store = native_html_capture(monkeypatch, server)
    result = capture.collect()
    assert result.status == 'partial' and len(result.cards) == 1
    assert [d.http_status for d in store.docs[1:]] == [302,200]
    assert store.docs[-1].body == body and store.run['final_url'] == server.url+'/tour-trong-nuoc?page=1'
    assert [n['terminal'] for n in capture.manifest['navigation']] == [False,True]
    assert [h['path'] for h in server.hits] == ['/tour-trong-nuoc','/tour-trong-nuoc?page=1']


@pytest.mark.browser
@pytest.mark.parametrize('kind', ['loop','hops','missing','meta'])
def test_html_native_navigation_bounds_fail_closed(monkeypatch, chromium_available, http_servers, kind):
    if kind == 'hops':
        paths = ['/tour-trong-nuoc'] + [f'/tour-trong-nuoc?page={n}' for n in range(1,7)]
        routes = {('GET',path):(302,{'Location':paths[n+1]},b'hop') for n,path in enumerate(paths[:-1])}
    elif kind == 'meta':
        routes = {('GET','/tour-trong-nuoc'):(200,{'Content-Type':'text/html'},
            b'<meta http-equiv="refresh" content="0;url=/booking">'+native_listing_body('domestic_html_listing_page_2.html'))}
    else:
        routes = {('GET','/tour-trong-nuoc'):(302,{'Location':'/tour-trong-nuoc'} if kind == 'loop' else {},b'hop')}
    server = http_servers(routes)
    capture, _ = native_html_capture(monkeypatch, server)
    result = capture.collect()
    assert result.status == 'failed' and not result.cards and not result.terminal
    assert len(server.hits) == (6 if kind == 'hops' else 1)
    assert not any(h['path'] in {'/booking','/tour-trong-nuoc?page=6'} for h in server.hits)


@pytest.mark.browser
@pytest.mark.parametrize('trigger,target', [
    ('meta', '/booking'), ('delayed_meta', '/booking'),
    ('meta', '/admin/secret'), ('header', '/comment/reply/node/1'), ('meta', 'other_host'),
])
def test_html_native_teardown_never_detaches_live_guard(monkeypatch, chromium_available, http_servers,
                                                       capsys, caplog, trigger, target):
    from playwright.sync_api import BrowserContext, Browser
    other = http_servers()
    target = other.url + '/escape' if target == 'other_host' else target
    body = native_listing_body('domestic_html_listing_page_2.html')
    headers = {'Content-Type': 'text/html'}
    if trigger == 'header':
        headers['Refresh'] = '0;url=' + target
    else:
        delay = '1' if trigger == 'delayed_meta' else '0'
        body = f'<meta http-equiv="refresh" content="{delay};url={target}">'.encode() + body
    server = http_servers({('GET','/tour-trong-nuoc'): (200, headers, body)})
    events, detached_live = [], []
    unroute, close, browser_close = BrowserContext.unroute_all, BrowserContext.close, Browser.close
    def detach(context, **kwargs):
        live = [p for p in context.pages if not p.is_closed()]
        detached_live.extend(live)
        events.append('unroute_live' if live else 'unroute_closed')
        unroute(context, **kwargs)
        # Test-only dispatch barrier exposes native navigation after live unroute.
        # It does not alter acquisition retries/timeouts or block forbidden targets.
        for page in live:
            page.wait_for_timeout(100)
    def shutdown(context, **kwargs):
        events.append('context_close')
        return close(context, **kwargs)
    def finish(browser, **kwargs):
        events.append('browser_close')
        return browser_close(browser, **kwargs)
    monkeypatch.setattr(BrowserContext, 'unroute_all', detach)
    monkeypatch.setattr(BrowserContext, 'close', shutdown)
    monkeypatch.setattr(Browser, 'close', finish)
    capture, store = native_html_capture(monkeypatch, server)
    result = capture.collect()
    assert not detached_live, events
    assert events.index('context_close') < events.index('browser_close')
    assert [h['path'] for h in server.hits] == ['/tour-trong-nuoc']
    assert other.hits == []
    assert not result.terminal and result.status != 'complete'
    raw = [d for d in store.docs if d.kind == 'http_document']
    assert len(raw) == 1 and raw[0].body == body
    assert store.run['final_url'] == server.url + '/tour-trong-nuoc'
    output = capsys.readouterr()
    assert 'TargetClosedError' not in output.err + caplog.text
    assert 'Error occurred in event listener' not in output.err + caplog.text


@pytest.mark.browser
@pytest.mark.parametrize('kind',['timeout','bytes'])
def test_html_native_timeout_and_body_limit_never_follow(monkeypatch, chromium_available, http_servers, kind):
    import time
    def response(request):
        if kind == 'timeout': time.sleep(0.2)
        return 302,{'Location':'?page=1'},b'x'*1000
    server = http_servers({('GET','/tour-trong-nuoc'):response})
    capture, store = native_html_capture(monkeypatch, server,
        **({'navigation_timeout':0.05} if kind == 'timeout' else {'max_document_bytes':100}))
    result = capture.collect()
    assert result.status == 'failed' and not result.cards and not result.terminal
    assert [h['path'] for h in server.hits] == ['/tour-trong-nuoc']
    assert [d.kind for d in store.docs] == ['robots']


@pytest.mark.browser
def test_html_native_transient_retry_remains_bounded_partial(monkeypatch, chromium_available, http_servers):
    attempts = []
    def response(request):
        attempts.append(request.path)
        if len(attempts) == 1:
            return 503,{'Retry-After':'0','Content-Type':'text/html'},b'temporary'
        return 200,{'Content-Type':'text/html'},native_listing_body('domestic_html_listing_page_2.html')
    server = http_servers({('GET','/tour-trong-nuoc'):response})
    capture, store = native_html_capture(monkeypatch, server)
    result = capture.collect()
    assert result.status == 'partial' and len(result.cards) == 1 and not result.terminal
    assert attempts == ['/tour-trong-nuoc']*2
    assert [d.http_status for d in store.docs[1:]] == [503,200]
