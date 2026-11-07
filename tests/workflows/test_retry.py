"""Scoped retry contracts; PostgreSQL tests only use the isolated test fixture."""
from dataclasses import replace
from datetime import timedelta

import pytest

from hanoitourist_crawler.config import Config, DEFAULT_TOUR_URL
from hanoitourist_crawler.models import now
from hanoitourist_crawler.workflows.retry import select_targets, retry_domestic, retry_report


def source_failure(storage, capture_history):
    batch = storage.create_batch()
    tour = storage.register_tour(DEFAULT_TOUR_URL, 'Tour', batch)
    run, _ = capture_history(status='partial')
    storage.begin_attempt(batch, tour['id'], run, 'retry')
    storage.apply_attempt(batch, tour['id'], run)
    storage.freeze_batch(batch, 'partial')
    return batch, tour['id'], run


@pytest.mark.postgres
@pytest.mark.parametrize('old_status,new_status,unfinished', [
    (200, 200, False), (404, 200, False), (410, 200, False),
    (200, 404, False), (200, 410, False), (404, 200, True)])
def test_recovery_cannot_undo_newer_scoped_publication(storage, capture_history, documents, tmp_path,
                                                     old_status, new_status, unfinished):
    from pathlib import Path
    from tests.workflows.test_incremental import collector
    from hanoitourist_crawler.workflows.incremental import recover_batches
    from hanoitourist_crawler.workflows.pipeline import parse_run
    source, tid, _ = source_failure(storage, capture_history)
    abandoned = storage.create_batch()
    old_run = storage.create_run(DEFAULT_TOUR_URL)
    storage.begin_attempt(abandoned, tid, old_run, 'retry')
    collector(documents, http_status=old_status)(Config(), storage, old_run).collect()
    if old_status == 200:
        parse_run(storage, old_run)
    if unfinished:
        storage.update_run(old_run, capture_status='running', finished_at=None)
    old = storage.get_run(old_run), storage.get_snapshot(old_run), storage.load_raw(old_run)
    result = retry_domestic(Config(export_dir=tmp_path), storage, source, [DEFAULT_TOUR_URL],
                            collector_class=collector(documents, http_status=new_status, price='7,690,000'))
    current = storage.get_tour(tid)
    published = storage.get_batch(result.batch_id), storage.batch_tours(result.batch_id)
    artifacts = {name: Path(path).read_bytes() for name, path in result.paths.items()}
    recover_batches(storage, tmp_path, batch_id=abandoned)
    assert storage.get_tour(tid) == current
    row = storage.batch_tours(abandoned)[0]
    assert row['outcome'] == 'skipped' and row['change_kinds'] == []
    assert (storage.get_snapshot(old_run), storage.load_raw(old_run)) == old[1:]
    if unfinished:
        recovered = storage.get_run(old_run)
        assert recovered['capture_status'] == 'partial' and recovered['finished_at'] is not None
        assert recovered['errors'] == old[0]['errors'] + ['Abandoned capture; checkpoints retained']
    else:
        assert storage.get_run(old_run) == old[0]
    assert (storage.get_batch(result.batch_id), storage.batch_tours(result.batch_id)) == published
    assert {name: Path(path).read_bytes() for name, path in result.paths.items()} == artifacts


def test_requires_frozen_error_source(storage, capture_history):
    batch, tid, run = source_failure(storage, capture_history)
    targets = select_targets(storage, batch, [DEFAULT_TOUR_URL, DEFAULT_TOUR_URL + '/'])
    assert len(targets) == 1
    assert targets[0]['source_run_ids'] == [run]
    assert targets[0]['tour_id'] == tid
    with pytest.raises(ValueError):
        select_targets(storage, batch, [DEFAULT_TOUR_URL, 'https://hanoitourist.vn/another-tour'])


def test_old_complete_still_eligible_newer_success_skips(storage, capture_history):
    batch, tid, _ = source_failure(storage, capture_history)
    end = storage.get_batch(batch)['finished_at']
    for offset, expected in [(-1, None), (0, None), (1, 'newer_success')]:
        run, snap = capture_history(at=end + timedelta(seconds=offset))
        storage._update_tour(tid, current_snapshot_id=snap.id, last_successful_check_at=end + timedelta(seconds=offset))
        assert select_targets(storage, batch, [DEFAULT_TOUR_URL])[0]['skip_reason'] == expected


def test_invalid_input_zero_writes(storage, capture_history, tmp_path):
    batch, _, _ = source_failure(storage, capture_history)
    before = storage.connection.execute('SELECT count(*) AS n FROM domestic_batches').fetchone()['n']
    config = Config(database_url='unused', export_dir=tmp_path)
    with pytest.raises(ValueError):
        retry_domestic(config, storage, batch, [DEFAULT_TOUR_URL, 'https://hanoitourist.vn/nope'])
    assert storage.connection.execute('SELECT count(*) AS n FROM domestic_batches').fetchone()['n'] == before
    assert not list(tmp_path.iterdir())


def test_report_requires_retry_context(storage, tmp_path):
    batch = storage.create_batch()
    with pytest.raises(ValueError):
        retry_report(storage, batch, tmp_path)


def test_busy_cross_connection_no_artifacts(storage, tmp_path):
    import os
    from hanoitourist_crawler.persistence.storage import Storage
    schema = storage.connection.execute('SELECT current_schema() AS s').fetchone()['s']
    other = Storage(os.environ['TEST_DATABASE_URL'], schema=schema)
    try:
        with other.crawl_lock() as acquired:
            assert acquired
            result = retry_domestic(Config(export_dir=tmp_path), storage,
                                    '00000000-0000-0000-0000-000000000000', [DEFAULT_TOUR_URL])
            assert result.status == 'busy'
            assert not list(tmp_path.iterdir())
    finally:
        other.close()


def test_all_skipped_no_database_writes(storage, capture_history, tmp_path):
    batch, tid, _ = source_failure(storage, capture_history)
    storage._update_tour(tid, availability='unavailable')
    before = storage.connection.execute('SELECT txid_current() AS x').fetchone()['x']
    result = retry_domestic(Config(export_dir=tmp_path), storage, batch, [DEFAULT_TOUR_URL])
    assert result.status == 'handled' and result.batch_id is None
    assert storage.connection.execute('SELECT txid_current() AS x').fetchone()['x'] == before + 1
    assert result.paths['report']


def test_real_engine_publication_and_offline_report(storage, capture_history, documents, tmp_path):
    from tests.workflows.test_incremental import collector
    from pathlib import Path
    batch, tid, _ = source_failure(storage, capture_history)
    original = storage.get_batch(batch)
    rows = storage.batch_tours(batch)
    calls = []
    result = retry_domestic(Config(export_dir=tmp_path), storage, batch, [DEFAULT_TOUR_URL],
                            collector_class=collector(documents, calls=calls))
    assert calls == [DEFAULT_TOUR_URL]
    assert result.status == 'partial'
    new = storage.get_batch(result.batch_id)
    assert new['discovery_run_id'] is None and not new['discovery_complete']
    assert new['scheduled_for'] is None
    assert storage.get_tour(tid)['current_snapshot_id']
    assert storage.get_batch(batch) == original
    assert storage.batch_tours(batch) == rows
    assert set(result.paths) == {'full', 'delta', 'report'}
    paths_before = {k: Path(v).read_bytes() for k,v in result.paths.items()}
    report, finished = retry_report(storage, result.batch_id, tmp_path)
    assert finished and report != result.paths['report']
    assert calls == [DEFAULT_TOUR_URL]
    for k,v in result.paths.items():
        assert Path(v).read_bytes() == paths_before[k]


@pytest.mark.postgres
@pytest.mark.parametrize('second_status', ['complete', 'partial', 'failed', 'rejected'])
def test_two_selected_preserve_source_unrelated_and_old_baseline(
        storage, capture_history, documents, tmp_path, monkeypatch, second_status):
    from pathlib import Path
    from hanoitourist_crawler.reporting.excel import export_batch_excel
    from hanoitourist_crawler.models import uid, json_value
    from hanoitourist_crawler.processing.parser import parse
    from openpyxl import load_workbook
    urls = [DEFAULT_TOUR_URL, 'https://hanoitourist.vn/retry-second', 'https://hanoitourist.vn/retry-unrelated']
    ids = dict(zip(urls, ['9011', '9012', '9013']))
    _, baseline = capture_history(url=urls[1], source_id=ids[urls[1]], at=now()-timedelta(days=1))
    source = storage.create_batch()
    tours, source_runs = [], []
    for url in urls:
        tour = storage.register_tour(url, 'Tour', source)
        rid, _ = capture_history(url=url, source_id=ids[url], status='partial')
        storage.begin_attempt(source, tour['id'], rid, 'retry')
        storage.apply_attempt(source, tour['id'], rid)
        tours.append(storage.get_tour(tour['id']))
        source_runs.append(rid)
    storage.freeze_batch(source, 'partial')
    source_paths = export_batch_excel(storage, source, tmp_path)
    source_bytes = {k: Path(v).read_bytes() for k,v in source_paths.items()}
    before = (storage.get_batch(source), storage.batch_tours(source),
              [storage.get_run(rid) for rid in source_runs],
              [json_value(storage.get_snapshot(rid)) for rid in source_runs],
              [[(d.id, d.body, d.sha256, d.captured_at) for d in storage.load_raw(rid)] for rid in source_runs])
    pending = storage.create_batch()
    storage.ensure_batch_tour(pending, tours[2]['id'])
    pending_before = storage.get_batch(pending), storage.batch_tours(pending)
    scheduler_before = storage.last_handled_slot()
    def forbidden(*args, **kwargs): raise AssertionError('Unscoped workload')
    monkeypatch.setattr('hanoitourist_crawler.workflows.incremental.recover_batches', forbidden)
    monkeypatch.setattr('hanoitourist_crawler.workflows.incremental.crawl_domestic', forbidden)
    monkeypatch.setattr('hanoitourist_crawler.acquisition.discovery.discover_domestic', forbidden)
    calls = []
    class OfflineCollector:
        def __init__(self, config, repository, run_id, **kwargs):
            self.config, self.storage, self.run_id = config, repository, run_id
        def collect(self):
            url = self.config.tour_url
            calls.append(url)
            status = second_status if url == urls[1] else 'complete'
            if status == 'failed':
                self.storage.update_run(self.run_id, capture_status='failed', finished_at=now(), errors=['Offline navigation failure'])
                return
            sid = '9999' if status == 'rejected' else ids[url]
            for doc in documents:
                body = doc.body.replace(DEFAULT_TOUR_URL.encode(), url.encode()).replace(b'8140', sid.encode())
                self.storage.append_raw(replace(doc, id=uid(), run_id=self.run_id, url=url, body=body))
            snapshot = parse(self.storage.load_raw(self.run_id), self.run_id)
            raw = self.storage.load_raw(self.run_id)[0]
            self.storage.update_run(self.run_id, capture_status='partial' if status == 'partial' else 'complete',
                                    finished_at=now(), manifest={'actions': {'initial': 'captured'},
                                    'inventory': snapshot.coverage['discovered'], 'navigation': [
                                        {'url': url, 'status': 200, 'raw_document_id': raw.id,
                                         'owned_primary': True, 'terminal': True}]})
    result = retry_domestic(Config(export_dir=tmp_path), storage, source, urls[:2], collector_class=OfflineCollector)
    assert calls == urls[:2] and result.status == 'partial'
    rows = {r['tour_id']:r for r in storage.batch_tours(result.batch_id)}
    assert all(len(rows[t['id']]['attempt_run_ids']) == 1 for t in tours[:2])
    assert not rows[tours[2]['id']]['attempt_run_ids']
    assert storage.get_tour(tours[0]['id'])['current_snapshot_id']
    current = storage.get_tour(tours[1]['id'])
    if second_status == 'complete':
        assert rows[tours[1]['id']]['outcome'] == 'unchanged'
        assert current['current_snapshot_id'] != baseline.id
    else:
        assert current['current_snapshot_id'] == baseline.id
        assert current['last_successful_check_at'] == tours[1]['last_successful_check_at']
        assert rows[tours[1]['id']]['outcome'] == ('partial' if second_status == 'partial' else 'failed')
    assert storage.get_tour(tours[2]['id']) == tours[2]
    assert (storage.get_batch(pending), storage.batch_tours(pending)) == pending_before
    assert storage.last_handled_slot() == scheduler_before
    after = (storage.get_batch(source), storage.batch_tours(source),
             [storage.get_run(rid) for rid in source_runs],
             [json_value(storage.get_snapshot(rid)) for rid in source_runs],
             [[(d.id, d.body, d.sha256, d.captured_at) for d in storage.load_raw(rid)] for rid in source_runs])
    assert before == after
    assert all(Path(path).read_bytes() == source_bytes[k] for k,path in source_paths.items())
    for kind in ('full', 'delta'):
        book = load_workbook(result.paths[kind])
        try:
            assert len(book.sheetnames) == 7
            sheet = book['Danh sách tour']
            col = [c.value for c in sheet[4]].index('ID catalogue') + 1
            assert {sheet.cell(i,col).value for i in range(5,sheet.max_row+1)} == (
                {t['id'] for t in tours} if kind == 'full' else {tours[0]['id']})
        finally:
            book.close()


@pytest.mark.postgres
@pytest.mark.parametrize('failure', [KeyboardInterrupt, RuntimeError])
@pytest.mark.parametrize('stop_at', ['capture', 'publication'])
@pytest.mark.parametrize('has_baseline', [False, True])
def test_stopped_retry_preserves_source_unrelated_and_exception(
        storage, capture_history, documents, tmp_path, monkeypatch, capsys,
        failure, stop_at, has_baseline):
    import json
    import re
    from hanoitourist_crawler.reporting.excel import export_batch_excel
    from hanoitourist_crawler.models import json_value, uid
    from tests.workflows.test_incremental import collector
    baseline = capture_history(at=now()-timedelta(days=1))[1] if has_baseline else None
    source, tid, source_run = source_failure(storage, capture_history)
    source_paths = export_batch_excel(storage, source, tmp_path)
    source_bytes = {name: path.read_bytes() for name, path in source_paths.items()}
    source_before = (storage.get_batch(source), storage.batch_tours(source), storage.get_run(source_run),
                     json_value(storage.get_snapshot(source_run)), storage.load_raw(source_run))
    target_before = storage.get_tour(tid)
    aliases_before = storage.tour_aliases(tid)
    baseline_before = json_value(storage.get_snapshot_by_id(baseline.id)) if baseline else None
    seed = storage.create_batch()
    unrelated_url = 'https://hanoitourist.vn/unrelated-stopped-retry'
    unrelated = storage.register_tour(unrelated_url, 'Unrelated', seed)
    storage.freeze_batch(seed, 'partial')
    pending = storage.create_batch()
    unrelated_run = storage.create_run(unrelated_url)
    storage.begin_attempt(pending, unrelated['id'], unrelated_run, 'new')
    unrelated_before = (storage.get_tour(unrelated['id']), storage.get_batch(pending),
                        storage.batch_tours(pending), storage.get_run(unrelated_run))
    scheduler_before = storage.last_handled_slot()
    calls, checkpoints = [], {}
    error = failure('Stopped offline acquisition/publication')
    def forbidden(*args, **kwargs):
        raise AssertionError('Retry must not discover or recover other work')
    monkeypatch.setattr('hanoitourist_crawler.workflows.incremental.recover_batches', forbidden)
    monkeypatch.setattr('hanoitourist_crawler.acquisition.discovery.discover_domestic', forbidden)
    class StoppingCollector(collector(documents)):
        def collect(self):
            calls.append(self.config.tour_url)
            if stop_at == 'publication':
                super().collect()
            else:
                for document in documents:
                    self.storage.append_raw(replace(document, id=uid(), run_id=self.run_id))
                self.storage.update_run(self.run_id, errors=['Capture checkpoint'])
            checkpoints['run'] = self.storage.get_run(self.run_id)
            checkpoints['raw'] = self.storage.load_raw(self.run_id)
            if stop_at == 'capture':
                raise error
    if stop_at == 'publication':
        def reject(batch, tour, run):
            assert storage.get_snapshot(run).status == 'complete'
            raise error
        monkeypatch.setattr(storage, 'apply_attempt', reject)
    with pytest.raises(failure) as raised:
        retry_domestic(Config(export_dir=tmp_path), storage, source, [DEFAULT_TOUR_URL],
                       collector_class=StoppingCollector)
    if failure is RuntimeError:
        assert raised.value is error
    output = capsys.readouterr().out
    batch_id = re.search(r'batch=([0-9a-f-]{36}) source_batch=', output).group(1)
    batch = storage.get_batch(batch_id)
    assert calls == [DEFAULT_TOUR_URL]
    assert batch['stage'] == 'frozen' and batch['status'] == 'partial'
    assert batch['export_status'] == 'complete'
    assert batch['discovery_run_id'] is None and batch['scheduled_for'] is None
    row = next(row for row in storage.batch_tours(batch_id) if row['tour_id'] == tid)
    run = row['attempt_run_ids'][0]
    assert row['attempt_run_ids'] == [checkpoints['run']['id']]
    assert row['outcome'] == 'failed' and not row['change_kinds']
    assert row['errors'] == ['Scoped retry stopped before atomic publication']
    after = storage.get_run(run)
    assert after['parse_status'] == 'pending' and storage.get_snapshot(run) is None
    if stop_at == 'capture':
        assert after['capture_status'] == 'failed' and after['finished_at']
        assert after['errors'] == ['Capture checkpoint', 'Scoped retry interrupted; checkpoints retained']
    else:
        assert after == checkpoints['run']
    assert storage.load_raw(run) == checkpoints['raw']
    current = storage.get_tour(tid)
    for key in ('current_snapshot_id', 'last_successful_check_at', 'fingerprint', 'fingerprint_version',
                'availability', 'availability_changed_at', 'source_tour_id', 'canonical_url'):
        assert current[key] == target_before[key]
    assert current['requires_refresh'] and current['latest_attempt_run_id'] == run
    assert storage.tour_aliases(tid) == aliases_before
    if baseline:
        assert json_value(storage.get_snapshot_by_id(baseline.id)) == baseline_before
    assert (storage.get_batch(source), storage.batch_tours(source), storage.get_run(source_run),
            json_value(storage.get_snapshot(source_run)), storage.load_raw(source_run)) == source_before
    assert {name: path.read_bytes() for name, path in source_paths.items()} == source_bytes
    assert (storage.get_tour(unrelated['id']), storage.get_batch(pending),
            storage.batch_tours(pending), storage.get_run(unrelated_run)) == unrelated_before
    assert storage.last_handled_slot() == scheduler_before
    reports = list((tmp_path/'diagnostics'/'retry'/batch_id).glob('*/report.json'))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report['finished'] and report['targets'][0]['retry_run_ids'] == [run]
    assert report['targets'][0]['accepted_after'] == target_before['current_snapshot_id']


@pytest.mark.postgres
@pytest.mark.parametrize('failure', [KeyboardInterrupt, RuntimeError])
def test_stopped_retry_does_not_overwrite_completed_attempt(
        storage, capture_history, documents, tmp_path, capsys, failure):
    import re
    from openpyxl import load_workbook
    from hanoitourist_crawler.models import uid
    from tests.workflows.test_incremental import collector
    second = 'https://hanoitourist.vn/retry-stop-second'
    _, baseline = capture_history(url=second, source_id='9021', at=now()-timedelta(days=1))
    source = storage.create_batch()
    tours = []
    for url, sid in [(DEFAULT_TOUR_URL, '8140'), (second, '9021')]:
        tour = storage.register_tour(url, 'Tour', source)
        run, _ = capture_history(url=url, source_id=sid, status='partial')
        storage.begin_attempt(source, tour['id'], run, 'retry')
        storage.apply_attempt(source, tour['id'], run)
        tours.append(tour['id'])
    storage.freeze_batch(source, 'partial')
    calls = []
    class SecondStops(collector(documents)):
        def collect(self):
            calls.append(self.config.tour_url)
            if self.config.tour_url == DEFAULT_TOUR_URL:
                super().collect()
            else:
                for document in documents:
                    self.storage.append_raw(replace(document, id=uid(), run_id=self.run_id,
                        url=second, body=document.body.replace(DEFAULT_TOUR_URL.encode(), second.encode())
                                                     .replace(b'8140', b'9021')))
                raise failure('Second selected capture stopped')
    with pytest.raises(failure):
        retry_domestic(Config(export_dir=tmp_path), storage, source, [DEFAULT_TOUR_URL, second],
                       collector_class=SecondStops)
    batch_id = re.search(r'batch=([0-9a-f-]{36}) source_batch=', capsys.readouterr().out).group(1)
    rows = {row['tour_id']: row for row in storage.batch_tours(batch_id)}
    assert calls == [DEFAULT_TOUR_URL, second]
    assert all(len(rows[tid]['attempt_run_ids']) == 1 for tid in tours)
    first = storage.get_tour(tours[0])
    first_run = rows[tours[0]]['attempt_run_ids'][0]
    assert rows[tours[0]]['outcome'] == 'changed' and rows[tours[0]]['change_kinds'] == ['new']
    assert first['current_snapshot_id'] == storage.get_snapshot(first_run).id
    assert storage.get_run(first_run)['capture_status'] == 'complete'
    assert storage.get_run(first_run)['parse_status'] == 'complete'
    assert not first['requires_refresh']
    assert rows[tours[1]]['outcome'] == 'failed' and not rows[tours[1]]['change_kinds']
    assert storage.get_tour(tours[1])['current_snapshot_id'] == baseline.id
    assert storage.get_snapshot(rows[tours[1]]['attempt_run_ids'][0]) is None
    batch = storage.get_batch(batch_id)
    assert batch['stage'] == 'frozen' and batch['export_status'] == 'complete'
    book = load_workbook(tmp_path/'domestic'/batch_id/'delta.xlsx')
    try:
        summary = book['Danh sách tour']
        column = [cell.value for cell in summary[4]].index('ID catalogue') + 1
        assert [summary.cell(i, column).value for i in range(5, summary.max_row+1)] == [tours[0]]
    finally:
        book.close()


def test_rejected_complete_is_atomic_and_recovery_safe(storage, capture_history, documents, tmp_path):
    from tests.workflows.test_incremental import collector
    from hanoitourist_crawler.workflows.incremental import recover_batches
    batch, tid, _ = source_failure(storage, capture_history)
    outsider = 'https://hanoitourist.vn/outsider'
    seed = storage.create_batch()
    capture_history(url=outsider, source_id='8140')
    outside = storage.register_tour(outsider, 'Outside', seed)
    storage.freeze_batch(seed, 'partial')
    # Existing source ID now belongs to an unselected identity; publication must not merge.
    original = storage.get_tour(outside['id'])
    result = retry_domestic(Config(export_dir=tmp_path), storage, batch, [DEFAULT_TOUR_URL],
                            collector_class=collector(documents))
    row = next(r for r in storage.batch_tours(result.batch_id) if r['tour_id'] == tid)
    assert row['outcome'] == 'failed'
    assert storage.get_tour(tid)['current_snapshot_id'] is None
    assert storage.get_snapshot(row['attempt_run_ids'][0]).status == 'complete'
    assert storage.get_tour(outside['id']) == original
    with storage.crawl_lock():
        recover_batches(storage, tmp_path, batch_id=result.batch_id)
    assert storage.get_tour(tid)['current_snapshot_id'] is None
