import json
import re
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook

from hanoitourist_crawler.reporting import excel
from hanoitourist_crawler.reporting.excel import COLUMNS, export_excel
from hanoitourist_crawler.processing.parser import parse
from hanoitourist_crawler.models import json_value
from hanoitourist_crawler.config import DEFAULT_TOUR_URL

SQL_DIR = Path(__file__).parents[2] / 'src/hanoitourist_crawler/persistence/sql'
OPERATIONAL = {'run_id', 'parser_version', 'parsed_at', 'status', 'coverage', 'warnings',
               'provenance', 'state_key', 'observed_at'}
RELATIONAL = {
    'tour_snapshots': ['id'],
    'tour_sections': ['snapshot_id'],
    'itinerary_days': ['snapshot_id'],
    'departure_observations': ['id', 'snapshot_id'],
    'price_cells': ['departure_id'],
    'tour_media': ['snapshot_id'],
}
# Projection-only identifiers copied from the parent record, not stored on the child table.
DENORMALIZED = {
    'tour_snapshots': [],
    'tour_sections': ['source_tour_id'],
    'itinerary_days': ['source_tour_id'],
    'departure_observations': ['source_tour_id'],
    'price_cells': ['source_tour_id', 'source_schedule_id'],
    'tour_media': ['source_tour_id'],
}
IDENTIFIERS = {
    'tour_snapshots': ['id', 'source_tour_id'],
    'tour_sections': ['source_tour_id', 'snapshot_id'],
    'itinerary_days': ['source_tour_id', 'snapshot_id'],
    'departure_observations': ['source_tour_id', 'id', 'snapshot_id', 'source_schedule_id'],
    'price_cells': ['source_tour_id', 'source_schedule_id', 'departure_id'],
    'tour_media': ['source_tour_id', 'snapshot_id'],
}
SOURCE_COLUMNS = {
    'tour_snapshots': [
        'source_tour_id', 'canonical_url', 'title', 'duration_text', 'duration_days',
        'duration_nights', 'origin_text', 'promotional_price_raw', 'promotional_price',
        'promotional_currency', 'seats_raw', 'seats', 'review_summary', 'sidebar_text'],
    'tour_sections': ['key', 'ordinal', 'title', 'content_text', 'state'],
    'itinerary_days': ['ordinal', 'label', 'title', 'content_text', 'transport_labels'],
    'departure_observations': [
        'source_schedule_id', 'booking_href', 'departure_raw', 'departure_at', 'seats_raw',
        'seats', 'price_from_raw', 'price_from', 'currency', 'features'],
    'price_cells': [
        'row_index', 'column_index', 'price_label', 'age_label', 'raw_value', 'amount',
        'currency', 'rowspan', 'colspan', 'applicable_labels'],
    'tour_media': [
        'role', 'url', 'original_url', 'alt', 'caption', 'ordinal', 'section_key', 'day_ordinal'],
}


@pytest.fixture
def excel_storage(documents):
    snapshot = parse(documents, documents[0].run_id)
    snapshot.status = 'complete'
    return SimpleNamespace(snapshot=snapshot, versions=[],
                           get_snapshot=lambda run, version=None: snapshot,
                           get_run=lambda run: {'started_at': documents[0].captured_at})


def headers(ws):
    return [cell.value for cell in ws[1]]


def values(ws, header):
    column = headers(ws).index(header) + 1
    return [ws.cell(ri, column).value for ri in range(2, ws.max_row + 1)]


def sql_tables():
    text = '\n'.join(path.read_text() for path in SQL_DIR.glob('*.sql'))
    tables = {}
    for match in re.finditer(r'CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);', text, re.S):
        parts, depth, buffer = [], 0, ''
        for char in match[2]:
            depth += char == '('
            depth -= char == ')'
            if char == ',' and depth == 0:
                parts.append(buffer)
                buffer = ''
            else:
                buffer += char
        parts.append(buffer)
        columns = [part.split()[0] for part in parts if part.split()
                   and part.split()[0].upper() not in {'PRIMARY', 'UNIQUE', 'FOREIGN', 'CHECK', 'CONSTRAINT'}]
        tables[match[1]] = columns
    return tables


def batch_row(s, tour_id='t1', changes=('new',), display=True):
    state = {'current_snapshot_id': s.id if display else None,
             'source_tour_id': s.source_tour_id, 'listing_title': s.title,
             'canonical_url': s.canonical_url, 'availability': 'available',
             'availability_reason': '', 'is_listed': True, 'last_successful_check_at': None}
    return {'tour_id': tour_id, 'frozen_state': state,
            'display_snapshot_id': s.id if display else None,
            'change_kinds': list(changes), 'errors': [], 'outcome': 'changed',
            'selection_reason': 'new', 'attempt_run_ids': [s.run_id]}


def test_headers_are_exact_postgresql_columns(excel_storage, tmp_path):
    s = excel_storage.snapshot
    path, _ = export_excel(excel_storage, s.run_id, tmp_path)
    book = load_workbook(path)
    tables = sql_tables()
    try:
        assert list(book.sheetnames) == list(COLUMNS)
        for name, columns in COLUMNS.items():
            assert headers(book[name]) == columns
            assert set(columns) - set(DENORMALIZED[name]) <= set(tables[name])
            assert len(columns) == len(set(columns))
    finally:
        book.close()


def second_snapshot(documents, source_id='9999'):
    url = 'https://hanoitourist.vn/second-tour'
    docs = [replace(d, run_id='run-2',
                    body=d.body.replace(DEFAULT_TOUR_URL.encode(), url.encode())
                            .replace(b'8140', source_id.encode()))
            for d in documents]
    snapshot = parse(docs, 'run-2')
    snapshot.status = 'complete'
    return snapshot


def assert_relationships(book, snapshots):
    ids = values(book['tour_snapshots'], 'id')
    assert ids == [s.id for s in snapshots]
    assert len(ids) == len(set(ids))
    tour_of = {s.id: s.source_tour_id for s in snapshots}
    assert values(book['tour_snapshots'], 'source_tour_id') == [s.source_tour_id for s in snapshots]
    departure_ids = values(book['departure_observations'], 'id')
    assert len(departure_ids) == len(set(departure_ids))
    for name in ('tour_sections', 'itinerary_days', 'departure_observations', 'tour_media'):
        foreign = values(book[name], 'snapshot_id')
        assert None not in foreign and set(foreign) <= set(ids)
        assert all(tour_of[parent] == tour for tour, parent
                   in zip(values(book[name], 'source_tour_id'), foreign))
    dep_tour = dict(zip(departure_ids, (tour_of[parent] for parent
                                       in values(book['departure_observations'], 'snapshot_id'))))
    dep_schedule = dict(zip(departure_ids, values(book['departure_observations'], 'source_schedule_id')))
    for tour, schedule, departure in zip(values(book['price_cells'], 'source_tour_id'),
                                         values(book['price_cells'], 'source_schedule_id'),
                                         values(book['price_cells'], 'departure_id')):
        assert departure in dep_tour
        assert tour == dep_tour[departure] and schedule == dep_schedule[departure]
    for name, expected in [
            ('tour_sections', {s.id: len(s.sections) for s in snapshots}),
            ('itinerary_days', {s.id: len(s.days) for s in snapshots}),
            ('departure_observations', {s.id: len(s.departures) for s in snapshots}),
            ('tour_media', {s.id: len(s.media) for s in snapshots})]:
        counts = {}
        for key in values(book[name], 'snapshot_id'):
            counts[key] = counts.get(key, 0) + 1
        assert counts == {key: count for key, count in expected.items() if count}
    expected = {d.id: len(d.price_cells) for s in snapshots for d in s.departures if d.price_cells}
    counts = {}
    for key in values(book['price_cells'], 'departure_id'):
        counts[key] = counts.get(key, 0) + 1
    assert counts == expected


def test_identifiers_lead_each_worksheet(excel_storage, tmp_path):
    s = excel_storage.snapshot
    path, _ = export_excel(excel_storage, s.run_id, tmp_path)
    book = load_workbook(path)
    try:
        for name, keys in IDENTIFIERS.items():
            assert headers(book[name])[:len(keys)] == keys
            assert not any(dimension.hidden for dimension in book[name].column_dimensions.values())
    finally:
        book.close()


def test_only_identifier_columns_were_added():
    for name, columns in COLUMNS.items():
        assert len(columns) == len(set(columns))
        assert set(columns) == set(SOURCE_COLUMNS[name]) | set(RELATIONAL[name]) | set(DENORMALIZED[name])


def test_single_workbook_relationships_are_closed(excel_storage, tmp_path):
    s = excel_storage.snapshot
    path, _ = export_excel(excel_storage, s.run_id, tmp_path)
    book = load_workbook(path)
    try:
        assert_relationships(book, [s])
    finally:
        book.close()


def test_batch_full_and_delta_workbooks_are_relationally_closed(excel_storage, documents):
    first = excel_storage.snapshot
    second = second_snapshot(documents)
    storage = SimpleNamespace(get_snapshot_by_id=lambda sid: {first.id: first, second.id: second}.get(sid))
    rows = [batch_row(first, 'one', changes=('new',)), batch_row(second, 'two', changes=())]
    full = excel._batch_workbook(storage, {'id': 'batch'}, rows, delta=False)
    delta = excel._batch_workbook(storage, {'id': 'batch'}, rows, delta=True)
    try:
        assert_relationships(full, [first, second])
        assert_relationships(delta, [first])
        expected = {
            'tour_snapshots': 1,
            'tour_sections': len(second.sections),
            'itinerary_days': len(second.days),
            'departure_observations': len(second.departures),
            'price_cells': sum(len(d.price_cells) for d in second.departures),
            'tour_media': len(second.media),
        }
        for name, count in expected.items():
            assert values(full[name], 'source_tour_id').count('9999') == count
        for name in COLUMNS:
            assert set(values(delta[name], 'source_tour_id')) == {first.source_tour_id}
    finally:
        full.close()
        delta.close()


def test_same_tour_multiple_snapshots_stay_distinguishable(excel_storage, documents):
    first = excel_storage.snapshot
    second = second_snapshot(documents, source_id=first.source_tour_id)
    assert second.id != first.id and second.source_tour_id == first.source_tour_id
    storage = SimpleNamespace(get_snapshot_by_id=lambda sid: {first.id: first, second.id: second}.get(sid))
    rows = [batch_row(first, 'one'), batch_row(second, 'two')]
    book = excel._batch_workbook(storage, {'id': 'batch'}, rows, delta=False)
    try:
        assert_relationships(book, [first, second])
        assert set(values(book['tour_sections'], 'snapshot_id')) == {first.id, second.id}
        assert set(values(book['tour_sections'], 'source_tour_id')) == {first.source_tour_id}
    finally:
        book.close()


def test_missing_schedule_identifier_stays_blank(excel_storage, tmp_path):
    s = excel_storage.snapshot
    departure = s.departures[0]
    assert departure.price_cells
    departure.source_schedule_id = ''
    path, _ = export_excel(excel_storage, s.run_id, tmp_path)
    book = load_workbook(path)
    try:
        ws = book['price_cells']
        departure_column = headers(ws).index('departure_id')
        schedule_column = headers(ws).index('source_schedule_id')
        rows = list(ws.iter_rows(min_row=2, values_only=True))
        blank = [row[schedule_column] for row in rows if row[departure_column] == departure.id]
        other = [row[schedule_column] for row in rows if row[departure_column] != departure.id]
        assert len(blank) == len(departure.price_cells)
        assert all(value in (None, '') for value in blank)
        assert other and all(value not in (None, '') for value in other)
    finally:
        book.close()


def test_workbook_contains_only_source_data(excel_storage, tmp_path):
    s = excel_storage.snapshot
    path, _ = export_excel(excel_storage, s.run_id, tmp_path)
    book = load_workbook(path)
    try:
        assert 'Nhật ký tải' not in book.sheetnames
        for ws in book:
            assert not set(headers(ws)) & OPERATIONAL
            assert ws.sheet_state == 'visible'
            assert not any(dimension.hidden for dimension in ws.column_dimensions.values())
            assert all(cell.comment is None for row in ws.iter_rows() for cell in row)
    finally:
        book.close()


def test_six_worksheets_for_single_and_batch_exports(excel_storage, tmp_path):
    s = excel_storage.snapshot
    path, _ = export_excel(excel_storage, s.run_id, tmp_path)
    single = load_workbook(path)
    try:
        assert len(single.worksheets) == 6
    finally:
        single.close()
    storage = SimpleNamespace(get_snapshot_by_id=lambda sid: s if sid == s.id else None)
    for delta in (False, True):
        book = excel._batch_workbook(storage, {'id': 'batch'}, [batch_row(s)], delta=delta)
        try:
            assert list(book.sheetnames) == list(COLUMNS)
            for name in COLUMNS:
                assert headers(book[name]) == COLUMNS[name]
        finally:
            book.close()


def test_cell_borders_are_visible(excel_storage, tmp_path):
    s = excel_storage.snapshot
    path, _ = export_excel(excel_storage, s.run_id, tmp_path)
    book = load_workbook(path)
    try:
        for ws in book:
            for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=len(headers(ws))):
                for cell in row:
                    for side in (cell.border.left, cell.border.right, cell.border.top, cell.border.bottom):
                        assert side.style == 'thin'
                        assert side.color.rgb == 'FFCBD5E1'
    finally:
        book.close()


def test_header_and_data_alignment(excel_storage, tmp_path):
    s = excel_storage.snapshot
    path, _ = export_excel(excel_storage, s.run_id, tmp_path)
    book = load_workbook(path)
    try:
        for ws in book:
            for cell in ws[1]:
                assert cell.alignment.horizontal == 'center'
                assert cell.alignment.vertical == 'center'
            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    assert cell.alignment.vertical == 'center'
    finally:
        book.close()


def test_dynamic_row_height_follows_content(excel_storage):
    s = excel_storage.snapshot
    s.days[0].content_text = s.days[0].content_html = 'Ngắn'
    short = excel._workbook(s)
    try:
        short_height = short['itinerary_days'].row_dimensions[2].height
    finally:
        short.close()
    s.days[0].content_text = s.days[0].content_html = 'Đoạn văn dài: ' * 2000 + '\n' * 20
    long = excel._workbook(s)
    try:
        long_height = long['itinerary_days'].row_dimensions[2].height
        assert long_height > short_height
        assert long_height <= 409
        assert long['itinerary_days'].cell(2, 6).alignment.vertical == 'center'
    finally:
        long.close()


def test_autofilter_and_freeze_panes(excel_storage, tmp_path):
    s = excel_storage.snapshot
    path, _ = export_excel(excel_storage, s.run_id, tmp_path)
    book = load_workbook(path)
    try:
        for ws in book:
            assert ws.freeze_panes == 'A2'
            assert ws.auto_filter.ref == f'A1:{ws.cell(1, len(headers(ws))).column_letter}{ws.max_row}'
    finally:
        book.close()


def test_data_fidelity(excel_storage, tmp_path):
    s = excel_storage.snapshot
    s.days[0].content_text = 'Dòng một\nDòng hai — tiếng Việt đầy đủ'
    path, _ = export_excel(excel_storage, s.run_id, tmp_path)
    book = load_workbook(path)
    try:
        snapshots = book['tour_snapshots']
        assert values(snapshots, 'title') == [s.title]
        assert values(snapshots, 'promotional_price_raw') == [s.promotional_price_raw]
        assert values(snapshots, 'promotional_price') == [int(s.promotional_price)]
        assert values(snapshots, 'canonical_url') == [s.canonical_url]
        assert values(book['itinerary_days'], 'content_text')[0] == s.days[0].content_text
        assert values(book['itinerary_days'], 'transport_labels')[0] == json.dumps(
            s.days[0].transport_labels, ensure_ascii=False)
        assert values(book['departure_observations'], 'departure_at')[0] == s.departures[0].departure_at.astimezone(
            ZoneInfo('Asia/Ho_Chi_Minh')).replace(tzinfo=None)
        cells = book['price_cells']
        assert values(cells, 'raw_value')[0] == s.departures[0].price_cells[0].raw_value
        assert values(cells, 'amount')[0] == int(s.departures[0].price_cells[0].amount)
        assert values(cells, 'age_label').count(None) == sum(
            1 for d in s.departures for c in d.price_cells if c.age_label is None)
    finally:
        book.close()


def test_shared_prices_are_not_duplicated(excel_storage, tmp_path):
    s = excel_storage.snapshot
    path, _ = export_excel(excel_storage, s.run_id, tmp_path)
    book = load_workbook(path)
    try:
        ws = book['price_cells']
        expected = [cell for departure in s.departures for cell in departure.price_cells]
        assert ws.max_row - 1 == len(expected)
        assert values(ws, 'amount') == [excel._amount(c.amount) for c in expected]
        assert values(ws, 'rowspan') == [c.rowspan for c in expected]
        assert values(ws, 'colspan') == [c.colspan for c in expected]
        assert all(cell.comment is None for row in ws.iter_rows() for cell in row)
    finally:
        book.close()


def test_repeated_source_records_are_kept(excel_storage, tmp_path):
    s = excel_storage.snapshot
    day = s.days[0]
    s.days.append(replace(day, id='day-variant', content_text='Second full day variant'))
    section = next(x for x in s.sections if x.key == 'dieukien')
    s.sections.append(replace(section, id='section-variant', content_text='Second terms'))
    s.departures.append(replace(s.departures[0], id='conflict', price_cells=[],
                               departure_raw='', departure_at=None, seats=None))
    path, _ = export_excel(excel_storage, s.run_id, tmp_path)
    book = load_workbook(path)
    try:
        assert values(book['itinerary_days'], 'content_text').count('Second full day variant') == 1
        assert values(book['tour_sections'], 'content_text').count('Second terms') == 1
        assert book['departure_observations'].max_row == 1 + len(s.departures)
        assert book['tour_media'].max_row == 1 + len(s.media)
        assert len([v for v in values(book['tour_sections'], 'key') if v == 'lichkhoihanh']) >= 2
    finally:
        book.close()


def test_variants_and_missing_values(excel_storage, tmp_path):
    s = excel_storage.snapshot
    day = s.days[0]
    day.content_text = '=HYPERLINK("https://example.org", "test")'
    s.days.append(replace(day, id='day-variant', content_text='Second full day variant'))
    s.departures.append(replace(s.departures[0], id='conflict', price_from=Decimal('1.25'),
                               price_cells=[], departure_raw='', departure_at=None, seats=None))
    s.status = 'partial'
    s.warnings = ['Missing departure date']
    path, status = export_excel(excel_storage, s.run_id, tmp_path)
    book = load_workbook(path)
    try:
        assert status == 'partial'
        assert set(values(book['itinerary_days'], 'content_text')[:2]) == {day.content_text, 'Second full day variant'}
        assert book['departure_observations'].max_row == 1 + len(s.departures)
        assert values(book['departure_observations'], 'departure_at')[-1] is None
        assert values(book['departure_observations'], 'seats')[-1] is None
        assert values(book['departure_observations'], 'price_from')[-1] == 1.25
        assert 'warnings' not in headers(book['tour_snapshots'])
        assert all(cell.data_type != 'f' for ws in book for row in ws for cell in row)
    finally:
        book.close()


def test_full_and_delta_selection(excel_storage):
    s = excel_storage.snapshot
    storage = SimpleNamespace(get_snapshot_by_id=lambda sid: s if sid == s.id else None)
    changed = batch_row(s, 'changed', changes=('new',))
    unchanged = batch_row(s, 'unchanged', changes=())
    full = excel._batch_workbook(storage, {'id': 'batch'}, [changed, unchanged], delta=False)
    delta = excel._batch_workbook(storage, {'id': 'batch'}, [changed, unchanged], delta=True)
    try:
        assert full['tour_snapshots'].max_row == 3
        assert delta['tour_snapshots'].max_row == 2
    finally:
        full.close()
        delta.close()


def test_catalogue_only_rows_are_not_fabricated(excel_storage):
    s = excel_storage.snapshot
    storage = SimpleNamespace(get_snapshot_by_id=lambda sid: None)
    missing = batch_row(s, changes=('new',), display=False)
    book = excel._batch_workbook(storage, {'id': 'batch'}, [missing], delta=False)
    try:
        for ws in book:
            assert ws.max_row == 1
    finally:
        book.close()


def test_large_money_preserves_precision(excel_storage, tmp_path):
    s = excel_storage.snapshot
    amount = Decimal('1234567890123456.78')
    s.departures[0].price_cells[0].amount = amount
    path, _ = export_excel(excel_storage, s.run_id, tmp_path)
    assert values(load_workbook(path)['price_cells'], 'amount')[0] == str(amount)


@pytest.mark.parametrize('content', ['x' * 32768, 'bad\x01content'])
def test_unsupported_content_never_truncated(excel_storage, tmp_path, content):
    s = excel_storage.snapshot
    path, _ = export_excel(excel_storage, s.run_id, tmp_path)
    previous = path.read_bytes()
    s.days[0].content_text = content
    with pytest.raises(ValueError, match='Excel cell'):
        export_excel(excel_storage, s.run_id, tmp_path)
    assert path.read_bytes() == previous
    assert list(path.parent.iterdir()) == [path]


def test_atomic_failure_keeps_previous_excel(excel_storage, tmp_path, monkeypatch):
    s = excel_storage.snapshot
    path, _ = export_excel(excel_storage, s.run_id, tmp_path)
    previous = path.read_bytes()
    monkeypatch.setattr('hanoitourist_crawler.reporting.excel.os.replace',
                        lambda *a: (_ for _ in ()).throw(OSError('disk')))
    with pytest.raises(OSError):
        export_excel(excel_storage, s.run_id, tmp_path)
    assert path.read_bytes() == previous
    assert list(path.parent.iterdir()) == [path]


def test_missing_snapshot_requires_json(tmp_path):
    storage = SimpleNamespace(get_snapshot=lambda *a: None)
    with pytest.raises(ValueError, match='JSON report'):
        export_excel(storage, 'run', tmp_path, 'missing-version')
    assert not list(tmp_path.iterdir())


def test_empty_sheets(excel_storage, tmp_path):
    s = excel_storage.snapshot
    s.sections.clear()
    s.days.clear()
    s.media.clear()
    s.departures.clear()
    path, _ = export_excel(excel_storage, s.run_id, tmp_path)
    book = load_workbook(path)
    try:
        assert len(book.worksheets) == 6
        for ws in book.worksheets[1:]:
            assert ws.max_row == 1 and ws.auto_filter.ref
    finally:
        book.close()


def test_export_does_not_mutate_snapshot(excel_storage, tmp_path):
    s = excel_storage.snapshot
    before = json_value(s)
    export_excel(excel_storage, s.run_id, tmp_path)
    assert json_value(s) == before
