"""Excel view of persisted source data; JSON remains canonical.

Only source-data columns, the keys needed to rebuild parent-child
relationships, and the parent's source identifiers projected onto each child
are exported, under their exact PostgreSQL names. Other
ingestion metadata (run/batch ids, parser versions, statuses, warnings,
provenance, timestamps added during capture) is intentionally absent.
"""

import math
import os
import tempfile
import json
import shutil
from hashlib import sha256
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# Explicit allowlists: columns not listed here never reach the workbook.
# Leading keys mirror the PostgreSQL foreign keys so every child row can be
# joined to its parent inside the workbook alone. `source_tour_id` and
# price-cell `source_schedule_id` are projection-only conveniences derived
# from those parents; they are not persisted on the child tables.
COLUMNS = {
    'tour_snapshots': [
        'id', 'source_tour_id',
        'canonical_url', 'title', 'duration_text', 'duration_days',
        'duration_nights', 'origin_text', 'promotional_price_raw', 'promotional_price',
        'promotional_currency', 'seats_raw', 'seats', 'review_summary', 'sidebar_text'],
    # content_html is omitted: source sections regularly exceed Excel's 32767-char
    # cell limit, while content_text carries the readable value. JSON keeps both.
    'tour_sections': [
        'source_tour_id', 'snapshot_id',
        'key', 'ordinal', 'title', 'content_text', 'state'],
    'itinerary_days': [
        'source_tour_id', 'snapshot_id',
        'ordinal', 'label', 'title', 'content_text', 'transport_labels'],
    'departure_observations': [
        'source_tour_id', 'id', 'snapshot_id', 'source_schedule_id',
        'booking_href', 'departure_raw', 'departure_at', 'seats_raw',
        'seats', 'price_from_raw', 'price_from', 'currency', 'features'],
    'price_cells': [
        'source_tour_id', 'source_schedule_id', 'departure_id',
        'row_index', 'column_index', 'price_label', 'age_label', 'raw_value', 'amount',
        'currency', 'rowspan', 'colspan', 'applicable_labels'],
    'tour_media': [
        'source_tour_id', 'snapshot_id',
        'role', 'url', 'original_url', 'alt', 'caption', 'ordinal', 'section_key', 'day_ordinal'],
}

WIDTHS = {
    'tour_snapshots': {
        'id': 38, 'source_tour_id': 16, 'canonical_url': 50, 'title': 45, 'duration_text': 18,
        'duration_days': 12, 'duration_nights': 12, 'origin_text': 22,
        'promotional_price_raw': 20, 'promotional_price': 18, 'promotional_currency': 10,
        'seats_raw': 14, 'seats': 10, 'review_summary': 50, 'sidebar_text': 70},
    'tour_sections': {
        'source_tour_id': 16, 'snapshot_id': 38, 'key': 18, 'ordinal': 10, 'title': 32,
        'content_text': 90, 'state': 12},
    'itinerary_days': {
        'source_tour_id': 16, 'snapshot_id': 38, 'ordinal': 10, 'label': 24, 'title': 45,
        'content_text': 90, 'transport_labels': 34},
    'departure_observations': {
        'source_tour_id': 16, 'id': 38, 'snapshot_id': 38, 'source_schedule_id': 20,
        'booking_href': 55, 'departure_raw': 24, 'departure_at': 22, 'seats_raw': 14,
        'seats': 10, 'price_from_raw': 20, 'price_from': 18, 'currency': 10, 'features': 45},
    'price_cells': {
        'source_tour_id': 16, 'source_schedule_id': 20, 'departure_id': 38,
        'row_index': 11, 'column_index': 11, 'price_label': 28, 'age_label': 24,
        'raw_value': 20, 'amount': 18, 'currency': 10, 'rowspan': 10, 'colspan': 10,
        'applicable_labels': 50},
    'tour_media': {
        'source_tour_id': 16, 'snapshot_id': 38, 'role': 14, 'url': 60, 'original_url': 55,
        'alt': 40, 'caption': 40, 'ordinal': 10, 'section_key': 18, 'day_ordinal': 12},
}

WRAP = {
    'tour_snapshots': {'canonical_url', 'title', 'review_summary', 'sidebar_text'},
    'tour_sections': {'title', 'content_text'},
    'itinerary_days': {'label', 'title', 'content_text', 'transport_labels'},
    'departure_observations': {'booking_href', 'features'},
    'price_cells': {'price_label', 'age_label', 'applicable_labels'},
    'tour_media': {'url', 'original_url', 'alt', 'caption'},
}
MONEY = {
    'tour_snapshots': {'promotional_price'},
    'tour_sections': set(), 'itinerary_days': set(),
    'departure_observations': {'price_from'},
    'price_cells': {'amount'}, 'tour_media': set(),
}
DATES = {'tour_snapshots': set(), 'tour_sections': set(), 'itinerary_days': set(),
         'departure_observations': {'departure_at'}, 'price_cells': set(), 'tour_media': set()}
URLS = {'tour_snapshots': {'canonical_url'}, 'tour_sections': set(), 'itinerary_days': set(),
        'departure_observations': set(), 'price_cells': set(),
        'tour_media': {'url', 'original_url'}}
INTS = {
    'tour_snapshots': {'duration_days', 'duration_nights', 'seats'},
    'tour_sections': {'ordinal'},
    'itinerary_days': {'ordinal'},
    'departure_observations': {'seats'},
    'price_cells': {'row_index', 'column_index', 'rowspan', 'colspan'},
    'tour_media': {'ordinal', 'day_ordinal'},
}

HEADER_FILL = PatternFill('solid', fgColor='FF243447')
HEADER_FONT = Font(name='Calibri', size=11, bold=True, color='FFFFFFFF')
BODY_FONT = Font(name='Calibri', size=11, color='FF1F2937')
BODY_FILL = PatternFill('solid', fgColor='FFFFFFFF')
BORDER = Border(left=Side(style='thin', color='FFCBD5E1'), right=Side(style='thin', color='FFCBD5E1'),
                top=Side(style='thin', color='FFCBD5E1'), bottom=Side(style='thin', color='FFCBD5E1'))

# v1-v3 bundles predate the tour-level projection columns; they stay immutable.
BUNDLE_SCHEMAS = {'v1', 'v2', 'v3', 'v4'}
NEW_BUNDLE_SCHEMA = 'v4'


def _local(value):
    return value.astimezone(ZoneInfo('Asia/Ho_Chi_Minh')).replace(tzinfo=None) if value else None


def _amount(value):
    if value is None:
        return None
    # Excel keeps only 15 significant digits. Preserve larger amounts as text.
    if len(value.normalize().as_tuple().digits) > 15:
        return format(value, '.2f')
    return int(value) if value == value.to_integral_value() else value


def _text(value):
    if isinstance(value, str):
        if len(value.encode('utf-16-le')) // 2 > 32767:
            raise ValueError('Excel cell exceeds 32767 characters; use JSON export')
        if ILLEGAL_CHARACTERS_RE.search(value):
            raise ValueError('Excel cell contains unsupported characters; use JSON export')
    return value


def _sheet(book, name, rows):
    columns = COLUMNS[name]
    if len(rows) + 1 > 1048576:
        raise ValueError('Excel row limit exceeded; use JSON export')
    ws = book.create_sheet(name)
    for ci, header in enumerate(columns, 1):
        cell = ws.cell(1, ci, header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = BORDER
    ws.row_dimensions[1].height = 32
    widths = WIDTHS[name]
    for ri, row in enumerate(rows, 2):
        lines = 1
        for ci, column in enumerate(columns, 1):
            value = row[ci - 1]
            cell = ws.cell(ri, ci, _text(value))
            if isinstance(value, str):
                cell.data_type = 's'  # Never interpret source text as a formula.
            cell.font = BODY_FONT
            cell.fill = BODY_FILL
            cell.border = BORDER
            cell.alignment = Alignment(
                vertical='center', wrap_text=column in WRAP[name],
                horizontal='right' if column in MONEY[name] | DATES[name] | INTS[name] else 'left')
            if column in MONEY[name]:
                fractional = isinstance(value, Decimal) and value != value.to_integral_value()
                cell.number_format = '#,##0.00' if fractional else '#,##0'
            elif column in INTS[name]:
                cell.number_format = '#,##0'
            elif column in DATES[name]:
                cell.number_format = 'dd/mm/yyyy hh:mm'
            if column in URLS[name] and isinstance(value, str):
                target = urlsplit(value)
                if target.scheme in {'https', 'http'} and '/booking' not in target.path:
                    cell.hyperlink = value
                    cell.font = Font(name='Calibri', size=11, color='FF167B8C', underline='single')
            if column in WRAP[name] and value:
                width = max(12, widths[column] - 2)
                lines = max(lines, sum(max(1, math.ceil(len(line) / width))
                                       for line in str(value).split('\n')))
        ws.row_dimensions[ri].height = min(409, max(20, 16 * lines + 10))
    for ci, column in enumerate(columns, 1):
        ws.column_dimensions[get_column_letter(ci)].width = widths[column]
    ws.auto_filter.ref = f'A1:{get_column_letter(len(columns))}{len(rows) + 1}'
    ws.freeze_panes = 'A2'
    return ws


def _project(item, name, **foreign_keys):
    row = []
    for column in COLUMNS[name]:
        value = foreign_keys[column] if column in foreign_keys else getattr(item, column)
        if column in MONEY[name]:
            value = _amount(value)
        elif column in DATES[name]:
            value = _local(value)
        elif isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False)
        row.append(value)
    return row


def _rows(snapshot):
    tour = snapshot.source_tour_id
    return {
        'tour_snapshots': [_project(snapshot, 'tour_snapshots')],
        'tour_sections': [_project(x, 'tour_sections', snapshot_id=snapshot.id, source_tour_id=tour)
                          for x in sorted(snapshot.sections, key=lambda x: (x.ordinal, x.id))],
        'itinerary_days': [_project(x, 'itinerary_days', snapshot_id=snapshot.id, source_tour_id=tour)
                           for x in sorted(snapshot.days, key=lambda x: (x.ordinal, x.id))],
        'departure_observations': [
            _project(x, 'departure_observations', snapshot_id=snapshot.id, source_tour_id=tour)
            for x in snapshot.departures],
        'price_cells': [
            _project(cell, 'price_cells', departure_id=departure.id, source_tour_id=tour,
                     source_schedule_id=departure.source_schedule_id)
            for departure in snapshot.departures for cell in departure.price_cells],
        'tour_media': [_project(x, 'tour_media', snapshot_id=snapshot.id, source_tour_id=tour)
                       for x in sorted(snapshot.media, key=lambda x: (x.ordinal, x.id))],
    }


def export_excel(storage, run_id, output_dir, version=None):
    snapshot = storage.get_snapshot(run_id, version)
    if snapshot is None:
        raise ValueError('No snapshot for Excel export; use JSON report')
    book = _workbook(snapshot)
    directory = Path(output_dir) / run_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (snapshot.parser_version + '.xlsx')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w+b', suffix='.xlsx', dir=directory, delete=False) as handle:
            temporary = handle.name
            book.save(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        book.close()
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return path, snapshot.status


def _workbook(snapshot):
    book = Workbook()
    book.remove(book.active)
    for name, rows in _rows(snapshot).items():
        _sheet(book, name, rows)
    book.active = 0
    return book


def _display_snapshot(storage, row):
    state = row['frozen_state']
    display = row['display_snapshot_id']
    current = state['current_snapshot_id']
    snapshot = storage.get_snapshot_by_id(display) if display else None
    if display and snapshot is None:
        raise ValueError('Frozen batch snapshot missing')
    if current and display != current:
        raise ValueError('Invalid frozen display snapshot; accepted reference differs')
    if snapshot and (snapshot.status != ('complete' if current else 'partial')
                     or (state['source_tour_id'] and snapshot.source_tour_id != state['source_tour_id'])):
        raise ValueError('Invalid frozen display snapshot; identity or quality differs')
    return snapshot


def _batch_workbook(storage, batch, rows, *, delta):
    combined = {name: [] for name in COLUMNS}
    for row in rows:
        if delta and not row['change_kinds']:
            continue
        snapshot = _display_snapshot(storage, row)
        if snapshot is None:
            # Catalogue-only records stay in PostgreSQL; never fabricate source rows.
            continue
        for name, values in _rows(snapshot).items():
            combined[name].extend(values)
    book = Workbook()
    book.remove(book.active)
    for name, values in combined.items():
        _sheet(book, name, values)
    book.active = 0
    return book


def export_batch_excel(storage, batch_id, output_dir):
    from ..models import json_value
    batch = storage.get_batch(batch_id)
    if batch['stage'] != 'frozen':
        raise ValueError('Batch is not frozen for offline export')
    rows = [r for r in storage.batch_tours(batch_id) if r['frozen_state'] is not None]
    rows.sort(key=lambda r: (r['frozen_state']['canonical_url'], r['tour_id']))
    frozen = sha256(json.dumps(json_value({'id': batch_id, 'status': batch['status'],
        'counts': batch['counts'], 'errors': batch['errors'], 'rows': rows}), ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    directory = Path(output_dir) / 'domestic' / batch_id
    directory.parent.mkdir(parents=True, exist_ok=True)
    filenames = ('delta.xlsx', 'full.xlsx')

    def validate_bundle():
        try:
            manifest = json.loads((directory / '.bundle.json').read_text())
            if manifest['schema'] not in BUNDLE_SCHEMAS or manifest['batch_id'] != batch_id or manifest['frozen'] != frozen:
                raise ValueError('Incompatible Excel bundle; use another output-dir')
            for name in filenames:
                if sha256((directory / name).read_bytes()).hexdigest() != manifest['files'][name]:
                    raise ValueError('Damaged Excel bundle; use another output-dir')
            return manifest
        except (OSError, KeyError, json.JSONDecodeError):
            raise ValueError('Incomplete Excel bundle; use another output-dir') from None

    temporary = None
    try:
        for row in rows:
            _display_snapshot(storage, row)
        receipt = batch['export_manifest']
        if receipt:
            files = receipt.get('files') if isinstance(receipt, dict) else None
            if (not isinstance(receipt, dict) or receipt.get('schema') not in BUNDLE_SCHEMAS
                    or receipt.get('batch_id') != batch_id or receipt.get('frozen') != frozen
                    or not isinstance(receipt.get('directory'), str) or not receipt['directory']
                    or not isinstance(files, dict) or set(files) != set(filenames)
                    or any(not isinstance(value, str) or len(value) != 64
                           or any(char not in '0123456789abcdef' for char in value) for value in files.values())):
                raise ValueError('Invalid published Excel receipt; history retained')
        if directory.exists():
            manifest = validate_bundle()
        else:
            temporary = Path(tempfile.mkdtemp(prefix=f'.{batch_id}-', dir=directory.parent))
            manifest = {'schema': NEW_BUNDLE_SCHEMA, 'batch_id': batch_id, 'frozen': frozen, 'files': {}}
            for name, delta in zip(filenames, (True, False)):
                book = _batch_workbook(storage, batch, rows, delta=delta)
                try:
                    with (temporary / name).open('w+b') as handle:
                        book.save(handle)
                        handle.flush()
                        os.fsync(handle.fileno())
                finally:
                    book.close()
                manifest['files'][name] = sha256((temporary / name).read_bytes()).hexdigest()
            with (temporary / '.bundle.json').open('w') as handle:
                json.dump(manifest, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            descriptor = os.open(temporary, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            try:
                os.rename(temporary, directory)
            except OSError:
                if not directory.exists():
                    raise
                manifest = validate_bundle()
            descriptor = os.open(directory.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        storage.update_batch(batch_id, export_status='complete', export_manifest={**manifest, 'directory': str(directory)},
                             export_errors=[])
        return {'delta': directory / 'delta.xlsx', 'full': directory / 'full.xlsx'}
    except Exception as exc:
        storage.update_batch(batch_id, export_status='failed', export_errors=[f'Excel export failed ({type(exc).__name__})'])
        raise
    finally:
        if temporary and temporary.exists():
            shutil.rmtree(temporary)
