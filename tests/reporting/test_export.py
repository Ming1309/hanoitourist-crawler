import json
from dataclasses import replace

import pytest
from hanoitourist_crawler.reporting.export import export_run
from hanoitourist_crawler.workflows.pipeline import parse_run
from hanoitourist_crawler.processing.parser import parse
from hanoitourist_crawler.config import DEFAULT_TOUR_URL

pytestmark = pytest.mark.postgres


def test_export_from_database(storage, documents, tmp_path):
    run = storage.create_run(DEFAULT_TOUR_URL)
    for document in documents:
        storage.append_raw(replace(document, run_id=run))
    expected = parse(storage.load_raw(run), run)
    storage.update_run(run, capture_status='complete', manifest={'actions': {'initial':'captured'},
                       'inventory': expected.coverage['discovered']})
    snapshot = parse_run(storage, run)
    path, status = export_run(storage, run, tmp_path)
    payload = json.loads(path.read_text())
    assert status == 'complete' and len(payload['snapshot']['departures']) == 9
    assert 'PHÚ QUỐC' in path.read_text() and '\\u' not in path.read_text()
    first = payload['snapshot']['departures'][0]['observations'][0]
    assert isinstance(first['price_from'], str)
    assert first['departure_raw'] == '08/10/2026 12:00'
    assert all('body' not in d for d in payload['raw_manifest'])
    assert payload['run']['started_at'] == storage.get_run(run)['started_at'].isoformat()
    assert export_run(storage, run, tmp_path)[0] == path
    assert json.loads(path.read_text()) == payload
    parse_run(storage, run, version='another')
    second_path, _ = export_run(storage, run, tmp_path)
    assert second_path != path and path.exists()


def test_excel_from_database_preserves_history(storage, documents, tmp_path, monkeypatch):
    from openpyxl import load_workbook
    from hanoitourist_crawler.reporting.excel import export_excel
    from hanoitourist_crawler.models import json_value
    run = storage.create_run(DEFAULT_TOUR_URL)
    for document in documents:
        storage.append_raw(replace(document, run_id=run))
    storage.update_run(run, capture_status='complete', manifest={'actions': {'initial': 'captured'}})
    old = parse_run(storage, run, version='old-parser')
    current = parse_run(storage, run)
    before = (json_value(storage.get_run(run)), json_value(old), json_value(current),
              [(d.id, d.sha256, d.captured_at, d.body) for d in storage.load_raw(run)])
    json_path, _ = export_run(storage, run, tmp_path)
    json_bytes = json_path.read_bytes()
    path, status = export_excel(storage, run, tmp_path, version='old-parser')
    assert path.name == 'old-parser.xlsx' and status == 'complete'
    book = load_workbook(path)
    assert book['Lịch trình'].max_row == 8
    assert book['Giá chi tiết'].max_row == 121
    assert export_excel(storage, run, tmp_path)[0].name == current.parser_version + '.xlsx'
    monkeypatch.setattr('hanoitourist_crawler.reporting.excel.os.replace',
                        lambda *a: (_ for _ in ()).throw(OSError('disk')))
    with pytest.raises(OSError):
        export_excel(storage, run, tmp_path)
    assert before == (json_value(storage.get_run(run)), json_value(storage.get_snapshot(run, 'old-parser')),
                      json_value(storage.get_snapshot(run, current.parser_version)),
                      [(d.id, d.sha256, d.captured_at, d.body) for d in storage.load_raw(run)])
    assert json_path.read_bytes() == json_bytes and path.exists()


def test_failure_report(storage, tmp_path):
    run = storage.create_run(DEFAULT_TOUR_URL)
    storage.update_run(run, capture_status='failed', errors=['robots denied'])
    path, status = export_run(storage, run, tmp_path)
    assert status == 'failed'
    assert json.loads(path.read_text())['snapshot'] is None


def test_export_failure_does_not_delete_snapshot(storage, documents, tmp_path, monkeypatch):
    run = storage.create_run(DEFAULT_TOUR_URL)
    for d in documents: storage.append_raw(replace(d, run_id=run))
    snapshot = parse_run(storage, run)
    monkeypatch.setattr('hanoitourist_crawler.reporting.export.os.replace', lambda *a: (_ for _ in ()).throw(OSError('disk')))
    with pytest.raises(OSError): export_run(storage, run, tmp_path)
    assert storage.get_snapshot(run).id == snapshot.id


def test_export_retains_variants_and_unrecognized_row(storage, documents, tmp_path):
    from bs4 import BeautifulSoup
    from hanoitourist_crawler.models import uid
    run = storage.create_run(DEFAULT_TOUR_URL)
    soup = BeautifulSoup(documents[0].body, 'html.parser')
    soup.select_one('#lichkhoihanh a[href*="/lich/8141/"]').decompose()
    node = soup.select_one('#dieukien')
    node.clear()
    node.append(BeautifulSoup('<a href="/terms-a.pdf">Terms</a>', 'html.parser'))
    first = replace(documents[0], run_id=run, body=str(soup).encode())
    second = replace(first, id=uid(), sequence=2, body=first.body.replace(b'/terms-a.pdf', b'/terms-b.pdf'))
    for doc in [first, second]:
        storage.append_raw(doc)
    storage.update_run(run, capture_status='complete', manifest={'actions': {'initial': 'captured'}})
    snapshot = parse_run(storage, run)
    path, status = export_run(storage, run, tmp_path)
    payload = json.loads(path.read_text())
    assert status == 'partial' and len(payload['snapshot']['departures']) == 8
    assert snapshot.coverage['distinct_schedules'] == 8
    sections = [s for s in payload['snapshot']['sections'] if s['key'] == 'dieukien']
    assert len(sections) == 2 and all(len(s['provenance']) == 1 for s in sections)
    assert '/terms-a.pdf' in path.read_text() and '/terms-b.pdf' in path.read_text()
    unresolved = [r for r in payload['snapshot']['coverage']['discovered']['schedule_rows'] if r['identity_error']]
    assert len(unresolved) == 2 and all(r['provenance'] for r in unresolved)


def test_export_success_with_historical_errors_and_versions(storage, documents, tmp_path):
    from hanoitourist_crawler.errors import parse_failure
    run = storage.create_run(DEFAULT_TOUR_URL)
    for document in documents:
        storage.append_raw(replace(document, run_id=run))
    storage.update_run(run, capture_status='complete', manifest={'actions': {'initial': 'captured'}})
    old = parse_run(storage, run, version='0.1.1')
    old_path, _ = export_run(storage, run, tmp_path, version='0.1.1')
    old_bytes = old_path.read_bytes()
    history = ['Parse failed (ValueError), parser=legacy', parse_failure('old', ValueError())]
    storage.update_run(run, errors=history)
    new = parse_run(storage, run)
    path, status = export_run(storage, run, tmp_path)
    payload = json.loads(path.read_text())
    assert status == 'complete' and payload['snapshot']['warnings'] == []
    assert payload['run']['errors'] == history
    assert path != old_path and old_path.read_bytes() == old_bytes
    assert storage.get_snapshot(run, '0.1.1').id == old.id
    assert parse_run(storage, run).id == new.id


def test_missing_date_export_preserves_row_and_history(storage, documents, tmp_path):
    from bs4 import BeautifulSoup
    from hanoitourist_crawler import PARSER_VERSION
    from hanoitourist_crawler.models import json_value
    run = storage.create_run(DEFAULT_TOUR_URL)
    soup = BeautifulSoup(documents[0].body, 'html.parser')
    soup.select_one('#lichkhoihanh td.ngay-khoi-hanh')['class'] = ['date-column-new']
    doc = replace(documents[0], run_id=run, body=str(soup).encode())
    storage.append_raw(doc)
    storage.update_run(run, capture_status='complete', manifest={'actions': {'initial': 'captured'}})
    old = parse_run(storage, run, version='0.1.2')
    old_path, _ = export_run(storage, run, tmp_path, version='0.1.2')
    old_bytes, old_snapshot = old_path.read_bytes(), json_value(old)
    new = parse_run(storage, run)
    path, status = export_run(storage, run, tmp_path)
    payload = json.loads(path.read_text())
    assert status == 'partial' and new.parser_version == PARSER_VERSION == '0.1.5'
    assert len(payload['snapshot']['departures']) == 9
    observation = next(d for d in payload['snapshot']['departures'] if d['source_schedule_id'] == '8141')['observations'][0]
    assert observation['departure_at'] is None and len(observation['price_cells']) == 13
    record = next(r for r in payload['snapshot']['coverage']['discovered']['schedule_rows'] if r['source_schedule_id'] == '8141')
    assert record['provenance'][0]['raw_document_id'] == doc.id
    assert any('departure date' in w and doc.id in w for w in payload['snapshot']['warnings'])
    assert path != old_path and old_path.read_bytes() == old_bytes
    assert json_value(storage.get_snapshot(run, '0.1.2')) == old_snapshot
    assert storage.load_raw(run)[0].body == doc.body
    assert parse_run(storage, run).id == new.id


def frozen_batch(storage, capture_history):
    capture_history()
    batch = storage.create_batch()
    storage.register_tour(DEFAULT_TOUR_URL, 'Phú Quốc', batch)
    storage.freeze_batch(batch, 'complete')
    return batch


def test_reexport_preserves_first_published_receipt(storage, capture_history, tmp_path):
    from hanoitourist_crawler.reporting.excel import export_batch_excel
    batch = frozen_batch(storage, capture_history)
    paths = export_batch_excel(storage, batch, tmp_path)
    before = storage.get_batch(batch)
    refs = storage.batch_tours(batch)
    original = {p.name:p.read_bytes() for p in paths['full'].parent.iterdir()}
    alternate = export_batch_excel(storage, batch, tmp_path/'alternate')
    assert alternate['full'].exists() and alternate != paths
    assert storage.get_batch(batch) == before and storage.batch_tours(batch) == refs
    assert {p.name:p.read_bytes() for p in paths['full'].parent.iterdir()} == original
    assert export_batch_excel(storage, batch, tmp_path) == paths
    assert storage.get_batch(batch) == before


@pytest.mark.parametrize('wrapper', [False, True])
@pytest.mark.parametrize('failure', ['disk', 'corrupt'])
def test_secondary_export_failure_preserves_primary_receipt(storage, capture_history, tmp_path, monkeypatch, wrapper, failure):
    import shutil
    from hanoitourist_crawler.reporting import excel
    from hanoitourist_crawler.workflows.incremental import _export
    batch = frozen_batch(storage, capture_history)
    paths = excel.export_batch_excel(storage, batch, tmp_path)
    before = storage.get_batch(batch)
    refs = storage.batch_tours(batch)
    original = {p.name:p.read_bytes() for p in paths['full'].parent.iterdir()}
    alternate = tmp_path/'alternate'
    if failure == 'disk':
        monkeypatch.setattr(excel, '_batch_workbook', lambda *a,**k:(_ for _ in ()).throw(OSError('disk')))
    else:
        directory = alternate/'domestic'/batch
        shutil.copytree(paths['full'].parent, directory)
        (directory/'delta.xlsx').write_bytes(b'broken')
        bad_bytes = {p.name:p.read_bytes() for p in directory.iterdir()}
    with pytest.raises(OSError if failure == 'disk' else ValueError):
        (_export if wrapper else excel.export_batch_excel)(storage, batch, alternate)
    assert storage.get_batch(batch) == before and storage.batch_tours(batch) == refs
    assert {p.name:p.read_bytes() for p in paths['full'].parent.iterdir()} == original
    if failure == 'corrupt':
        assert {p.name:p.read_bytes() for p in directory.iterdir()} == bad_bytes
    else:
        assert not (alternate/'domestic'/batch).exists()


def test_stale_acknowledgement_cannot_replace_first_receipt(storage, capture_history, tmp_path):
    from hanoitourist_crawler.reporting.excel import export_batch_excel
    batch = frozen_batch(storage, capture_history)
    export_batch_excel(storage, batch, tmp_path)
    before = storage.get_batch(batch)
    storage.update_batch(batch, export_status='failed', export_errors=['stale failure'])
    assert storage.get_batch(batch) == before
    storage.update_batch(batch, export_status='complete', export_manifest={'directory':'stale receipt'}, export_errors=[])
    assert storage.get_batch(batch) == before


@pytest.mark.parametrize('invalid', ['schema', 'frozen', 'files', 'directory'])
def test_invalid_saved_receipt_fails_without_repair(storage, capture_history, tmp_path, invalid):
    from psycopg.types.json import Jsonb
    from hanoitourist_crawler.reporting.excel import export_batch_excel
    batch = frozen_batch(storage, capture_history)
    paths = export_batch_excel(storage, batch, tmp_path)
    receipt = storage.get_batch(batch)['export_manifest']
    receipt[invalid] = {} if invalid == 'files' else ''
    # Deliberate historical corruption only in this disposable test schema.
    storage.connection.execute('UPDATE domestic_batches SET export_manifest=%s WHERE id=%s', (Jsonb(receipt), batch))
    before = storage.get_batch(batch)
    original = {p.name:p.read_bytes() for p in paths['full'].parent.iterdir()}
    with pytest.raises(ValueError, match='[Rr]eceipt'):
        export_batch_excel(storage, batch, tmp_path/'alternate')
    assert storage.get_batch(batch) == before
    assert not (tmp_path/'alternate'/'domestic'/batch).exists()
    assert {p.name:p.read_bytes() for p in paths['full'].parent.iterdir()} == original


def test_concurrent_exports_preserve_first_atomic_receipt(storage, capture_history, tmp_path, monkeypatch):
    import os
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock
    from hanoitourist_crawler.persistence.storage import Storage
    from hanoitourist_crawler.reporting.excel import export_batch_excel
    batch = frozen_batch(storage, capture_history)
    schema = storage.connection.execute('SELECT current_schema() AS name').fetchone()['name']
    barrier = Barrier(2)
    acknowledgement_lock = Lock()
    receipts = []
    update = Storage.update_batch
    def acknowledge(self, batch_id, **values):
        if values.get('export_status') == 'complete':
            # Both callers have read the same empty receipt before either ack.
            barrier.wait(timeout=20)
            with acknowledgement_lock:
                result = update(self, batch_id, **values)
                receipts.append(self.get_batch(batch_id)['export_manifest'])
                return result
        return update(self, batch_id, **values)
    monkeypatch.setattr(Storage, 'update_batch', acknowledge)
    def export(destination):
        own = Storage(os.environ['TEST_DATABASE_URL'], schema=schema)
        try: return export_batch_excel(own, batch, destination)
        finally: own.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(export, tmp_path/name) for name in ('one','two')]
        outputs = [f.result(timeout=30) for f in futures]
    first = storage.get_batch(batch)
    assert first['export_status'] == 'complete'
    assert len(receipts) == 2 and receipts[0] == receipts[1] == first['export_manifest']
    # Independent export copies can both exist; only the first acknowledgement is canonical.
    assert all(paths['full'].exists() for paths in outputs)
    monkeypatch.setattr(Storage, 'update_batch', update)
    for paths in outputs:
        export_batch_excel(storage, batch, paths['full'].parents[2])
        assert storage.get_batch(batch) == first


def test_batch_second_workbook_failure_never_publishes_half(storage, capture_history, tmp_path, monkeypatch):
    from hanoitourist_crawler.reporting import excel
    batch = frozen_batch(storage, capture_history)
    original = excel._batch_workbook
    def build(*args, **kwargs):
        if not kwargs['delta']: raise OSError('disk')
        return original(*args, **kwargs)
    monkeypatch.setattr(excel, '_batch_workbook', build)
    with pytest.raises(OSError): excel.export_batch_excel(storage, batch, tmp_path)
    assert not (tmp_path/'domestic'/batch).exists()
    assert list((tmp_path/'domestic').iterdir()) == []
    assert storage.get_batch(batch)['export_status'] == 'failed'
    assert storage.list_tours()[0]['current_snapshot_id']


def test_batch_published_before_db_ack_recovers_offline(storage, capture_history, tmp_path, monkeypatch):
    from hanoitourist_crawler.reporting.excel import export_batch_excel
    batch = frozen_batch(storage, capture_history)
    update = storage.update_batch
    def fail_ack(batch_id, **values):
        if values.get('export_status') == 'complete': raise OSError('ack')
        return update(batch_id, **values)
    monkeypatch.setattr(storage, 'update_batch', fail_ack)
    with pytest.raises(OSError): export_batch_excel(storage, batch, tmp_path)
    previous = (tmp_path/'domestic'/batch/'full.xlsx').read_bytes()
    monkeypatch.setattr(storage, 'update_batch', update)
    monkeypatch.setattr('hanoitourist_crawler.reporting.excel._batch_workbook', lambda *a,**k:pytest.fail('must reuse published bundle'))
    paths = export_batch_excel(storage, batch, tmp_path)
    assert paths['full'].read_bytes() == previous and storage.get_batch(batch)['export_status']=='complete'


def test_batch_corrupted_bundle_is_not_overwritten(storage, capture_history, tmp_path):
    from hanoitourist_crawler.reporting.excel import export_batch_excel
    batch = frozen_batch(storage, capture_history)
    paths = export_batch_excel(storage, batch, tmp_path)
    paths['delta'].write_bytes(b'broken')
    previous = paths['full'].read_bytes()
    with pytest.raises(ValueError, match='Damaged'): export_batch_excel(storage, batch, tmp_path)
    assert paths['full'].read_bytes() == previous


@pytest.mark.parametrize('invalid', ['unaccepted_complete', 'wrong_partial', 'accepted_ref', 'missing'])
def test_invalid_frozen_display_never_publishes(storage, capture_history, tmp_path, monkeypatch, invalid):
    from hanoitourist_crawler.reporting.excel import export_batch_excel
    capture_history(status='complete' if invalid == 'accepted_ref' else 'partial')
    batch = storage.create_batch()
    tour = storage.register_tour(DEFAULT_TOUR_URL, 'Tour', batch)
    storage.freeze_batch(batch, 'partial')
    if invalid == 'missing':
        monkeypatch.setattr(storage, 'get_snapshot_by_id', lambda *a: None)
    else:
        _, bad = capture_history(source_id='9900' if invalid == 'wrong_partial' else '8140',
                                 status='partial' if invalid == 'wrong_partial' else 'complete')
        storage.update_batch_tour(batch, tour['id'], display_snapshot_id=bad.id)
    before = storage.batch_tours(batch)
    with pytest.raises(ValueError, match='[Ff]rozen'):
        export_batch_excel(storage, batch, tmp_path)
    assert not (tmp_path/'domestic'/batch).exists()
    assert storage.batch_tours(batch) == before and storage.get_batch(batch)['export_status'] == 'failed'


def test_invalid_published_bundle_is_rejected_without_overwrite(storage, capture_history, tmp_path, monkeypatch):
    from hanoitourist_crawler.reporting import excel
    from hanoitourist_crawler.models import json_value
    capture_history(status='partial')
    batch = storage.create_batch()
    tour = storage.register_tour(DEFAULT_TOUR_URL, 'Tour', batch)
    storage.freeze_batch(batch, 'partial')
    run, bad = capture_history(source_id='9900')
    storage.update_batch_tour(batch, tour['id'], display_snapshot_id=bad.id)
    # Simulate a pre-fix bundle whose hashes match its unsafe frozen refs.
    with monkeypatch.context() as prior:
        prior.setattr(excel, '_display_snapshot', lambda store, row: store.get_snapshot_by_id(row['display_snapshot_id']))
        paths = excel.export_batch_excel(storage, batch, tmp_path)
    before = {p.name: p.read_bytes() for p in paths['full'].parent.iterdir()}
    refs = storage.batch_tours(batch)
    snapshot = json_value(storage.get_snapshot_by_id(bad.id))
    raw = [(d.id, d.body, d.sha256, d.captured_at) for d in storage.load_raw(run)]
    with pytest.raises(ValueError, match='Invalid frozen'):
        excel.export_batch_excel(storage, batch, tmp_path)
    assert {p.name: p.read_bytes() for p in paths['full'].parent.iterdir()} == before
    assert storage.batch_tours(batch) == refs
    assert json_value(storage.get_snapshot_by_id(bad.id)) == snapshot
    assert [(d.id, d.body, d.sha256, d.captured_at) for d in storage.load_raw(run)] == raw


@pytest.mark.parametrize('last_status', ['complete', 'partial', 'failed'])
def test_scoped_publication_reuses_cached_refs_and_preserves_old_bundle(storage, capture_history, tmp_path, last_status):
    from openpyxl import load_workbook
    from hanoitourist_crawler.reporting.excel import export_batch_excel, _local
    urls = ['https://hanoitourist.vn/repair-tour-' + str(i) for i in range(4)]
    old_batch = storage.create_batch()
    tours = [storage.register_tour(url, 'Tour '+str(i), old_batch) for i, url in enumerate(urls)]
    for i in range(2):
        run, _ = capture_history(url=urls[i], source_id=str(9200+i))
        storage.begin_attempt(old_batch, tours[i]['id'], run, 'new')
        storage.apply_attempt(old_batch, tours[i]['id'], run)
    storage.freeze_batch(old_batch, 'partial')
    old_paths = export_batch_excel(storage, old_batch, tmp_path)
    old_refs, old_receipt = storage.batch_tours(old_batch), storage.get_batch(old_batch)
    old_bytes = {p.name:p.read_bytes() for p in old_paths['full'].parent.iterdir()}
    cached = [storage.get_tour(t['id']) for t in tours[:2]]
    batch = storage.create_batch()
    storage.update_batch(batch, stage='details', errors=['Scoped repair; discovery not run'])
    for i in (2, 3):
        run, _ = capture_history(url=urls[i], source_id=str(9200+i), status='complete' if i == 2 else last_status)
        storage.begin_attempt(batch, tours[i]['id'], run, 'new')
        storage.apply_attempt(batch, tours[i]['id'], run)
    storage.freeze_batch(batch, 'partial', ['Scoped repair; discovery not run'])
    paths = export_batch_excel(storage, batch, tmp_path)
    assert storage.get_batch(batch)['discovery_run_id'] is None
    assert not storage.get_batch(batch)['discovery_complete']
    book = load_workbook(paths['full'])
    try:
        assert len(book.sheetnames) == 7
        headers = [c.value for c in book['Danh sách tour'][4]]
        summary = {dict(zip(headers, r))['ID catalogue']:dict(zip(headers, r))
                   for r in book['Danh sách tour'].iter_rows(min_row=5, values_only=True)}
        assert len(summary) == 4
        for i, tour in enumerate(tours):
            state = storage.get_tour(tour['id'])
            row = next(r for r in storage.batch_tours(batch) if r['tour_id'] == tour['id'])
            snap = storage.get_snapshot_by_id(row['display_snapshot_id'])
            assert summary[tour['id']]['ID snapshot'] == snap.id
            assert summary[tour['id']]['Chất lượng dữ liệu'] == snap.status
            checked = summary[tour['id']]['Kiểm tra thành công (VN)']
            if state['last_successful_check_at']:
                assert abs((checked - _local(state['last_successful_check_at'])).total_seconds()) <= 0.001
            else:
                assert checked is None
            for name, expected in [('Lịch trình', len(snap.days)),
                                   ('Lịch khởi hành', len(snap.departures)),
                                   ('Giá chi tiết', sum(len(d.price_cells) for d in snap.departures))]:
                ws = book[name]; cols = [c.value for c in ws[4]]
                child = [dict(zip(cols, r)) for r in ws.iter_rows(min_row=5, values_only=True)
                         if r[cols.index('ID catalogue')] == tour['id']]
                assert len(child) == expected and all(r['ID snapshot'] == snap.id for r in child)
            if i < 2:
                assert state == cached[i] and row['attempt_run_ids'] == [] and row['change_kinds'] == []
            if i == 3 and last_status != 'complete':
                assert state['current_snapshot_id'] is None and state['last_successful_check_at'] is None
                assert row['change_kinds'] == []
    finally:
        book.close()
    assert storage.batch_tours(old_batch) == old_refs and storage.get_batch(old_batch) == old_receipt
    assert {p.name:p.read_bytes() for p in old_paths['full'].parent.iterdir()} == old_bytes


def test_valid_partial_reexport_is_frozen_after_new_batch(storage, capture_history, tmp_path):
    from openpyxl import load_workbook
    from hanoitourist_crawler.reporting.excel import export_batch_excel
    _, partial = capture_history(status='partial')
    batch = storage.create_batch()
    tour = storage.register_tour(DEFAULT_TOUR_URL, 'Tour', batch)
    storage.freeze_batch(batch, 'partial')
    paths = export_batch_excel(storage, batch, tmp_path)
    before = {p.name: p.read_bytes() for p in paths['full'].parent.iterdir()}
    refs = storage.batch_tours(batch)
    later = storage.create_batch()
    run, _ = capture_history(price='7,690,000')
    storage.begin_attempt(later, tour['id'], run, 'retry')
    storage.apply_attempt(later, tour['id'], run)
    storage.freeze_batch(later, 'complete')
    export_batch_excel(storage, later, tmp_path)
    assert export_batch_excel(storage, batch, tmp_path) == paths
    again = export_batch_excel(storage, batch, tmp_path/'offline')
    for key in paths:
        first, second = load_workbook(paths[key]), load_workbook(again[key])
        try:
            assert first.sheetnames == second.sheetnames
            for a, b in zip(first, second):
                assert list(a.values) == list(b.values)
        finally:
            first.close(); second.close()
    assert storage.batch_tours(batch) == refs and refs[0]['display_snapshot_id'] == partial.id
    assert {p.name: p.read_bytes() for p in paths['full'].parent.iterdir()} == before

