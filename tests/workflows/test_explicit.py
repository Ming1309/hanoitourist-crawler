"""Explicit-target crawling: no discovery, no TTL skip, still a normal frozen batch."""

from dataclasses import replace
from datetime import timedelta

import pytest
from openpyxl import load_workbook

from hanoitourist_crawler.config import DEFAULT_TOUR_URL
from hanoitourist_crawler.models import RawDocument, now, uid
from hanoitourist_crawler.processing.parser import parse
from hanoitourist_crawler.workflows.incremental import crawl_domestic
from hanoitourist_crawler.workflows.retry import retry_domestic, select_targets
from tests.workflows.test_incremental import NEW_URL, config, summaries


def target_collector(documents, *, calls=None, status=200, capture_status='complete',
                     price=None, failing=(), interrupting=()):
    class FakeCollector:
        def __init__(self, config, storage, run_id, **kwargs):
            self.config, self.storage, self.run_id = config, storage, run_id

        def collect(self):
            url = self.config.tour_url
            if calls is not None:
                calls.append(url)
            if url in interrupting:
                raise KeyboardInterrupt()
            if url in failing or status != 200:
                code = 503 if url in failing else status
                doc = RawDocument(self.run_id, 1, 'http_document', 'tour', 'initial', url, b'error',
                                  http_status=code)
                self.storage.append_raw(doc)
                self.storage.update_run(self.run_id, capture_status='failed', finished_at=now(), manifest={
                    'navigation': [{'url': doc.url, 'status': code, 'raw_document_id': doc.id,
                                    'owned_primary': True, 'terminal': True}]})
                return
            sid = '9900' if url == NEW_URL else '8140'
            for original in documents:
                body = (original.body.replace(DEFAULT_TOUR_URL.encode(), url.encode())
                        .replace(b'8140', sid.encode()))
                if price:
                    body = body.replace(b'7,680,000', price.encode())
                self.storage.append_raw(replace(original, id=uid(), run_id=self.run_id, url=url, body=body))
            snapshot = parse(self.storage.load_raw(self.run_id), self.run_id)
            doc = self.storage.load_raw(self.run_id)[0]
            self.storage.update_run(self.run_id, capture_status=capture_status, finished_at=now(), manifest={
                'actions': {'initial': 'captured'}, 'inventory': snapshot.coverage['discovered'],
                'navigation': [{'url': doc.url, 'status': 200, 'raw_document_id': doc.id,
                                'owned_primary': True, 'terminal': True}]})
    return FakeCollector


@pytest.mark.postgres
def test_one_fresh_url_is_crawled_immediately_without_discovery(storage, capture_history, documents, tmp_path):
    at = now() - timedelta(days=1)
    _, baseline = capture_history(at=at)
    calls = []
    result = crawl_domestic(config(tmp_path), storage, urls=[DEFAULT_TOUR_URL],
                            collector_class=target_collector(documents, calls=calls))
    batch = storage.get_batch(result.batch_id)
    tour = storage.list_tours()[0]
    assert calls == [DEFAULT_TOUR_URL] and result.status == 'complete'
    assert batch['discovery_run_id'] is None and batch['discovery_complete'] is False
    assert batch['stage'] == 'frozen' and batch['export_status'] == 'complete'
    assert tour['current_snapshot_id'] != baseline.id
    assert tour['last_successful_check_at'] > at
    row = storage.batch_tours(result.batch_id)[0]
    assert row['selection_reason'] == 'requested' and len(row['attempt_run_ids']) == 1
    assert result.counts['unchanged'] == 1


@pytest.mark.postgres
def test_multiple_urls_are_processed_sequentially(storage, documents, tmp_path):
    calls = []
    result = crawl_domestic(config(tmp_path), storage, urls=[DEFAULT_TOUR_URL, NEW_URL],
                            collector_class=target_collector(documents, calls=calls))
    assert calls == [DEFAULT_TOUR_URL, NEW_URL]
    assert result.status == 'complete' and result.counts['new'] == 2
    assert len(storage.list_tours()) == 2
    accepted = {t['canonical_url']: t['current_snapshot_id'] for t in storage.list_tours()}
    assert set(accepted) == {DEFAULT_TOUR_URL, NEW_URL}
    assert all(accepted.values())


@pytest.mark.postgres
def test_duplicate_canonical_urls_make_one_attempt(storage, documents, tmp_path):
    calls = []
    variants = [DEFAULT_TOUR_URL, DEFAULT_TOUR_URL + '/', DEFAULT_TOUR_URL + '?utm_source=test']
    result = crawl_domestic(config(tmp_path), storage, urls=variants,
                            collector_class=target_collector(documents, calls=calls))
    rows = storage.batch_tours(result.batch_id)
    assert calls == [DEFAULT_TOUR_URL]
    assert len(storage.list_tours()) == 1 and len(rows) == 1
    assert len(rows[0]['attempt_run_ids']) == 1 and result.status == 'complete'


@pytest.mark.postgres
@pytest.mark.parametrize('url', [
    'https://example.com/tour-a',
    'http://hanoitourist.vn/tour-a',
    'https://hanoitourist.vn/tour-trong-nuoc',
    'https://hanoitourist.vn/tour-a/extra',
    'https://hanoitourist.vn/tour-a?page=2',
    '',
])
def test_invalid_urls_are_rejected_before_any_batch_or_run(storage, tmp_path, url):
    calls = []
    with pytest.raises(ValueError):
        crawl_domestic(config(tmp_path), storage, urls=[DEFAULT_TOUR_URL, url],
                       collector_class=target_collector([], calls=calls))
    assert calls == []
    assert storage.connection.execute('SELECT count(*) AS n FROM crawl_batches').fetchone()['n'] == 0
    assert storage.connection.execute('SELECT count(*) AS n FROM crawl_runs').fetchone()['n'] == 0


@pytest.mark.postgres
def test_alias_and_canonical_dedupe_by_verified_identity(storage, capture_history, documents, tmp_path):
    alias = 'https://hanoitourist.vn/a-renamed-tour'
    capture_history(at=now() - timedelta(days=1))
    seed = storage.create_batch()
    known = storage.register_tour(DEFAULT_TOUR_URL, 'Known', seed)
    storage.freeze_batch(seed, 'complete')
    calls = []
    result = crawl_domestic(config(tmp_path), storage, urls=[alias, DEFAULT_TOUR_URL],
                            collector_class=target_collector(documents, calls=calls, price='7,690,000'))
    tours = storage.list_tours()
    assert calls == [alias] and len(tours) == 1 and tours[0]['id'] == known['id']
    assert alias in storage.tour_aliases(known['id'])
    assert DEFAULT_TOUR_URL in storage.tour_aliases(known['id'])
    rows = storage.batch_tours(result.batch_id)
    active = next(r for r in rows if r['tour_id'] == known['id'])
    assert len(active['attempt_run_ids']) == 1 and result.status == 'complete'


@pytest.mark.postgres
def test_partial_attempt_does_not_overwrite_accepted_snapshot(storage, capture_history, documents, tmp_path):
    at = now() - timedelta(days=8)
    _, baseline = capture_history(at=at)
    result = crawl_domestic(config(tmp_path), storage, urls=[DEFAULT_TOUR_URL],
                            collector_class=target_collector(documents, capture_status='partial',
                                                             price='7,690,000'))
    tour = storage.list_tours()[0]
    row = next(r for r in storage.batch_tours(result.batch_id) if r['tour_id'] == tour['id'])
    assert result.status == 'partial'
    assert tour['current_snapshot_id'] == baseline.id
    assert tour['last_successful_check_at'] == at and tour['requires_refresh']
    assert row['outcome'] == 'partial'
    assert summaries(result.paths['delta'], 'source_tour_id') == []


@pytest.mark.postgres
def test_failed_target_does_not_stop_subsequent_tours(storage, documents, tmp_path):
    calls = []
    result = crawl_domestic(config(tmp_path), storage, urls=[DEFAULT_TOUR_URL, NEW_URL],
                            collector_class=target_collector(documents, calls=calls,
                                                             failing={DEFAULT_TOUR_URL}))
    first = next(t for t in storage.list_tours() if t['canonical_url'] == DEFAULT_TOUR_URL)
    second = next(t for t in storage.list_tours() if t['canonical_url'] == NEW_URL)
    assert calls == [DEFAULT_TOUR_URL, NEW_URL] and result.status == 'partial'
    assert first['current_snapshot_id'] is None and first['requires_refresh']
    assert second['current_snapshot_id'] is not None
    assert result.counts['failed'] == 1 and result.counts['new'] == 1


@pytest.mark.postgres
def test_explicit_batch_retains_other_catalogue_tours(storage, capture_history, documents, tmp_path):
    capture_history(at=now() - timedelta(days=8))
    seed = storage.create_batch()
    other = storage.register_tour(DEFAULT_TOUR_URL, 'Other', seed)
    storage.freeze_batch(seed, 'complete')
    before = storage.get_tour(other['id'])
    result = crawl_domestic(config(tmp_path), storage, urls=[NEW_URL],
                            collector_class=target_collector(documents))
    assert storage.get_tour(other['id']) == before
    other_row = next(r for r in storage.batch_tours(result.batch_id) if r['tour_id'] == other['id'])
    assert other_row['discovered'] is False and other_row['outcome'] == 'skipped'
    assert other_row['attempt_run_ids'] == []
    new = next(t for t in storage.list_tours() if t['canonical_url'] == NEW_URL)
    assert summaries(result.paths['delta'], 'source_tour_id') == [new['source_tour_id']]
    assert set(summaries(result.paths['full'], 'source_tour_id')) == {
        other['source_tour_id'], new['source_tour_id']}
    book = load_workbook(result.paths['full'])
    try:
        assert len(book.sheetnames) == 6 and 'Nhật ký tải' not in book.sheetnames
    finally:
        book.close()


@pytest.mark.postgres
def test_interrupted_explicit_batch_is_recoverable_and_retryable(storage, documents, tmp_path):
    calls = []
    with pytest.raises(KeyboardInterrupt):
        crawl_domestic(config(tmp_path), storage, urls=[DEFAULT_TOUR_URL, NEW_URL],
                       collector_class=target_collector(documents, calls=calls,
                                                        interrupting={NEW_URL}))
    batch = str(storage.connection.execute(
        'SELECT id FROM crawl_batches ORDER BY started_at DESC LIMIT 1').fetchone()['id'])
    rows = {r['tour_id']: r for r in storage.batch_tours(batch)}
    second = next(t for t in storage.list_tours() if t['canonical_url'] == NEW_URL)
    assert calls == [DEFAULT_TOUR_URL, NEW_URL]
    assert storage.get_batch(batch)['status'] == 'partial'
    assert storage.get_batch(batch)['export_status'] == 'complete'
    assert rows[second['id']]['outcome'] == 'failed' and second['requires_refresh']
    targets = select_targets(storage, batch, [NEW_URL])
    assert len(targets) == 1 and targets[0]['skip_reason'] is None
    retried = retry_domestic(config(tmp_path), storage, batch, [NEW_URL],
                             collector_class=target_collector(documents))
    assert retried.status == 'partial'
    assert storage.get_tour(second['id'])['current_snapshot_id'] is not None
