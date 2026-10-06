from copy import deepcopy
from dataclasses import replace

import pytest
from bs4 import BeautifulSoup

from hanoitourist_crawler.parser import parse
from hanoitourist_crawler.validation import validate


def run_for(snapshot, **changes):
    result = {'capture_status': 'complete', 'errors': [], 'manifest': {
        'actions': {'initial': 'captured', 'dialog': 'captured'},
        'inventory': deepcopy(snapshot.coverage['discovered'])}}
    result.update(changes)
    return result


def test_complete_and_empty_sections(documents):
    snapshot = parse(documents, documents[0].run_id)
    assert validate(snapshot, documents, run_for(snapshot)).status == 'complete'
    assert snapshot.coverage['price_cells'] == 117


def test_missing_cell_never_complete(documents):
    snapshot = parse(documents, documents[0].run_id)
    run = run_for(snapshot)
    snapshot.departures[0].price_cells.pop()
    assert validate(snapshot, documents, run).status == 'partial'
    assert any('matrix' in w for w in snapshot.warnings)


def test_partial_capture_cannot_be_promoted(documents):
    snapshot = parse(documents, documents[0].run_id)
    assert validate(snapshot, documents, run_for(snapshot, capture_status='partial')).status == 'partial'


def test_missing_day_and_provenance(documents):
    snapshot = parse(documents, documents[0].run_id)
    run = run_for(snapshot)
    snapshot.days.pop()
    snapshot.departures[0].provenance = []
    assert validate(snapshot, documents, run).status == 'partial'
    assert any('Itinerary' in w for w in snapshot.warnings)
    assert any('provenance' in w for w in snapshot.warnings)


def test_unvisited_action(documents):
    snapshot = parse(documents, documents[0].run_id)
    run = run_for(snapshot)
    run['manifest']['actions']['day:4'] = 'discovered'
    assert validate(snapshot, documents, run).status == 'partial'


@pytest.mark.parametrize('href', [None, '', '/unknown',
    'https://example.org/tour/8140/lich/8141/booking',
    '/tour/999/lich/8141/booking', '/unknown?next=/tour/8140/lich/8141/booking'])
def test_unrecognized_schedule_row_is_partial_with_provenance(documents, href):
    doc = documents[0]
    soup = BeautifulSoup(doc.body, 'html.parser')
    link = soup.select_one('#lichkhoihanh a[href*="/lich/8141/"]')
    if href is None:
        link.decompose()
    else:
        link['href'] = href
    changed = replace(doc, body=str(soup).encode())
    snapshot = parse([changed], doc.run_id)
    # Legacy manifest without IDs or row descriptors cannot mask source rows.
    run = {'capture_status': 'complete', 'errors': [], 'manifest': {'actions': {'initial': 'captured'}}}
    assert validate(snapshot, [changed], run).status == 'partial'
    assert len(snapshot.departures) == 8
    records = snapshot.coverage['discovered']['schedule_rows']
    assert len(records) == 9
    unresolved = [r for r in records if r['identity_error']]
    assert len(unresolved) == 1 and unresolved[0]['departure_raw'] == '08/10/2026 12:00'
    assert unresolved[0]['provenance'][0]['raw_document_id'] == changed.id
    assert any('schedule row' in w for w in snapshot.warnings)


def test_legacy_parse_error_does_not_contaminate_success(documents):
    snapshot = parse(documents, documents[0].run_id)
    run = run_for(snapshot, errors=['Parse failed (ValueError), parser=old-parser'])
    assert validate(snapshot, documents, run).status == 'complete'
    assert not snapshot.warnings


@pytest.mark.parametrize('missing', ['mapping', 'observation'])
def test_each_source_row_must_have_normalized_observation(documents, missing):
    snapshot = parse(documents, documents[0].run_id)
    run = run_for(snapshot)
    if missing == 'mapping':
        snapshot.coverage['parsed_schedule_rows'].pop(next(iter(snapshot.coverage['parsed_schedule_rows'])))
    else:
        snapshot.departures.pop()
    assert validate(snapshot, documents, run).status == 'partial'
    assert any('schedule row' in w for w in snapshot.warnings)


def test_expected_row_missing_from_raw_is_partial(documents):
    snapshot = parse(documents, documents[0].run_id)
    run = run_for(snapshot)
    removed = snapshot.coverage['discovered']['schedule_rows'].pop()
    snapshot.coverage['parsed_schedule_rows'].pop(removed['row_key'])
    assert validate(snapshot, documents, run).status == 'partial'
    assert any('discovered schedule row' in w for w in snapshot.warnings)


@pytest.mark.parametrize('capture_status', ['complete', 'partial', 'failed'])
def test_parse_history_never_masks_capture_issues(documents, capture_status):
    from hanoitourist_crawler.errors import parse_failure
    snapshot = parse(documents, documents[0].run_id)
    run = run_for(snapshot, capture_status=capture_status, errors=[parse_failure('old', ValueError())])
    assert validate(snapshot, documents, run).status == ('complete' if capture_status == 'complete' else 'partial')
    assert all('Parse failed' not in w for w in snapshot.warnings)


def test_empty_marker_cannot_mask_unrecognized_rows(documents):
    doc = documents[0]
    changed = replace(doc, body=doc.body.replace(b'/booking', b'/unknown') + b'<div class="view-empty"></div>')
    snapshot = parse([changed], doc.run_id)
    assert validate(snapshot, [changed], run_for(snapshot)).status == 'partial'
    assert len(snapshot.coverage['discovered']['schedule_rows']) == 9
    assert not snapshot.departures


@pytest.mark.parametrize('change', ['rename_class', 'remove_class', 'remove_cell'])
def test_missing_date_is_partial_without_parser_warnings(documents, change):
    doc = documents[0]
    soup = BeautifulSoup(doc.body, 'html.parser')
    cell = soup.select_one('#lichkhoihanh td.ngay-khoi-hanh')
    if change == 'rename_class':
        cell['class'] = ['date-column-new']
    elif change == 'remove_class':
        del cell['class']
    else:
        cell.decompose()
    changed = replace(doc, body=str(soup).encode())
    snapshot = parse([changed], doc.run_id)
    snapshot.warnings.clear()
    # Legacy inventory cannot supply the missing row; discovery must find it itself.
    run = {'capture_status': 'complete', 'errors': [], 'manifest': {'actions': {'initial': 'captured'}}}
    assert validate(snapshot, [changed], run).status == 'partial'
    assert len(snapshot.coverage['discovered']['schedule_rows']) == 9
    assert any('departure date' in w for w in snapshot.warnings)


@pytest.mark.parametrize('marker_on', ['row', 'cell'])
def test_footable_empty_placeholder_does_not_create_schedule(documents, marker_on):
    doc = documents[0]
    soup = BeautifulSoup(doc.body, 'html.parser')
    tbody = soup.select_one('#lichkhoihanh table.footable > tbody')
    tbody.clear()
    row = BeautifulSoup('<tr><td colspan="6">Không có lịch khởi hành</td></tr>', 'html.parser').tr
    (row if marker_on == 'row' else row.td)['class'] = ['footable-empty']
    tbody.append(row)
    soup.select_one('#lichkhoihanh').append(
        BeautifulSoup('<div class="view-empty">Không có lịch khởi hành</div>', 'html.parser').div)
    changed = replace(doc, body=str(soup).encode())
    snapshot = parse([changed], doc.run_id)
    run = {'capture_status': 'complete', 'errors': [], 'manifest': {'actions': {'initial': 'captured'}}}
    assert validate(snapshot, [changed], run).status == 'complete'
    assert snapshot.coverage['discovered']['schedule_rows'] == []
    assert not snapshot.departures and not snapshot.warnings


def test_invalid_normalized_date_is_partial_without_parser_warnings(documents):
    snapshot = parse(documents, documents[0].run_id)
    snapshot.departures[0].departure_at = None
    snapshot.warnings.clear()
    assert validate(snapshot, documents, run_for(snapshot)).status == 'partial'
    assert any('departure date' in w for w in snapshot.warnings)

