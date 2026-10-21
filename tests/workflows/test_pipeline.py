from dataclasses import replace

import pytest
from hanoitourist_crawler.workflows.pipeline import parse_run, crawl
from hanoitourist_crawler.config import Config, DEFAULT_TOUR_URL
from hanoitourist_crawler.processing.parser import parse

pytestmark = pytest.mark.postgres


def stage(storage, documents, status='complete'):
    run = storage.create_run(DEFAULT_TOUR_URL)
    docs = [replace(d, run_id=run) for d in documents]
    for document in docs:
        storage.append_raw(document)
    snapshot = parse(docs, run)
    storage.update_run(run, capture_status=status, manifest={'actions': {'initial':'captured'},
                       'inventory':snapshot.coverage['discovered']})
    return run


def test_replay_offline_and_versioning(storage, documents, monkeypatch):
    run = stage(storage, documents)
    import httpx
    monkeypatch.setattr(httpx.Client, 'get', lambda *a, **k: (_ for _ in ()).throw(AssertionError('network')))
    first = parse_run(storage, run)
    original_time = storage.get_run(run)['started_at']
    assert parse_run(storage, run).id == first.id
    second = parse_run(storage, run, version='new-parser')
    assert second.id != first.id
    assert storage.get_run(run)['started_at'] == original_time
    assert storage.get_snapshot(run, first.parser_version).id == first.id


def test_parse_failure_preserves_raw(storage, documents, monkeypatch):
    run = stage(storage, documents)
    monkeypatch.setattr('hanoitourist_crawler.workflows.pipeline.parse', lambda *a, **k: (_ for _ in ()).throw(ValueError('broken')))
    with pytest.raises(ValueError):
        parse_run(storage, run)
    assert storage.get_run(run)['parse_status'] == 'failed'
    assert len(storage.load_raw(run)) == 4
    assert storage.get_snapshot(run) is None


def test_partial_capture_replayed_as_partial(storage, documents):
    run = stage(storage, documents, 'partial')
    assert parse_run(storage, run).status == 'partial'
    assert storage.get_run(run)['capture_status'] == 'partial'


def test_db_preflight_before_browser(monkeypatch):
    class Unavailable:
        def preflight(self): raise RuntimeError('DB unavailable')
    class NoBrowser:
        def __init__(self, *args): raise AssertionError('browser must not start')
    with pytest.raises(RuntimeError, match='DB'):
        crawl(Config(), Unavailable(), collector_class=NoBrowser)


@pytest.mark.parametrize('version', ['old-parser', 'retry-parser'])
def test_failed_parse_can_recover_without_erasing_history(storage, documents, monkeypatch, version):
    run = stage(storage, documents)
    raw_before = [(d.id, d.sha256, d.captured_at) for d in storage.load_raw(run)]
    monkeypatch.setattr('hanoitourist_crawler.workflows.pipeline.parse', lambda *a, **k: (_ for _ in ()).throw(ValueError('secret-password')))
    with pytest.raises(ValueError):
        parse_run(storage, run, version=version)
    history = storage.get_run(run)['errors']
    monkeypatch.setattr('hanoitourist_crawler.workflows.pipeline.parse', parse)
    result = parse_run(storage, run, version='retry-parser')
    assert result.status == 'complete' and not result.warnings
    assert storage.get_run(run)['errors'] == history
    assert storage.get_run(run)['parse_status'] == 'complete'
    assert [(d.id, d.sha256, d.captured_at) for d in storage.load_raw(run)] == raw_before
    assert 'secret-password' not in str(history)


def test_collect_run_leaves_parsing_to_completion(storage, documents):
    from hanoitourist_crawler.workflows.pipeline import collect_run
    from tests.workflows.test_incremental import collector
    run = storage.create_run(DEFAULT_TOUR_URL)
    assert collect_run(Config(), storage, run, collector_class=collector(documents)) == run
    assert storage.get_run(run)['capture_status'] == 'complete'
    assert storage.get_run(run)['parse_status'] == 'pending'
    assert storage.get_snapshot(run) is None
    assert len(storage.load_raw(run)) == 4


def test_legacy_capture_still_parses_without_catalogue_publication(storage, documents):
    from hanoitourist_crawler.workflows.pipeline import capture_run
    from tests.workflows.test_incremental import collector
    run = storage.create_run(DEFAULT_TOUR_URL)
    assert capture_run(Config(), storage, run, collector_class=collector(documents)) == run
    assert storage.get_snapshot(run).status == 'complete'
    assert storage.list_tours() == []


def test_legacy_failure_replay_recovers(storage, documents):
    run = stage(storage, documents)
    history = ['Parse failed (ValueError), parser=old-parser']
    storage.update_run(run, errors=history)
    assert parse_run(storage, run).status == 'complete'
    assert storage.get_run(run)['errors'] == history
