"""Attempt completion through its caller-facing seam and real PostgreSQL adapter."""
import os
from datetime import timedelta

import psycopg
import pytest

from hanoitourist_crawler.workflows.attempts import complete_daily, complete_recovery, complete_retry
from hanoitourist_crawler.config import Config, DEFAULT_TOUR_URL
from hanoitourist_crawler.models import now
from hanoitourist_crawler.persistence.storage import Storage
from tests.workflows.test_incremental import collector

pytestmark = pytest.mark.postgres


def staged_attempt(storage, documents):
    batch = storage.create_batch()
    tour = storage.register_tour(DEFAULT_TOUR_URL, 'Tour', batch)
    run = storage.create_run(DEFAULT_TOUR_URL)
    storage.begin_attempt(batch, tour['id'], run, 'retry')
    collector(documents)(Config(), storage, run).collect()
    target = {'tour_id': tour['id'], 'aliases': [DEFAULT_TOUR_URL]}
    return batch, target, run


def test_stopped_daily_running_capture_keeps_raw_and_never_parses_or_publishes(storage, documents, monkeypatch):
    from hanoitourist_crawler.workflows.attempts import complete_daily_stopped
    batch, target, run = staged_attempt(storage, documents)
    storage.update_run(run, capture_status='running', finished_at=None, errors=['Checkpoint'])
    before = storage.load_raw(run)
    finished = now()
    def forbidden(*args, **kwargs):
        pytest.fail('Stopped daily capture must not parse or publish')
    monkeypatch.setattr('hanoitourist_crawler.workflows.pipeline.parse_run', forbidden)
    monkeypatch.setattr(storage, 'apply_attempt', forbidden)
    complete_daily_stopped(storage, batch, target['tour_id'], run, clock=lambda: finished)
    saved = storage.get_run(run)
    assert saved['capture_status'] == 'failed' and saved['finished_at'] == finished
    assert saved['parse_status'] == 'pending'
    assert saved['errors'] == ['Checkpoint', 'Daily crawl interrupted; checkpoints retained']
    assert storage.get_snapshot(run) is None and storage.load_raw(run) == before
    assert storage.batch_tours(batch)[0]['outcome'] == 'failed'
    assert storage.get_tour(target['tour_id'])['requires_refresh']


def retry(storage, batch, target, run):
    return complete_retry(storage, batch, target, run,
                          attempted={target['tour_id']}, selected={target['tour_id']: target})


@pytest.mark.parametrize('mode', ['daily', 'retry'])
def test_completion_publishes_accepted_snapshot(storage, documents, mode):
    batch, target, run = staged_attempt(storage, documents)
    result = (complete_daily(storage, batch, target['tour_id'], run) if mode == 'daily'
              else retry(storage, batch, target, run))
    assert result == target['tour_id']
    snapshot = storage.get_snapshot(run)
    assert snapshot.status == 'complete'
    assert storage.get_tour(result)['current_snapshot_id'] == snapshot.id
    assert storage.batch_tours(batch)[0]['change_kinds'] == ['new']


@pytest.mark.parametrize('mode', ['daily', 'retry'])
@pytest.mark.parametrize('has_baseline', [False, True])
def test_commit_visibility_preserves_daily_and_retry_difference(
        storage, documents, capture_history, monkeypatch, mode, has_baseline):
    baseline = capture_history(at=now()-timedelta(days=1))[1] if has_baseline else None
    baseline_id = baseline.id if baseline else None
    batch, target, run = staged_attempt(storage, documents)
    before = storage.get_tour(target['tour_id'])
    raw = storage.load_raw(run)
    schema = storage.connection.execute('SELECT current_schema() AS s').fetchone()['s']
    reader = Storage(os.environ['TEST_DATABASE_URL'], schema=schema)
    original = storage.apply_attempt
    observed = []
    def publish(*args):
        assert storage.get_snapshot(run).status == 'complete'
        observed.append(reader.get_snapshot(run) is not None)
        assert reader.get_run(run)['capture_status'] == 'complete'
        assert reader.load_raw(run) == raw
        visible = reader.get_tour(target['tour_id'])
        assert visible['current_snapshot_id'] == baseline_id
        assert visible['fingerprint'] == before['fingerprint']
        assert visible['last_successful_check_at'] == before['last_successful_check_at']
        result = original(*args)
        expected = storage.get_snapshot(run).id if mode == 'daily' else baseline_id
        assert reader.get_tour(target['tour_id'])['current_snapshot_id'] == expected
        return result
    monkeypatch.setattr(storage, 'apply_attempt', publish)
    try:
        if mode == 'daily':
            complete_daily(storage, batch, target['tour_id'], run)
        else:
            retry(storage, batch, target, run)
        assert observed == [mode == 'daily']
        assert reader.get_snapshot(run).status == 'complete'
        assert reader.get_tour(target['tour_id'])['current_snapshot_id'] == storage.get_snapshot(run).id
        if baseline:
            assert reader.get_snapshot_by_id(baseline.id) == baseline
    finally:
        reader.close()


@pytest.mark.parametrize('mode', ['daily', 'retry'])
@pytest.mark.parametrize('failure', [RuntimeError, KeyboardInterrupt, psycopg.OperationalError])
@pytest.mark.parametrize('has_baseline', [False, True])
def test_publication_failure_preserves_raw_and_commit_policy(
        storage, documents, capture_history, monkeypatch, mode, failure, has_baseline):
    baseline = capture_history(at=now()-timedelta(days=1))[1] if has_baseline else None
    batch, target, run = staged_attempt(storage, documents)
    before = storage.get_tour(target['tour_id'])
    aliases = storage.tour_aliases(target['tour_id'])
    raw = [(d.id, d.sha256, d.body, d.captured_at) for d in storage.load_raw(run)]
    def fail(*args):
        assert storage.get_snapshot(run).status == 'complete'
        raise failure('publication unavailable')
    monkeypatch.setattr(storage, 'apply_attempt', fail)
    with pytest.raises(failure):
        if mode == 'daily':
            complete_daily(storage, batch, target['tour_id'], run)
        else:
            retry(storage, batch, target, run)
    assert (storage.get_snapshot(run) is not None) is (mode == 'daily')
    assert storage.get_tour(target['tour_id']) == before
    assert storage.tour_aliases(target['tour_id']) == aliases
    if baseline:
        assert storage.get_snapshot_by_id(baseline.id) == baseline
    assert storage.get_run(run)['capture_status'] == 'complete'
    assert storage.get_run(run)['parse_status'] == ('complete' if mode == 'daily' else 'pending')
    assert [(d.id, d.sha256, d.body, d.captured_at) for d in storage.load_raw(run)] == raw


@pytest.mark.parametrize('mode', ['daily', 'retry'])
def test_parse_failure_records_diagnostic_then_completes(storage, documents, monkeypatch, mode):
    batch, target, run = staged_attempt(storage, documents)
    def fail(*args, **kwargs):
        raise ValueError('secret must not escape')
    monkeypatch.setattr('hanoitourist_crawler.workflows.pipeline.parse', fail)
    if mode == 'daily':
        complete_daily(storage, batch, target['tour_id'], run)
    else:
        retry(storage, batch, target, run)
    assert storage.get_run(run)['parse_status'] == 'failed'
    assert 'secret must not escape' not in str(storage.get_run(run)['errors'])
    assert storage.get_snapshot(run) is None
    assert storage.batch_tours(batch)[0]['outcome'] == 'failed'
    assert storage.get_tour(target['tour_id'])['requires_refresh']


def test_retry_parse_database_failure_rolls_back_and_propagates(storage, documents, monkeypatch):
    batch, target, run = staged_attempt(storage, documents)
    def fail(*args, **kwargs):
        raise psycopg.OperationalError('database unavailable')
    monkeypatch.setattr('hanoitourist_crawler.workflows.pipeline.parse', fail)
    with pytest.raises(psycopg.OperationalError):
        retry(storage, batch, target, run)
    assert storage.get_snapshot(run) is None
    assert storage.get_run(run)['parse_status'] == 'pending'
    assert storage.get_run(run)['errors'] == []
    assert storage.batch_tours(batch)[0]['outcome'] == 'pending'
    assert len(storage.load_raw(run)) == 4


def test_daily_parse_database_failure_keeps_existing_diagnostic_behavior(storage, documents, monkeypatch):
    batch, target, run = staged_attempt(storage, documents)
    def fail(*args, **kwargs):
        raise psycopg.OperationalError('database unavailable')
    monkeypatch.setattr('hanoitourist_crawler.workflows.pipeline.parse', fail)
    complete_daily(storage, batch, target['tour_id'], run)
    assert storage.get_run(run)['parse_status'] == 'failed'
    assert storage.batch_tours(batch)[0]['outcome'] == 'failed'


@pytest.mark.parametrize('mode', ['daily', 'retry', 'recovery'])
@pytest.mark.parametrize('has_baseline', [False, True])
def test_identity_rejection_keeps_baseline_and_distinct_bookkeeping(
        storage, documents, capture_history, monkeypatch, mode, has_baseline):
    baseline = capture_history(at=now()-timedelta(days=1))[1] if has_baseline else None
    batch, target, run = staged_attempt(storage, documents)
    before = storage.get_tour(target['tour_id'])
    aliases = storage.tour_aliases(target['tour_id'])
    raw = storage.load_raw(run)
    def reject(*args):
        raise ValueError('identity mismatch')
    monkeypatch.setattr(storage, 'apply_attempt', reject)
    if mode == 'daily':
        result = complete_daily(storage, batch, target['tour_id'], run)
        message = 'Identity publication failed; baseline retained'
    elif mode == 'retry':
        result = retry(storage, batch, target, run)
        message = 'Scoped identity publication rejected; baseline retained'
        assert any(e['step'] == 'identity_scope' for e in
                   storage.get_run(run)['manifest']['diagnostics']['events'])
    else:
        result = complete_recovery(storage, batch, target['tour_id'], run)
        message = 'Abandoned identity publication failed; baseline retained'
    assert result is None
    assert storage.get_tour(target['tour_id']) == {**before, 'requires_refresh': mode != 'recovery'}
    assert storage.tour_aliases(target['tour_id']) == aliases
    assert storage.load_raw(run) == raw
    if baseline:
        assert storage.get_snapshot_by_id(baseline.id) == baseline
    if mode == 'recovery':
        assert storage.get_snapshot(run) is None
    else:
        assert storage.get_snapshot(run).status == 'complete'
    assert storage.batch_tours(batch)[0]['outcome'] == 'failed'
    assert storage.batch_tours(batch)[0]['errors'] == [message]


@pytest.mark.parametrize('capture_status,expected,outcome', [
    ('running', 'partial', 'partial'), ('complete', 'complete', 'failed'),
    ('partial', 'partial', 'partial'), ('failed', 'failed', 'failed')])
@pytest.mark.parametrize('has_baseline', [False, True])
def test_recovery_never_parses_staged_raw(
        storage, documents, capture_history, monkeypatch, capture_status, expected, outcome, has_baseline):
    baseline = capture_history(at=now()-timedelta(days=1))[1] if has_baseline else None
    batch, target, run = staged_attempt(storage, documents)
    storage.update_run(run, capture_status=capture_status)
    before = storage.get_tour(target['tour_id'])
    raw = storage.load_raw(run)
    def forbidden(*args, **kwargs):
        raise AssertionError('Recovery must not acquire or parse')
    monkeypatch.setattr('hanoitourist_crawler.workflows.pipeline.parse_run', forbidden)
    monkeypatch.setattr('hanoitourist_crawler.workflows.pipeline.collect_run', forbidden)
    complete_recovery(storage, batch, target['tour_id'], run)
    assert storage.get_snapshot(run) is None
    assert storage.get_run(run)['capture_status'] == expected
    assert storage.get_run(run)['parse_status'] == 'pending'
    assert storage.batch_tours(batch)[0]['outcome'] == outcome
    assert storage.load_raw(run) == raw
    assert storage.get_tour(target['tour_id']) == {**before, 'requires_refresh': True}
    if baseline:
        assert storage.get_snapshot_by_id(baseline.id) == baseline


def test_retry_rejects_canonical_outside_verified_aliases_without_expanding_selection(storage, documents):
    from copy import deepcopy
    batch, target, run = staged_attempt(storage, documents)
    target['aliases'] = ['https://hanoitourist.vn/another-tour']
    selected = {target['tour_id']: target}
    attempted = {target['tour_id']}
    before = deepcopy(selected), set(attempted)
    result = complete_retry(storage, batch, target, run, attempted=attempted, selected=selected)
    assert result is None
    assert storage.get_snapshot(run).status == 'complete'
    assert storage.get_tour(target['tour_id'])['current_snapshot_id'] is None
    assert storage.batch_tours(batch)[0]['outcome'] == 'failed'
    assert (selected, attempted) == before


def test_stopped_retry_retains_committed_capture_and_baseline(storage, documents, capture_history, monkeypatch):
    from hanoitourist_crawler.workflows.attempts import complete_retry_stopped
    from hanoitourist_crawler.models import json_value
    _, baseline = capture_history()
    batch, target, run = staged_attempt(storage, documents)
    storage.update_run(run, capture_status='running', finished_at=None, errors=['Capture checkpoint'])
    before = storage.get_tour(target['tour_id'])
    snapshot_before = json_value(storage.get_snapshot_by_id(baseline.id))
    raw = storage.load_raw(run)
    finished = now()
    def forbidden(*args, **kwargs):
        raise AssertionError('Stopped retry must not acquire, parse or publish')
    monkeypatch.setattr('hanoitourist_crawler.workflows.pipeline.collect_run', forbidden)
    monkeypatch.setattr('hanoitourist_crawler.workflows.pipeline.parse_run', forbidden)
    monkeypatch.setattr(storage, 'apply_attempt', forbidden)
    assert complete_retry_stopped(storage, batch, target['tour_id'], run, clock=lambda: finished) is None
    stopped = storage.get_run(run)
    assert stopped['capture_status'] == 'failed' and stopped['finished_at'] == finished
    assert stopped['errors'] == ['Capture checkpoint', 'Scoped retry interrupted; checkpoints retained']
    assert stopped['parse_status'] == 'pending'
    assert storage.get_snapshot(run) is None
    assert storage.load_raw(run) == raw
    assert storage.get_tour(target['tour_id']) == {**before, 'requires_refresh': True}
    assert json_value(storage.get_snapshot_by_id(baseline.id)) == snapshot_before
    row = storage.batch_tours(batch)[0]
    assert row['outcome'] == 'failed'
    assert row['errors'] == ['Scoped retry stopped before atomic publication']
    assert row['change_kinds'] == []


@pytest.mark.parametrize('has_baseline', [False, True])
@pytest.mark.parametrize('status,has_raw,has_snapshot', [
    ('running', False, False), ('partial', True, False), ('failed', True, False),
    ('complete', True, False), ('complete', True, True),
])
def test_stopped_retry_keeps_terminal_capture_and_unaccepted_snapshot(
        storage, documents, capture_history, monkeypatch, has_baseline, status, has_raw, has_snapshot):
    from hanoitourist_crawler.workflows.attempts import complete_retry_stopped
    from hanoitourist_crawler.models import json_value
    from hanoitourist_crawler.workflows.pipeline import parse_run
    baseline = capture_history()[1] if has_baseline else None
    batch = storage.create_batch()
    tour = storage.register_tour(DEFAULT_TOUR_URL, 'Tour', batch)
    run = storage.create_run(DEFAULT_TOUR_URL)
    storage.begin_attempt(batch, tour['id'], run, 'retry')
    if has_raw:
        collector(documents)(Config(), storage, run).collect()
    if has_snapshot:
        assert parse_run(storage, run).status == 'complete'
    storage.update_run(run, capture_status=status, errors=['Existing capture diagnostic'])
    before_run = storage.get_run(run)
    before_tour = storage.get_tour(tour['id'])
    raw = storage.load_raw(run)
    snapshot = json_value(storage.get_snapshot(run))
    baseline_before = json_value(storage.get_snapshot_by_id(baseline.id)) if baseline else None
    finished = now()
    def clock():
        assert status == 'running', 'Terminal capture must not consume a new finish time'
        return finished
    def forbidden(*args, **kwargs):
        raise AssertionError('Stopped retry must not acquire, parse or publish')
    monkeypatch.setattr('hanoitourist_crawler.workflows.pipeline.collect_run', forbidden)
    monkeypatch.setattr('hanoitourist_crawler.workflows.pipeline.parse_run', forbidden)
    monkeypatch.setattr(storage, 'apply_attempt', forbidden)
    complete_retry_stopped(storage, batch, tour['id'], run, clock=clock)
    expected = dict(before_run)
    if status == 'running':
        expected.update(capture_status='failed', finished_at=finished,
                        errors=['Existing capture diagnostic', 'Scoped retry interrupted; checkpoints retained'])
    assert storage.get_run(run) == expected
    assert storage.load_raw(run) == raw
    assert json_value(storage.get_snapshot(run)) == snapshot
    assert storage.get_tour(tour['id']) == {**before_tour, 'requires_refresh': True}
    if baseline:
        assert json_value(storage.get_snapshot_by_id(baseline.id)) == baseline_before
    row = storage.batch_tours(batch)[0]
    assert row['outcome'] == 'failed'
    assert row['errors'] == ['Scoped retry stopped before atomic publication']
    assert not row['change_kinds']


def test_stopped_retry_failure_keeps_prior_checkpoint_commit(storage, monkeypatch):
    from hanoitourist_crawler.workflows.attempts import complete_retry_stopped
    batch = storage.create_batch()
    tour = storage.register_tour(DEFAULT_TOUR_URL, 'Tour', batch)
    run = storage.create_run(DEFAULT_TOUR_URL)
    storage.begin_attempt(batch, tour['id'], run, 'retry')
    finished = now()
    def fail(*args, **kwargs):
        raise RuntimeError('Attempt row write unavailable')
    monkeypatch.setattr(storage, 'update_batch_tour', fail)
    with pytest.raises(RuntimeError, match='Attempt row write unavailable'):
        complete_retry_stopped(storage, batch, tour['id'], run, clock=lambda: finished)
    assert storage.get_run(run)['capture_status'] == 'failed'
    assert storage.get_run(run)['finished_at'] == finished
    assert storage.batch_tours(batch)[0]['outcome'] == 'pending'
    assert not storage.get_tour(tour['id'])['requires_refresh']


@pytest.mark.parametrize('diagnostics', [False, True])
def test_retry_sql_failure_rolls_back_snapshot_and_publication(
        storage, documents, capture_history, monkeypatch, diagnostics):
    from hanoitourist_crawler.reporting.diagnostics import SCHEMA
    _, baseline = capture_history(at=now()-timedelta(days=1))
    batch, target, run = staged_attempt(storage, documents)
    if diagnostics:
        manifest = storage.get_run(run)['manifest']
        manifest['diagnostics'] = {'schema': SCHEMA, 'events': [], 'overflow': 0}
        storage.update_run(run, manifest=manifest)
    before_run = storage.get_run(run)
    before_tour = storage.get_tour(target['tour_id'])
    before_rows = storage.batch_tours(batch)
    aliases = storage.tour_aliases(target['tour_id'])
    raw = storage.load_raw(run)
    schema = storage.connection.execute('SELECT current_schema() AS s').fetchone()['s']
    reader = Storage(os.environ['TEST_DATABASE_URL'], schema=schema)
    original = storage.apply_attempt
    sqlstates = []
    def abort_after_publication(*args):
        result = original(*args)
        snapshot = storage.get_snapshot(run)
        assert snapshot.status == 'complete'
        assert storage.get_tour(target['tour_id'])['current_snapshot_id'] == snapshot.id
        assert reader.get_tour(target['tour_id'])['current_snapshot_id'] == baseline.id
        assert reader.get_snapshot(run) is None
        try:
            storage.connection.execute('SELECT 1/0')
        except psycopg.Error as exc:
            sqlstates.append(exc.sqlstate)
            raise
        return result
    monkeypatch.setattr(storage, 'apply_attempt', abort_after_publication)
    try:
        with pytest.raises(psycopg.Error) as raised:
            retry(storage, batch, target, run)
        assert sqlstates == ['22012']  # A real PostgreSQL division-by-zero aborted this transaction.
        assert raised.value.sqlstate in {'22012', '25P02'}
        assert storage.get_run(run) == before_run
        assert storage.get_tour(target['tour_id']) == before_tour
        assert storage.batch_tours(batch) == before_rows
        assert storage.tour_aliases(target['tour_id']) == aliases
        assert reader.get_snapshot(run) is None
        assert reader.get_snapshot_by_id(baseline.id) == baseline
        assert reader.get_tour(target['tour_id'])['current_snapshot_id'] == baseline.id
        assert reader.load_raw(run) == raw
        assert storage.connection.execute('SELECT 1 AS usable').fetchone()['usable'] == 1
    finally:
        reader.close()


def test_recovery_publishes_only_committed_snapshot_without_acquisition_or_parsing(
        storage, documents, capture_history, monkeypatch):
    from hanoitourist_crawler.workflows.pipeline import parse_run
    _, baseline = capture_history(at=now()-timedelta(days=1))
    batch, target, run = staged_attempt(storage, documents)
    snapshot = parse_run(storage, run)
    assert snapshot.status == 'complete'
    before_run = storage.get_run(run)
    raw = storage.load_raw(run)
    assert storage.get_tour(target['tour_id'])['current_snapshot_id'] == baseline.id
    def forbidden(*args, **kwargs):
        raise AssertionError('Committed-snapshot recovery must not acquire or parse')
    monkeypatch.setattr('hanoitourist_crawler.workflows.pipeline.collect_run', forbidden)
    monkeypatch.setattr('hanoitourist_crawler.workflows.pipeline.parse_run', forbidden)
    assert complete_recovery(storage, batch, target['tour_id'], run) == target['tour_id']
    assert storage.get_run(run) == before_run
    assert storage.load_raw(run) == raw
    assert storage.get_snapshot_by_id(snapshot.id) == snapshot
    assert storage.get_snapshot_by_id(baseline.id) == baseline
    tour = storage.get_tour(target['tour_id'])
    assert tour['current_snapshot_id'] == snapshot.id
    assert tour['last_successful_check_at'] == before_run['finished_at']
    assert not tour['requires_refresh']
    assert storage.batch_tours(batch)[0]['outcome'] == 'unchanged'


@pytest.mark.parametrize('has_baseline', [False, True])
def test_recovery_empty_abandoned_capture_is_failed(storage, capture_history, monkeypatch, has_baseline):
    baseline = capture_history(at=now()-timedelta(days=1))[1] if has_baseline else None
    batch = storage.create_batch()
    tour = storage.register_tour(DEFAULT_TOUR_URL, 'Tour', batch)
    run = storage.create_run(DEFAULT_TOUR_URL)
    storage.begin_attempt(batch, tour['id'], run, 'new')
    before = storage.get_tour(tour['id'])
    def forbidden(*args, **kwargs):
        raise AssertionError('Empty recovery must not acquire or parse')
    monkeypatch.setattr('hanoitourist_crawler.workflows.pipeline.collect_run', forbidden)
    monkeypatch.setattr('hanoitourist_crawler.workflows.pipeline.parse_run', forbidden)
    complete_recovery(storage, batch, tour['id'], run)
    assert storage.get_run(run)['capture_status'] == 'failed'
    assert storage.get_run(run)['finished_at'] <= now()
    assert storage.get_run(run)['parse_status'] == 'pending'
    assert storage.load_raw(run) == [] and storage.get_snapshot(run) is None
    assert storage.batch_tours(batch)[0]['outcome'] == 'failed'
    assert storage.get_tour(tour['id']) == {**before, 'requires_refresh': True}
    if baseline:
        assert storage.get_snapshot_by_id(baseline.id) == baseline
