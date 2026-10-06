import json
from dataclasses import replace

import pytest
from hanoitourist_crawler.export import export_run
from hanoitourist_crawler.pipeline import parse_run
from hanoitourist_crawler.parser import parse
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
    from hanoitourist_crawler.excel import export_excel
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
    monkeypatch.setattr('hanoitourist_crawler.excel.os.replace',
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
    monkeypatch.setattr('hanoitourist_crawler.export.os.replace', lambda *a: (_ for _ in ()).throw(OSError('disk')))
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
    assert status == 'partial' and new.parser_version == PARSER_VERSION == '0.1.4'
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

