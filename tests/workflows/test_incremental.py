from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from hanoitourist_crawler.config import Config, DEFAULT_TOUR_URL
from hanoitourist_crawler.reporting.excel import export_batch_excel
from hanoitourist_crawler.workflows.incremental import crawl_domestic, recover_batches, selection_reason
from hanoitourist_crawler.models import DiscoveryResult, RawDocument, now, uid
from hanoitourist_crawler.processing.parser import parse
from hanoitourist_crawler.workflows.pipeline import parse_run

NEW_URL = 'https://hanoitourist.vn/new-tour'


@pytest.mark.postgres
@pytest.mark.parametrize('failure', [KeyboardInterrupt, RuntimeError])
@pytest.mark.parametrize('has_baseline', [False, True])
def test_stopped_daily_attempt_is_terminal_and_retryable_when_omitted(
        storage, capture_history, documents, tmp_path, failure, has_baseline):
    from hanoitourist_crawler.workflows.retry import select_targets
    baseline = capture_history(at=now() - timedelta(days=8))[1] if has_baseline else None
    url = DEFAULT_TOUR_URL if baseline else NEW_URL
    class StoppedCollector:
        def __init__(self, cfg, repository, run_id, **kwargs):
            self.storage, self.run_id = repository, run_id
        def collect(self):
            self.storage.append_raw(RawDocument(self.run_id, 1, 'http_document', 'tour', 'initial', url, b'checkpoint'))
            self.storage.update_run(self.run_id, capture_status='partial', finished_at=now())
            raise failure()
    if failure is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            crawl_domestic(config(tmp_path), storage, discovery=discovery([url], 'partial'), collector_class=StoppedCollector)
    else:
        crawl_domestic(config(tmp_path), storage, discovery=discovery([url], 'partial'), collector_class=StoppedCollector)
    bid = str(storage.connection.execute('SELECT id FROM domestic_batches ORDER BY started_at DESC LIMIT 1').fetchone()['id'])
    row = storage.batch_tours(bid)[0]
    tour = storage.get_tour(row['tour_id'])
    run_id = row['attempt_run_ids'][0]
    assert row['outcome'] == 'failed' and tour['requires_refresh']
    assert tour['current_snapshot_id'] == (baseline.id if baseline else None)
    assert storage.get_run(run_id)['parse_status'] == 'pending' and storage.get_snapshot(run_id) is None
    assert storage.get_batch(bid)['export_status'] == 'complete'
    assert select_targets(storage, bid, [url])[0]['skip_reason'] is None
    frozen = storage.batch_tours(bid)
    calls = []
    next_batch = crawl_domestic(config(tmp_path), storage, discovery=discovery([], 'partial'),
                               collector_class=collector(documents, calls=calls))
    assert calls == [url] and not storage.get_tour(tour['id'])['requires_refresh']
    assert storage.batch_tours(next_batch.batch_id)[0]['selection_reason'] == 'retry'
    assert storage.batch_tours(bid) == frozen


def discovery(urls, status='complete'):
    def run(config, storage, run_id, **kwargs):
        storage.update_run(run_id, capture_status=status, finished_at=now())
        return DiscoveryResult(status, [{'url':url, 'title':'Tour', 'source_tour_id':None,
            'provenance':[], 'identity_error':''} for url in urls], terminal=status=='complete')
    return run


def collector(documents, *, http_status=200, capture_status='complete', calls=None, price=None):
    class FakeCollector:
        def __init__(self, config, storage, run_id, **kwargs):
            self.config, self.storage, self.run_id = config, storage, run_id
        def collect(self):
            if calls is not None: calls.append(self.config.tour_url)
            sid = '9900' if self.config.tour_url == NEW_URL else '8140'
            if http_status != 200:
                doc = RawDocument(self.run_id,1,'http_document','tour','initial',self.config.tour_url,b'error',http_status=http_status)
                self.storage.append_raw(doc)
                self.storage.update_run(self.run_id,capture_status='failed',finished_at=now(),manifest={
                    'navigation':[{'url':doc.url,'status':http_status,'raw_document_id':doc.id,
                                   'owned_primary':True,'terminal':True}]})
                return
            for original in documents:
                body = original.body.replace(DEFAULT_TOUR_URL.encode(),self.config.tour_url.encode()).replace(b'8140',sid.encode())
                if price: body = body.replace(b'7,680,000',price.encode())
                self.storage.append_raw(replace(original,id=uid(),run_id=self.run_id,url=self.config.tour_url,body=body))
            snapshot = parse(self.storage.load_raw(self.run_id),self.run_id)
            doc = self.storage.load_raw(self.run_id)[0]
            self.storage.update_run(self.run_id,capture_status=capture_status,finished_at=now(),manifest={
                'actions':{'initial':'captured'},'inventory':snapshot.coverage['discovered'],
                'navigation':[{'url':doc.url,'status':200,'raw_document_id':doc.id,
                               'owned_primary':True,'terminal':True}]})
    return FakeCollector


def config(tmp_path):
    return Config(database_url='unused',export_dir=tmp_path,request_interval=0)


def summaries(path, header='source_tour_id'):
    book=load_workbook(path)
    try:
        ws=book['tour_snapshots']; col=[c.value for c in ws[1]].index(header)+1
        return [ws.cell(i,col).value for i in range(2,ws.max_row+1)]
    finally: book.close()


@pytest.mark.browser
@pytest.mark.postgres
def test_html_partial_batch_accepts_details_then_reuses_fresh(storage, documents, capture_history, tmp_path, monkeypatch, chromium_available):
    from tests.acquisition.test_discovery import html_routes
    fetched, _ = html_routes(monkeypatch)
    capture_history(at=now()-timedelta(days=1))
    absent = 'https://hanoitourist.vn/absent-tour'
    capture_history(url=absent, source_id='8888', at=now()-timedelta(days=8))
    seed = storage.create_batch()
    absent_state = storage.register_tour(absent, 'Absent', seed)
    storage.freeze_batch(seed, 'partial')
    previous_absent = storage.get_tour(absent_state['id'])
    calls = []
    cfg = replace(config(tmp_path), navigation_timeout=2, discovery_timeout=15)
    first = crawl_domestic(cfg, storage, collector_class=collector(documents, calls=calls))
    assert first.status == 'partial' and first.counts['new'] == 1 and calls == [NEW_URL]
    batch = storage.get_batch(first.batch_id)
    assert batch['discovery_complete'] is False and batch['export_status'] == 'complete'
    run = storage.get_run(batch['discovery_run_id'])
    assert run['manifest']['source_pager_terminal_observed'] is True
    assert storage.get_tour(absent_state['id']) == previous_absent
    new = next(t for t in storage.list_tours() if t['canonical_url'] == NEW_URL)
    snapshot = storage.get_snapshot_by_id(new['current_snapshot_id'])
    assert snapshot.status == 'complete' and len(snapshot.days) == 4
    assert len(snapshot.departures) == 9 and sum(len(d.price_cells) for d in snapshot.departures) == 117
    assert summaries(first.paths['delta']) == [new['source_tour_id']]
    old_bytes = {name: path.read_bytes() for name, path in first.paths.items()}
    checked = new['last_successful_check_at']
    second = crawl_domestic(cfg, storage, collector_class=collector(documents, calls=calls))
    assert second.status == 'partial' and calls == [NEW_URL]
    assert storage.get_tour(new['id'])['last_successful_check_at'] == checked
    assert summaries(second.paths['delta'], 'source_tour_id') == []
    assert fetched == [cfg.domestic_listing_url, cfg.domestic_listing_url+'?page=1'] * 2
    for name, path in export_batch_excel(storage, first.batch_id, tmp_path).items():
        assert path.read_bytes() == old_bytes[name]
    book = load_workbook(first.paths['full'])
    try:
        assert len(book.sheetnames) == 6 and 'Nhật ký tải' not in book.sheetnames
    finally:
        book.close()


@pytest.mark.browser
@pytest.mark.postgres
@pytest.mark.parametrize('http_status,capture_status,price,outcome', [
    (200, 'complete', None, 'unchanged'),
    (200, 'complete', '7,690,000', 'changed'),
    (200, 'partial', '7,690,000', 'partial'),
    (503, 'failed', None, 'failed'),
])
def test_html_ttl_cycle_skips_before_boundary_and_recaptures_at_boundary(
        storage, documents, tmp_path, monkeypatch, chromium_available,
        http_status, capture_status, price, outcome):
    from tests.conftest import FIXTURES
    from tests.acquisition.test_discovery import html_routes, URL
    fetched, _ = html_routes(monkeypatch, pages={
        URL: (FIXTURES/'domestic_html_listing_page_2.html').read_text()})
    cfg = replace(config(tmp_path), navigation_timeout=2, discovery_timeout=15)
    calls = []
    first = crawl_domestic(cfg, storage, collector_class=collector(documents, calls=calls))
    original = storage.list_tours()[0]
    checked = original['last_successful_check_at']
    assert calls == [DEFAULT_TOUR_URL] and first.counts['new'] == 1
    frozen_bytes = {name: path.read_bytes() for name, path in first.paths.items()}
    baseline = (original['current_snapshot_id'], original['fingerprint'], checked)

    # Advance only the selection clock; never rewrite persisted capture timestamps.
    before_boundary = checked + timedelta(days=cfg.recheck_days) - timedelta(microseconds=1)
    second = crawl_domestic(cfg, storage, clock=lambda: before_boundary,
                           collector_class=collector(documents, calls=calls))
    fresh = storage.get_tour(original['id'])
    assert calls == [DEFAULT_TOUR_URL] and second.counts['skipped'] == 1
    assert (fresh['current_snapshot_id'], fresh['fingerprint'], fresh['last_successful_check_at']) == baseline
    assert summaries(second.paths['delta'], 'source_tour_id') == []

    at_boundary = checked + timedelta(days=cfg.recheck_days)
    third = crawl_domestic(cfg, storage, clock=lambda: at_boundary,
        collector_class=collector(documents, calls=calls, http_status=http_status,
                                  capture_status=capture_status, price=price))
    current = storage.get_tour(original['id'])
    row = next(r for r in storage.batch_tours(third.batch_id) if r['frozen_state'])
    assert calls == [DEFAULT_TOUR_URL, DEFAULT_TOUR_URL]
    assert row['selection_reason'] == 'due' and row['outcome'] == outcome
    assert bool(summaries(third.paths['delta'], 'source_tour_id')) is (outcome == 'changed')
    if capture_status == 'complete':
        assert current['current_snapshot_id'] != baseline[0]
        # Watermark is the actual successful capture finish, not the injected selection clock.
        assert current['last_successful_check_at'] > checked
        assert current['last_successful_check_at'] == storage.get_run(row['attempt_run_ids'][-1])['finished_at']
        assert not current['requires_refresh']
        assert (current['fingerprint'] == baseline[1]) is (outcome == 'unchanged')
        fourth = crawl_domestic(cfg, storage, clock=lambda: current['last_successful_check_at'] + timedelta(days=6),
                               collector_class=collector(documents, calls=calls))
        assert fourth.counts['skipped'] == 1 and calls == [DEFAULT_TOUR_URL, DEFAULT_TOUR_URL]
        assert storage.get_tour(original['id'])['last_successful_check_at'] == current['last_successful_check_at']
        assert summaries(fourth.paths['delta'], 'source_tour_id') == []
    else:
        assert (current['current_snapshot_id'], current['fingerprint'], current['last_successful_check_at']) == baseline
        assert current['requires_refresh'] and current['availability'] == 'available'
    assert all(storage.get_batch(result.batch_id)['status'] == 'partial' for result in (first, second, third))
    assert fetched == [URL] * (4 if capture_status == 'complete' else 3)
    assert {name: path.read_bytes() for name, path in first.paths.items()} == frozen_bytes


@pytest.mark.browser
@pytest.mark.postgres
@pytest.mark.parametrize('http_status,capture_status,availability,change', [
    (200,'complete','available','updated'), (200,'partial','available',None),
    (404,'failed','unavailable','unavailable'), (410,'failed','unavailable','unavailable'),
    (403,'failed','available',None)])
def test_html_partial_still_uses_qualified_detail_outcomes(storage, capture_history, documents, tmp_path,
        monkeypatch, chromium_available, http_status, capture_status, availability, change):
    from tests.conftest import FIXTURES
    from tests.acquisition.test_discovery import html_routes, URL
    html_routes(monkeypatch, pages={URL:(FIXTURES/'domestic_html_listing_page_2.html').read_text()})
    at = now()-timedelta(days=8)
    _, old = capture_history(at=at)
    calls = []
    result = crawl_domestic(replace(config(tmp_path), navigation_timeout=2, discovery_timeout=15), storage,
        collector_class=collector(documents, calls=calls, http_status=http_status,
                                  capture_status=capture_status, price='7,690,000'))
    tour = storage.list_tours()[0]
    assert result.status == 'partial' and calls == [DEFAULT_TOUR_URL]
    assert not storage.get_batch(result.batch_id)['discovery_complete']
    assert tour['availability'] == availability
    if http_status != 200 or capture_status != 'complete':
        assert tour['current_snapshot_id'] == old.id and tour['last_successful_check_at'] == at
    else:
        assert tour['current_snapshot_id'] != old.id and tour['last_successful_check_at'] > at
    assert bool(summaries(result.paths['delta'], 'source_tour_id')) is bool(change)
    row = next(r for r in storage.batch_tours(result.batch_id) if r['frozen_state'])
    assert row['change_kinds'] == ([change] if change else [])


@pytest.mark.browser
@pytest.mark.postgres
def test_html_empty_retries_only_prior_refresh_and_no_absence(storage, capture_history, documents, tmp_path,
        monkeypatch, chromium_available):
    from tests.conftest import FIXTURES
    from tests.acquisition.test_discovery import html_routes, URL
    html_routes(monkeypatch, pages={URL:(FIXTURES/'domestic_html_listing_empty.html').read_text()})
    capture_history(at=now()-timedelta(days=8))
    seed = storage.create_batch()
    tour = storage.register_tour(DEFAULT_TOUR_URL, 'Known', seed)
    storage._update_tour(tour['id'], requires_refresh=True)
    storage.freeze_batch(seed, 'partial')
    calls = []
    result = crawl_domestic(replace(config(tmp_path), navigation_timeout=2, discovery_timeout=15), storage,
                            collector_class=collector(documents, calls=calls))
    assert result.status == 'partial' and calls == [DEFAULT_TOUR_URL]
    row = next(r for r in storage.batch_tours(result.batch_id) if r['frozen_state'])
    assert row['selection_reason'] == 'retry' and storage.get_tour(tour['id'])['is_listed'] is True


@pytest.mark.browser
@pytest.mark.postgres
def test_html_empty_without_catalogue_retains_existing_failed_batch_semantics(storage, tmp_path, monkeypatch, chromium_available):
    from tests.conftest import FIXTURES
    from tests.acquisition.test_discovery import html_routes, URL
    html_routes(monkeypatch, pages={URL:(FIXTURES/'domestic_html_listing_empty.html').read_text()})
    result = crawl_domestic(replace(config(tmp_path), navigation_timeout=2, discovery_timeout=15), storage,
                            collector_class=lambda *a,**k:pytest.fail('no detail to capture'))
    batch = storage.get_batch(result.batch_id)
    assert result.status == 'failed' and not batch['discovery_complete']
    assert storage.get_run(batch['discovery_run_id'])['capture_status'] == 'partial'
    assert batch['export_status'] == 'complete' and not storage.list_tours()


@pytest.mark.browser
@pytest.mark.postgres
def test_abandoned_html_discovery_recovers_partial_without_recrawl(storage, capture_history, tmp_path,
        monkeypatch, chromium_available):
    from hanoitourist_crawler.acquisition.discovery import discover_domestic
    from tests.acquisition.test_discovery import html_routes
    html_routes(monkeypatch)
    capture_history()
    batch = storage.create_batch()
    tour = storage.register_tour(DEFAULT_TOUR_URL, 'Known', batch)
    before = storage.get_tour(tour['id'])
    run_id = storage.create_run(config(tmp_path).domestic_listing_url)
    storage.update_batch(batch, discovery_run_id=run_id)
    discover_domestic(replace(config(tmp_path), navigation_timeout=2, discovery_timeout=15), storage, run_id)
    # Crash before the orchestrator receives the diagnostic result.
    recover_batches(storage, tmp_path)
    assert storage.get_batch(batch)['status'] == 'partial'
    assert not storage.get_batch(batch)['discovery_complete'] and storage.get_tour(tour['id']) == before
    rows = storage.batch_tours(batch)
    paths = export_batch_excel(storage, batch, tmp_path)
    old_bytes = {name:path.read_bytes() for name,path in paths.items()}
    recover_batches(storage, tmp_path)
    assert storage.batch_tours(batch) == rows and storage.get_tour(tour['id']) == before
    assert {name:path.read_bytes() for name,path in paths.items()} == old_bytes


@pytest.mark.parametrize('terminal', [False, True])
def test_unattempted_pending_backlog_is_new(terminal):
    tour = {'availability': 'pending', 'current_snapshot_id': None,
            'latest_attempt_run_id': None, 'requires_refresh': False,
            'last_successful_check_at': None}
    assert selection_reason(tour, now(), 7, discovered=False, terminal=terminal) == 'new'


@pytest.mark.parametrize('availability,snapshot,attempt,refresh,discovered,terminal,expected', [
    ('unavailable', None, None, False, False, False, None),
    ('unavailable', None, None, True, False, False, None),
    ('unavailable', None, None, False, False, True, None),
    ('unavailable', None, None, False, True, False, 'reactivated'),
    ('unavailable', None, None, True, True, False, 'retry'),
    ('pending', None, 'run', False, False, False, None),
    ('pending', None, 'run', True, False, False, 'retry'),
    ('pending', None, None, True, False, False, 'retry'),
    ('pending', 'snapshot', None, False, False, False, None),
    ('available', 'snapshot', 'run', False, True, False, None),
    ('available', 'snapshot', 'run', False, False, True, 'missing_listing'),
])
def test_backlog_admission_keeps_existing_guards(availability, snapshot, attempt, refresh,
                                               discovered, terminal, expected):
    tour = {'availability': availability, 'current_snapshot_id': snapshot,
            'latest_attempt_run_id': attempt, 'requires_refresh': refresh,
            'last_successful_check_at': now()}
    assert selection_reason(tour, now(), 7, discovered=discovered, terminal=terminal) == expected


@pytest.mark.postgres
def test_unattempted_backlog_survives_budget_cutoff_and_partial_omission(storage, documents, tmp_path, monkeypatch):
    import hanoitourist_crawler.workflows.incremental as incremental
    elapsed = [0.0]
    calls = []
    def exhausted_discovery(cfg, repository, run_id, **kwargs):
        result = discovery([NEW_URL], status='partial')(cfg, repository, run_id)
        elapsed[0] = 2.0
        return result
    cfg = replace(config(tmp_path), batch_timeout=1)
    with monkeypatch.context() as patch:
        patch.setattr(incremental, 'time', SimpleNamespace(monotonic=lambda: elapsed[0], time=lambda: elapsed[0]))
        first = crawl_domestic(cfg, storage, discovery=exhausted_discovery,
                               collector_class=collector(documents, calls=calls))
    tour = storage.list_tours()[0]
    frozen = storage.batch_tours(first.batch_id)
    assert calls == [] and tour['canonical_url'] == NEW_URL
    assert tour['current_snapshot_id'] is None and tour['latest_attempt_run_id'] is None
    assert not tour['requires_refresh'] and frozen[0]['discovered']
    assert frozen[0]['outcome'] == 'skipped' and frozen[0]['attempt_run_ids'] == []
    assert any('TimeoutError' in e for e in storage.get_batch(first.batch_id)['errors'])
    assert storage.connection.execute('SELECT count(*) AS n FROM crawl_runs WHERE requested_url=%s',
                                      (NEW_URL,)).fetchone()['n'] == 0
    second = crawl_domestic(config(tmp_path), storage, discovery=discovery([], status='partial'),
                            collector_class=collector(documents, calls=calls))
    row = storage.batch_tours(second.batch_id)[0]
    accepted = storage.get_tour(tour['id'])
    assert calls == [NEW_URL] and not row['discovered'] and row['selection_reason'] == 'new'
    assert len(row['attempt_run_ids']) == 1 and row['outcome'] == 'changed'
    assert accepted['is_listed'] and accepted['current_snapshot_id']
    run_id = row['attempt_run_ids'][0]
    assert storage.load_raw(run_id) and storage.get_snapshot(run_id).status == 'complete'
    assert accepted['last_successful_check_at'] == storage.get_run(run_id)['finished_at']
    baseline = (accepted['current_snapshot_id'], accepted['last_successful_check_at'])
    for urls in ([NEW_URL], []):
        cached = crawl_domestic(config(tmp_path), storage, discovery=discovery(urls, status='partial'),
                                collector_class=collector(documents, calls=calls))
        assert not storage.batch_tours(cached.batch_id)[0]['attempt_run_ids']
        state = storage.get_tour(tour['id'])
        assert (state['current_snapshot_id'], state['last_successful_check_at']) == baseline
    assert calls == [NEW_URL] and storage.batch_tours(first.batch_id) == frozen
    boundary = accepted['last_successful_check_at'] + timedelta(days=7)
    assert selection_reason(accepted, boundary, 7, discovered=True, terminal=False) == 'due'
    assert selection_reason(accepted, boundary, 7, discovered=False, terminal=False) is None


@pytest.mark.postgres
def test_superseded_pending_backlog_is_not_captured(storage, documents, tmp_path):
    seed = storage.create_batch()
    candidate = storage.register_tour(NEW_URL, 'Superseded', seed)
    target = storage.register_tour(DEFAULT_TOUR_URL, 'Target', seed)
    storage._update_tour(candidate['id'], superseded_by_id=target['id'])
    storage._update_tour(target['id'], availability='unavailable')
    storage.freeze_batch(seed, 'partial')
    calls = []
    result = crawl_domestic(config(tmp_path), storage, discovery=discovery([], status='partial'),
                            collector_class=collector(documents, calls=calls))
    assert not calls and all(not r['attempt_run_ids'] for r in storage.batch_tours(result.batch_id))
    assert storage.get_tour(candidate['id'])['latest_attempt_run_id'] is None


@pytest.mark.postgres
@pytest.mark.parametrize('has_raw', [False, True])
def test_recovery_batch_filter_preserves_other_abandoned_batch(storage, capture_history, tmp_path, has_raw):
    from hanoitourist_crawler.models import json_value
    _, baseline = capture_history()
    seed = storage.create_batch()
    tour = storage.register_tour(DEFAULT_TOUR_URL, 'Cached', seed)
    storage.freeze_batch(seed, 'complete')
    selected, other = storage.create_batch(), storage.create_batch()
    runs = []
    for batch in (selected, other):
        run = storage.create_run(DEFAULT_TOUR_URL)
        storage.begin_attempt(batch, tour['id'], run, 'retry')
        if has_raw:
            storage.append_raw(RawDocument(run, 1, 'http_document', 'tour', 'initial',
                                           DEFAULT_TOUR_URL, b'checkpoint', http_status=200))
        runs.append(run)
    before_other = (storage.get_batch(other), storage.batch_tours(other), storage.get_run(runs[1]))
    before_snapshot = json_value(storage.get_snapshot_by_id(baseline.id))
    recover_batches(storage, tmp_path, batch_id=selected)
    assert storage.get_batch(selected)['status'] == 'partial'
    assert storage.get_batch(selected)['export_status'] == 'complete'
    assert storage.get_run(runs[0])['capture_status'] == ('partial' if has_raw else 'failed')
    assert (storage.get_batch(other), storage.batch_tours(other), storage.get_run(runs[1])) == before_other
    assert storage.get_tour(tour['id'])['current_snapshot_id'] == baseline.id
    assert json_value(storage.get_snapshot_by_id(baseline.id)) == before_snapshot
    frozen = storage.batch_tours(selected)
    published = (tmp_path/'domestic'/selected/'full.xlsx').read_bytes()
    recover_batches(storage, tmp_path, batch_id=selected)
    assert storage.batch_tours(selected) == frozen
    assert (tmp_path/'domestic'/selected/'full.xlsx').read_bytes() == published
    recover_batches(storage, tmp_path)
    assert storage.get_batch(other)['status'] == 'partial'


def test_exact_seven_day_boundary():
    at=now()
    tour={'availability':'available','latest_attempt_run_id':'run','requires_refresh':False,
          'current_snapshot_id':'snapshot','last_successful_check_at':at-timedelta(days=7)}
    assert selection_reason(tour,at,7,discovered=True,terminal=True)=='due'
    assert selection_reason(tour,at-timedelta(microseconds=1),7,discovered=True,terminal=True) is None
    assert selection_reason(tour,at,7,discovered=False,terminal=True)=='missing_listing'
    assert selection_reason(tour,at,7,discovered=False,terminal=False) is None


@pytest.mark.postgres
def test_reuse_skips_details_and_empty_delta(storage,capture_history,documents,tmp_path):
    capture_history(at=now()-timedelta(days=2))
    calls=[]
    result=crawl_domestic(config(tmp_path),storage,discovery=discovery([DEFAULT_TOUR_URL]),collector_class=collector(documents,calls=calls))
    assert result.status=='complete' and calls==[]
    assert summaries(result.paths['delta'],'source_tour_id')==[]
    assert len(summaries(result.paths['full'],'source_tour_id'))==1


@pytest.mark.postgres
@pytest.mark.parametrize('status,availability',[(200,'available'),(404,'unavailable'),(410,'unavailable'),(403,'available'),(503,'available')])
def test_missing_listing_is_verified_not_assumed(storage,capture_history,documents,tmp_path,status,availability):
    old=now()-timedelta(days=1);capture_history(at=old)
    calls=[]
    result=crawl_domestic(config(tmp_path),storage,discovery=discovery([]),collector_class=collector(documents,http_status=status,calls=calls))
    state=storage.list_tours()[0]
    assert calls==[DEFAULT_TOUR_URL] and state['availability']==availability
    if status!=200: assert state['last_successful_check_at']==old
    assert len(summaries(result.paths['full'])) == 1
    assert bool(summaries(result.paths['delta'],'source_tour_id')) is (availability=='unavailable')


@pytest.mark.postgres
def test_partial_listing_does_not_probe_absence(storage,capture_history,documents,tmp_path):
    capture_history();calls=[]
    result=crawl_domestic(config(tmp_path),storage,discovery=discovery([],status='failed'),collector_class=collector(documents,calls=calls))
    assert result.status!='complete' and calls==[]
    assert storage.list_tours()[0]['availability']=='available'


@pytest.mark.postgres
def test_new_then_cached_and_historical_export(storage,documents,tmp_path):
    calls=[];cfg=config(tmp_path)
    first=crawl_domestic(cfg,storage,discovery=discovery([NEW_URL]),collector_class=collector(documents,calls=calls))
    assert first.status=='complete' and first.counts['new']==1 and calls==[NEW_URL]
    assert len(summaries(first.paths['delta'],'source_tour_id'))==1
    previous=first.paths['full'].read_bytes()
    second=crawl_domestic(cfg,storage,discovery=discovery([NEW_URL]),collector_class=collector(documents,calls=calls))
    assert second.status=='complete' and calls==[NEW_URL]
    assert summaries(second.paths['delta'],'source_tour_id')==[]
    assert export_batch_excel(storage,first.batch_id,tmp_path)['full'].read_bytes()==previous
    assert len(load_workbook(first.paths['full']).sheetnames)==6


@pytest.mark.postgres
def test_partial_does_not_promote_or_advance(storage,capture_history,documents,tmp_path):
    at=now()-timedelta(days=8);_,old=capture_history(at=at)
    result=crawl_domestic(config(tmp_path),storage,discovery=discovery([DEFAULT_TOUR_URL]),collector_class=collector(documents,capture_status='partial',price='7,690,000'))
    state=storage.list_tours()[0]
    assert result.status=='partial' and state['current_snapshot_id']==old.id
    assert state['last_successful_check_at']==at and state['requires_refresh']
    assert summaries(result.paths['delta'],'source_tour_id')==[]


@pytest.mark.postgres
def test_changed_and_reactivation(storage,capture_history,documents,tmp_path):
    capture_history(at=now()-timedelta(days=8));cfg=config(tmp_path)
    changed=crawl_domestic(cfg,storage,discovery=discovery([DEFAULT_TOUR_URL]),collector_class=collector(documents,price='7,690,000'))
    assert changed.counts['updated']==1
    gone=crawl_domestic(cfg,storage,discovery=discovery([]),collector_class=collector(documents,http_status=404))
    assert gone.counts['unavailable']==1
    revived=crawl_domestic(cfg,storage,discovery=discovery([DEFAULT_TOUR_URL]),collector_class=collector(documents,price='7,690,000'))
    assert revived.counts['reactivated']==1
    assert storage.list_tours()[0]['availability']=='available'


@pytest.mark.postgres
def test_schedule_slot_is_durable(storage,capture_history,documents,tmp_path):
    capture_history(); slot=now();calls=[]
    first=crawl_domestic(config(tmp_path),storage,scheduled_for=slot,discovery=discovery([DEFAULT_TOUR_URL]),collector_class=collector(documents,calls=calls))
    second=crawl_domestic(config(tmp_path),storage,scheduled_for=slot,discovery=lambda *a,**k:pytest.fail('duplicate scheduled discovery'))
    assert first.batch_id and second.status=='handled' and storage.last_handled_slot()==slot


@pytest.mark.postgres
def test_crash_after_snapshot_recovers_without_network(storage,documents,tmp_path):
    batch=storage.create_batch();tour=storage.register_tour(NEW_URL,'New',batch)
    run=storage.create_run(NEW_URL);storage.begin_attempt(batch,tour['id'],run,'new')
    collector(documents)(replace(config(tmp_path),tour_url=NEW_URL),storage,run).collect()
    parse_run(storage,run)
    recover_batches(storage,tmp_path)
    recovered=storage.get_batch(batch)
    assert recovered['status']=='partial' and recovered['export_status']=='complete'
    assert recovered['counts']['new']==1
    assert len(summaries(tmp_path/'domestic'/batch/'delta.xlsx'))==1


@pytest.mark.postgres
@pytest.mark.parametrize('status', [404, 410])
def test_unowned_diagnostic_does_not_mark_unavailable(storage, capture_history, tmp_path, status):
    from hanoitourist_crawler.acquisition.collector import Collector
    _, old = capture_history()
    batch = storage.create_batch()
    tour = storage.register_tour(DEFAULT_TOUR_URL, 'Tour', batch)
    run = storage.create_run(DEFAULT_TOUR_URL)
    storage.begin_attempt(batch, tour['id'], run, 'missing_listing')
    capture = Collector(config(tmp_path), storage, run)
    capture.page = SimpleNamespace(main_frame='main', set_default_timeout=lambda *a: None)
    # Playwright requests are hashable, as is the existing request mock.
    from tests.acquisition.test_collector import Request
    request = Request(DEFAULT_TOUR_URL, resource_type='document')
    request.frame = 'main'
    capture.response(SimpleNamespace(request=request, url=DEFAULT_TOUR_URL, status=status,
                                    headers={}, body=lambda: b'error'))
    storage.update_run(run, capture_status='failed', finished_at=now(),
                       manifest=capture.manifest, errors=capture.errors)
    storage.apply_attempt(batch, tour['id'], run)
    current = storage.get_tour(tour['id'])
    assert current['availability'] == 'available'
    assert current['current_snapshot_id'] == old.id
    assert current['last_successful_check_at'] == tour['last_successful_check_at']
    assert not capture.manifest['navigation'] and capture.node_id == ''
    assert storage.load_raw(run)[0].http_status == status and capture.errors
    storage.freeze_batch(batch, 'partial')
    paths = export_batch_excel(storage, batch, tmp_path)
    assert summaries(paths['delta'], 'source_tour_id') == []


@pytest.mark.postgres
def test_identity_rejected_complete_is_not_displayed(storage, capture_history, documents, tmp_path):
    _, partial = capture_history(status='partial')
    wrong = [replace(d, body=d.body.replace(b'8140', b'9900')) for d in documents]
    result = crawl_domestic(config(tmp_path), storage, discovery=discovery([DEFAULT_TOUR_URL]),
                            collector_class=collector(wrong))
    tour = storage.list_tours()[0]
    assert tour['source_tour_id'] == '8140' and tour['current_snapshot_id'] is None
    assert tour['availability'] == 'pending' and tour['last_successful_check_at'] is None
    assert tour['fingerprint'] is None and result.counts['failed'] == 1
    assert summaries(result.paths['full'], 'source_tour_id') == ['8140']
    assert summaries(result.paths['delta'], 'source_tour_id') == []
    book = load_workbook(result.paths['full'])
    try:
        assert len(book.sheetnames) == 6
        assert book['itinerary_days'].max_row == 1 + len(partial.days)
        assert book['departure_observations'].max_row == 1 + len(partial.departures)
        assert book['price_cells'].max_row == 1 + sum(len(d.price_cells) for d in partial.departures)
    finally:
        book.close()
    row = next(r for r in storage.batch_tours(result.batch_id) if r['frozen_state'])
    assert row['outcome'] == 'failed' and row['errors'] and row['attempt_run_ids']
    assert storage.get_snapshot(tour['latest_attempt_run_id']).source_tour_id == '9900'


@pytest.mark.postgres
@pytest.mark.parametrize('days,listed', [(8, True), (1, True), (8, False)])
def test_alias_target_is_not_captured_twice(storage, capture_history, documents, tmp_path, days, listed):
    known = 'https://hanoitourist.vn/z-known-tour'
    alias = 'https://hanoitourist.vn/a-renamed-tour'
    capture_history(url=known, at=now()-timedelta(days=days))
    seed = storage.create_batch()
    old = storage.register_tour(known, 'Known', seed)
    storage.freeze_batch(seed, 'complete')
    previous = storage.batch_tours(seed)
    calls = []
    result = crawl_domestic(config(tmp_path), storage,
                            discovery=discovery([alias, known] if listed else [alias]),
                            collector_class=collector(documents, calls=calls, price='7,690,000'))
    assert calls == [alias]
    assert result.status == 'complete' and len(storage.list_tours()) == 1
    rows = storage.batch_tours(result.batch_id)
    active = next(r for r in rows if r['tour_id'] == old['id'])
    assert active['outcome'] == 'changed' and active['change_kinds'] == ['updated']
    assert len(active['attempt_run_ids']) == 1 and result.counts == {'changed': 1, 'updated': 1}
    assert active['previous_snapshot_id'] == old['current_snapshot_id']
    current = storage.get_tour(old['id'])
    assert active['result_snapshot_id'] == current['current_snapshot_id']
    assert current['last_successful_check_at'] == storage.get_run(active['attempt_run_ids'][0])['finished_at']
    assert summaries(result.paths['delta']) == [old['source_tour_id']]
    assert storage.batch_tours(seed) == previous


@pytest.mark.postgres
@pytest.mark.parametrize('failed', [False, True])
def test_unsuccessful_alias_stays_separate(storage, capture_history, documents, tmp_path, failed):
    known = 'https://hanoitourist.vn/z-known-tour'
    alias = 'https://hanoitourist.vn/a-renamed-tour'
    capture_history(url=known, at=now()-timedelta(days=8))
    calls = []
    result = crawl_domestic(config(tmp_path), storage, discovery=discovery([alias, known]),
                            collector_class=collector(documents, calls=calls, http_status=503 if failed else 200,
                                                      capture_status='partial'))
    assert calls == [alias, known] and len(storage.list_tours()) == 2 and result.status == 'partial'
    assert all(t['superseded_by_id'] is None for t in storage.list_tours())
    assert summaries(result.paths['delta'], 'source_tour_id') == []


@pytest.mark.postgres
def test_unrelated_entities_are_both_processed(storage, capture_history, documents, tmp_path):
    capture_history(at=now()-timedelta(days=8))
    calls = []
    result = crawl_domestic(config(tmp_path), storage, discovery=discovery([DEFAULT_TOUR_URL, NEW_URL]),
                            collector_class=collector(documents, calls=calls, price='7,690,000'))
    assert set(calls) == {DEFAULT_TOUR_URL, NEW_URL} and len(calls) == 2
    assert result.status == 'complete' and len(storage.list_tours()) == 2
    assert result.counts['new'] == result.counts['updated'] == 1


@pytest.mark.postgres
def test_canonical_before_unknown_alias_still_verifies_identity(storage, capture_history, documents, tmp_path):
    known = 'https://hanoitourist.vn/a-known-tour'
    alias = 'https://hanoitourist.vn/z-renamed-tour'
    capture_history(url=known, at=now()-timedelta(days=8))
    calls = []
    result = crawl_domestic(config(tmp_path), storage, discovery=discovery([known, alias]),
                            collector_class=collector(documents, calls=calls, price='7,690,000'))
    assert calls == [known, alias] and result.status == 'complete' and len(storage.list_tours()) == 1
    active = next(r for r in storage.batch_tours(result.batch_id) if r['frozen_state'])
    assert active['outcome'] == 'changed' and active['change_kinds'] == ['updated']
    assert len(active['attempt_run_ids']) == 2 and len(summaries(result.paths['delta'], 'source_tour_id')) == 1
    assert active['result_snapshot_id'] == storage.list_tours()[0]['current_snapshot_id']


@pytest.mark.postgres
@pytest.mark.parametrize('flags', [None, False, True])
@pytest.mark.parametrize('status', [404, 410])
def test_recovery_requires_owned_terminal_evidence(storage, capture_history, documents, tmp_path, flags, status):
    _, old = capture_history()
    batch = storage.create_batch()
    tour = storage.register_tour(DEFAULT_TOUR_URL, 'Tour', batch)
    run = storage.create_run(DEFAULT_TOUR_URL)
    storage.begin_attempt(batch, tour['id'], run, 'missing_listing')
    collector(documents, http_status=status)(config(tmp_path), storage, run).collect()
    manifest = storage.get_run(run)['manifest']
    if flags is None:
        del manifest['navigation'][0]['owned_primary']; del manifest['navigation'][0]['terminal']
    else:
        manifest['navigation'][0]['owned_primary'] = flags
    storage.update_run(run, manifest=manifest)
    recover_batches(storage, tmp_path)
    current = storage.get_tour(tour['id'])
    assert current['availability'] == ('unavailable' if flags else 'available')
    assert current['current_snapshot_id'] == old.id
    assert current['last_successful_check_at'] == tour['last_successful_check_at']
    rows = storage.batch_tours(batch)
    assert rows[0]['change_kinds'] == (['unavailable'] if flags else [])
    assert rows[0]['outcome'] == ('unavailable' if flags else 'failed')
    recover_batches(storage, tmp_path)
    assert storage.batch_tours(batch) == rows and storage.get_tour(tour['id']) == current


@pytest.mark.postgres
@pytest.mark.parametrize('fallback', [True, False])
def test_recovery_rejects_wrong_identity_without_mutating_history(storage, capture_history, tmp_path, fallback):
    from hanoitourist_crawler.models import json_value
    partial = capture_history(status='partial')[1] if fallback else None
    batch = storage.create_batch()
    tour = storage.register_tour(DEFAULT_TOUR_URL, 'Known', batch)
    if not fallback: storage._update_tour(tour['id'], source_tour_id='8140')
    run, rejected = capture_history(source_id='9900')
    storage.begin_attempt(batch, tour['id'], run, 'retry')
    snapshots = [s for s in [partial, rejected] if s]
    before = {s.id: json_value(storage.get_snapshot_by_id(s.id)) for s in snapshots}
    raw = {s.run_id: [(d.id, d.body, d.sha256, d.captured_at) for d in storage.load_raw(s.run_id)] for s in snapshots}
    recover_batches(storage, tmp_path)
    state = storage.get_tour(tour['id'])
    assert state['current_snapshot_id'] is None and state['last_successful_check_at'] is None
    assert state['source_tour_id'] == '8140' and state['availability'] == 'pending'
    rows = storage.batch_tours(batch)
    assert rows[0]['display_snapshot_id'] == (partial.id if partial else None)
    assert rows[0]['outcome'] == 'failed' and rows[0]['errors'] and not rows[0]['change_kinds']
    paths = export_batch_excel(storage, batch, tmp_path)
    assert summaries(paths['full'], 'source_tour_id') == (['8140'] if partial else [])
    assert summaries(paths['delta'], 'source_tour_id') == []
    if not partial:
        book = load_workbook(paths['full'])
        try:
            assert all(ws.max_row == 1 for ws in book)
        finally: book.close()
    recover_batches(storage, tmp_path)
    assert storage.batch_tours(batch) == rows
    for s in snapshots:
        assert json_value(storage.get_snapshot_by_id(s.id)) == before[s.id]
        assert [(d.id, d.body, d.sha256, d.captured_at) for d in storage.load_raw(s.run_id)] == raw[s.run_id]
