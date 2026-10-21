import os
from datetime import timedelta

import pytest

from hanoitourist_crawler.config import DEFAULT_TOUR_URL
from hanoitourist_crawler.models import RawDocument, now
from hanoitourist_crawler.persistence.storage import Storage

pytestmark = pytest.mark.postgres


def register(storage):
    batch = storage.create_batch()
    tour = storage.register_tour(DEFAULT_TOUR_URL, 'Phú Quốc', batch)
    return batch, tour


def test_seed_uses_capture_time_not_replay(storage, capture_history):
    at = now() - timedelta(days=2)
    run, snapshot = capture_history(at=at)
    batch, tour = register(storage)
    assert tour['current_snapshot_id'] == snapshot.id
    assert tour['last_successful_check_at'] == at
    repeated = storage.register_tour(DEFAULT_TOUR_URL, 'New card title', batch)
    assert repeated['id'] == tour['id'] and repeated['last_successful_check_at'] == at
    assert storage.batch_tours(batch)[0]['change_kinds'] == []


def test_partial_never_replaces_complete_seed(storage, capture_history):
    _, complete = capture_history(at=now() - timedelta(days=2))
    capture_history(status='partial')
    _, tour = register(storage)
    assert tour['current_snapshot_id'] == complete.id


def test_schema_upgrade_preserves_history(storage, capture_history):
    run, snapshot = capture_history()
    raw = [(d.id, d.body, d.sha256, d.captured_at) for d in storage.load_raw(run)]
    storage.connection.execute('DROP TABLE scheduler_state,domestic_batch_tours,domestic_tour_urls,domestic_tours,domestic_batches')
    storage.init_schema()
    storage.init_schema()
    assert storage.get_snapshot_by_id(snapshot.id).id == snapshot.id
    assert raw == [(d.id, d.body, d.sha256, d.captured_at) for d in storage.load_raw(run)]


def test_session_lock_survives_commits(storage):
    schema = storage.connection.execute('SELECT current_schema() AS name').fetchone()['name']
    other = Storage(os.environ['TEST_DATABASE_URL'], schema=schema)
    try:
        with storage.crawl_lock() as locked:
            assert locked
            storage.create_batch()
            with other.crawl_lock() as second:
                assert not second
        with other.crawl_lock() as locked:
            assert locked
    finally:
        other.close()


@pytest.mark.parametrize('status,role,gone', [(404,'tour',True),(410,'tour',True),(404,'schedule',False),(403,'tour',False),(503,'tour',False)])
def test_unavailable_requires_primary_evidence(storage, status, role, gone):
    batch, tour = register(storage)
    run = storage.create_run(DEFAULT_TOUR_URL)
    doc = RawDocument(run, 1, 'http_document', role, 'initial', DEFAULT_TOUR_URL, b'error', http_status=status)
    storage.append_raw(doc)
    storage.update_run(run, capture_status='failed', finished_at=now(), manifest={
        'navigation': [{'url': DEFAULT_TOUR_URL, 'status': status, 'raw_document_id': doc.id,
                        'owned_primary': True, 'terminal': True}]})
    storage.begin_attempt(batch, tour['id'], run, 'new')
    storage.apply_attempt(batch, tour['id'], run)
    state = storage.get_tour(tour['id'])
    assert (state['availability'] == 'unavailable') is gone
    assert state['last_successful_check_at'] is None
    frozen = storage.freeze_batch(batch, 'complete')
    assert frozen['counts'].get('unavailable', 0) == (1 if gone else 0)


def test_frozen_refs_do_not_follow_new_capture(storage, capture_history):
    _, first = capture_history()
    batch, tour = register(storage)
    storage.freeze_batch(batch, 'complete')
    before = storage.batch_tours(batch)
    _, second = capture_history(price='7,690,000')
    assert first.id != second.id
    storage._update_tour(tour['id'], current_snapshot_id=second.id)
    assert storage.batch_tours(batch) == before
    assert before[0]['display_snapshot_id'] == first.id


def test_identity_mismatch_does_not_publish_or_rebind(storage,capture_history):
    _,old=capture_history()
    batch,tour=register(storage)
    run,wrong=capture_history(source_id='9900')
    storage.begin_attempt(batch,tour['id'],run,'due')
    with pytest.raises(ValueError,match='identity changed'): storage.apply_attempt(batch,tour['id'],run)
    current=storage.get_tour(tour['id'])
    assert current['source_tour_id']=='8140' and current['current_snapshot_id']==old.id
    assert current['last_successful_check_at']==tour['last_successful_check_at']
    assert storage.get_snapshot_by_id(wrong.id).id==wrong.id


def test_verified_renamed_alias_merges_without_false_new(storage,capture_history):
    capture_history()
    batch,old=register(storage)
    alias='https://hanoitourist.vn/renamed-tour'
    candidate=storage.register_tour(alias,'Renamed',batch)
    run,snapshot=capture_history(url=alias)
    storage.begin_attempt(batch,candidate['id'],run,'new')
    target=storage.apply_attempt(batch,candidate['id'],run)
    assert target==old['id'] and len(storage.list_tours())==1
    assert storage.get_tour(candidate['id'])['superseded_by_id']==old['id']
    row=next(r for r in storage.batch_tours(batch) if r['tour_id']==target)
    assert 'new' not in row['change_kinds'] and row['previous_snapshot_id']==old['current_snapshot_id']
    assert storage.get_tour(target)['current_snapshot_id']==snapshot.id


@pytest.mark.parametrize('invalid', ['missing_flags', 'unowned', 'redirect', 'string_flag', 'numeric_flag',
                                     'wrong_run', 'kind', 'role', 'method', 'status', 'url', 'last_entry'])
def test_invalid_navigation_evidence_keeps_baseline(storage, capture_history, invalid):
    _, old = capture_history()
    batch, tour = register(storage)
    run = storage.create_run(DEFAULT_TOUR_URL)
    doc = RawDocument(run, 1, 'http_document', 'tour', 'initial', DEFAULT_TOUR_URL, b'error', http_status=404)
    if invalid == 'wrong_run': doc.run_id = storage.create_run(DEFAULT_TOUR_URL)
    if invalid == 'kind': doc.kind = 'ajax'
    if invalid == 'role': doc.role = 'listing'
    if invalid == 'method': doc.method = 'POST'
    if invalid == 'status': doc.http_status = 410
    storage.append_raw(doc)
    entry = {'url': DEFAULT_TOUR_URL, 'status': 404, 'raw_document_id': doc.id,
             'owned_primary': True, 'terminal': True}
    if invalid == 'missing_flags':
        del entry['owned_primary']; del entry['terminal']
    if invalid == 'unowned': entry['owned_primary'] = False
    if invalid == 'redirect': entry['terminal'] = False
    if invalid == 'string_flag': entry['owned_primary'] = 'true'
    if invalid == 'numeric_flag': entry['terminal'] = 1
    if invalid == 'url': entry['url'] = 'https://hanoitourist.vn/another-tour'
    navigation = [entry, {}] if invalid == 'last_entry' else [entry]
    storage.update_run(run, capture_status='failed', finished_at=now(), manifest={'navigation': navigation})
    storage.begin_attempt(batch, tour['id'], run, 'missing_listing')
    storage.apply_attempt(batch, tour['id'], run)
    current = storage.get_tour(tour['id'])
    assert current['availability'] == 'available' and current['current_snapshot_id'] == old.id
    assert current['last_successful_check_at'] == tour['last_successful_check_at']
    row = storage.batch_tours(batch)[0]
    assert row['outcome'] == 'failed' and row['change_kinds'] == [] and current['requires_refresh']


def test_partial_display_is_scoped_and_ordered_by_capture(storage, capture_history):
    from hanoitourist_crawler.workflows.pipeline import parse_run
    old_run, _ = capture_history(status='partial', at=now()-timedelta(days=8))
    batch, tour = register(storage)
    _, recent = capture_history(status='partial', at=now()-timedelta(days=2))
    bad_run, _ = capture_history(status='partial', source_id='9900')
    capture_history(status='partial', url='https://hanoitourist.vn/unrelated-tour')
    parse_run(storage, old_run, version='later-replay')
    storage._update_tour(tour['id'], latest_attempt_run_id=bad_run)
    storage.freeze_batch(batch, 'partial')
    row = storage.batch_tours(batch)[0]
    assert row['display_snapshot_id'] == recent.id
    assert row['previous_snapshot_id'] is None and row['frozen_state']['current_snapshot_id'] is None
    assert row['frozen_state']['last_successful_check_at'] is None
    before = storage.batch_tours(batch)
    capture_history(status='partial')
    storage.freeze_batch(batch, 'complete')
    assert storage.batch_tours(batch) == before


def test_conflicting_complete_same_source_is_not_partial_fallback(storage, capture_history):
    _, partial = capture_history(status='partial')
    batch, tour = register(storage)
    other = 'https://hanoitourist.vn/another-tour'
    storage.register_tour(other, 'Another', batch)
    run, rejected = capture_history(url=other)
    storage.begin_attempt(batch, tour['id'], run, 'retry')
    with pytest.raises(ValueError, match='Conflicting canonical'):
        storage.apply_attempt(batch, tour['id'], run)
    storage.freeze_batch(batch, 'partial')
    row = next(r for r in storage.batch_tours(batch) if r['tour_id'] == tour['id'])
    assert row['display_snapshot_id'] == partial.id and rejected.status == 'complete'
    assert row['frozen_state']['current_snapshot_id'] is None


def test_unknown_identity_partial_display_does_not_bind(storage, capture_history):
    batch, tour = register(storage)
    run, partial = capture_history(status='partial')
    storage.begin_attempt(batch, tour['id'], run, 'new')
    storage.apply_attempt(batch, tour['id'], run)
    storage.freeze_batch(batch, 'partial')
    row = storage.batch_tours(batch)[0]
    assert row['display_snapshot_id'] == partial.id and row['frozen_state']['source_tour_id'] is None
    assert row['frozen_state']['current_snapshot_id'] is None and row['change_kinds'] == []
    next_batch = storage.create_batch()
    run, complete = capture_history()
    storage.begin_attempt(next_batch, tour['id'], run, 'retry')
    storage.apply_attempt(next_batch, tour['id'], run)
    assert storage.batch_tours(next_batch)[0]['change_kinds'] == ['new']
    assert storage.get_tour(tour['id'])['current_snapshot_id'] == complete.id


def test_no_compatible_partial_stays_pending(storage, capture_history):
    batch, tour = register(storage)
    storage._update_tour(tour['id'], source_tour_id='8140')
    run, _ = capture_history(status='partial', source_id='9900')
    storage.begin_attempt(batch, tour['id'], run, 'new')
    storage.apply_attempt(batch, tour['id'], run)
    storage.freeze_batch(batch, 'partial')
    row = storage.batch_tours(batch)[0]
    assert row['display_snapshot_id'] is None and row['frozen_state']['availability'] == 'pending'


def test_accepted_display_survives_identity_rejection(storage, capture_history):
    _, accepted = capture_history()
    batch, tour = register(storage)
    run, _ = capture_history(source_id='9900')
    storage.begin_attempt(batch, tour['id'], run, 'due')
    with pytest.raises(ValueError): storage.apply_attempt(batch, tour['id'], run)
    storage.freeze_batch(batch, 'partial')
    assert storage.batch_tours(batch)[0]['display_snapshot_id'] == accepted.id
