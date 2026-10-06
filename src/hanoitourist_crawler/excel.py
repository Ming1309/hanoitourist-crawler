"""Readable six-sheet Excel view of one persisted snapshot; JSON remains canonical."""

import math
import os
import tempfile
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

LABELS = {'diemnoibat': 'Điểm nổi bật', 'baogom': 'Dịch vụ bao gồm',
          'khongbaogom': 'Dịch vụ không bao gồm', 'dieukien': 'Điều kiện',
          'ghichu': 'Ghi chú', 'videos': 'Video', 'lichtrinh': 'Lịch trình',
          'lichkhoihanh': 'Lịch khởi hành'}
KEY_HEADERS = ['ID tour', 'ID snapshot', 'Run ID']
GREEN = '176B55'


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


def _sheet(book, name, note, headers, rows, widths, hidden=(), wrap=(), money=(), dates=(), urls=()):
    if len(rows) + 4 > 1048576:
        raise ValueError('Excel row limit exceeded; use JSON export')
    ws = book.create_sheet(name)
    last = len(headers)
    for ri, value in ((1, name.upper()), (2, note)):
        ws.merge_cells(start_row=ri, start_column=1, end_row=ri, end_column=last)
        ws.cell(ri, 1, _text(value)).font = Font(name='Calibri', size=18 if ri == 1 else 10,
                                              bold=ri == 1, color=GREEN)
        ws.cell(ri, 1).alignment = Alignment(wrap_text=True, vertical='center')
        ws.row_dimensions[ri].height = 30 if ri == 1 else 34
    for ci, label in enumerate(headers, 1):
        cell = ws.cell(4, ci, label)
        cell.font = Font(name='Calibri', bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor=GREEN)
        cell.alignment = Alignment(wrap_text=True, vertical='center')
    ws.row_dimensions[4].height = 34
    for ri, row in enumerate(rows, 5):
        lines = 1
        for ci, value in enumerate(row, 1):
            cell = ws.cell(ri, ci, _text(value))
            if isinstance(value, str):
                cell.data_type = 's'  # Never interpret source text as a formula.
            cell.font = Font(name='Calibri', size=11, color='20352E')
            cell.alignment = Alignment(vertical='top', wrap_text=ci in wrap)
            if ci in money:
                fractional = isinstance(value, Decimal) and value != value.to_integral_value()
                cell.number_format = '#,##0.00' if fractional else '#,##0'
            if ci in dates:
                cell.number_format = 'dd/mm/yyyy hh:mm'
            if ci in urls and isinstance(value, str):
                target = urlsplit(value)
                if target.scheme in {'https', 'http'} and '/booking' not in target.path:
                    cell.hyperlink = value
                    cell.font = Font(name='Calibri', size=11, color='167B8C', underline='single')
            if ci in wrap and value:
                width = max(12, widths[ci - 1] - 2)
                lines = max(lines, sum(max(1, math.ceil(len(line) / width))
                                       for line in str(value).split('\n')))
        ws.row_dimensions[ri].height = min(409, max(30, 16 * lines + 10))
    for ci, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(ci)].width = width
    for ci in hidden:
        ws.column_dimensions[get_column_letter(ci)].hidden = True
    ref = f'A4:{get_column_letter(last)}{4 + len(rows)}'
    if rows:
        table = Table(displayName=f'Data{len(book.worksheets)}', ref=ref)
        table.tableStyleInfo = TableStyleInfo(name='TableStyleMedium4', showRowStripes=True)
        ws.add_table(table)
    else:
        ws.auto_filter.ref = ref
    ws.freeze_panes = 'B5'
    ws.sheet_view.showGridLines = False
    ws.sheet_view.zoomScale = 85
    ws.sheet_properties.tabColor = GREEN
    return ws


def _workbook(snapshot, run):
    s = snapshot
    book = Workbook()
    book.remove(book.active)
    keys = [s.source_tour_id, s.id, s.run_id]
    summary = [[s.title, s.origin_text, s.duration_text, _amount(s.promotional_price),
                s.promotional_price_raw, len({d.source_schedule_id for d in s.departures}),
                s.status, '\n'.join(s.warnings), _local(run['started_at']), s.canonical_url,
                s.review_summary, s.sidebar_text, s.parser_version, *keys]]
    _sheet(book, 'Danh sách tour', 'Một dòng / snapshot. Giá quảng bá không thay giá từng lịch; giờ Việt Nam.',
           ['Tên tour', 'Khởi hành từ', 'Thời lượng', 'Giá quảng bá', 'Giá — nguyên văn',
            'Số lịch', 'Trạng thái', 'Cảnh báo', 'Crawl lúc (VN)', 'URL nguồn',
            'Đánh giá nguồn', 'Thông tin sidebar', 'Parser version', *KEY_HEADERS],
           summary, [46, 20, 18, 19, 24, 12, 15, 55, 23, 42, 48, 65, 15, 18, 38, 38],
           hidden=(14, 15, 16), wrap=(1, 8, 10, 11, 12), money=(4,), dates=(9,), urls=(10,))
    variants = Counter()
    contents = []
    for section in sorted(s.sections, key=lambda x: (x.ordinal, x.id)):
        if section.key in {'lichtrinh', 'lichkhoihanh'}:
            continue  # These have dedicated structured sheets.
        variants[section.key] += 1
        contents.append([s.title, LABELS.get(section.key, section.key), section.title, section.state,
                         variants[section.key], section.content_text, section.id, *keys])
    _sheet(book, 'Nội dung tour', 'Mỗi mục / biến thể một dòng. Nội dung đầy đủ trong một ô; không chia đoạn.',
           ['Tên tour', 'Mục nội dung', 'Tiêu đề nguồn', 'Trạng thái mục', 'Biến thể',
            'Nội dung đầy đủ', 'ID section', *KEY_HEADERS], contents,
           [38, 27, 32, 18, 12, 120, 38, 18, 38, 38], hidden=(7, 8, 9, 10), wrap=(1, 2, 3, 6))
    days = [[s.title, d.ordinal, d.label, d.content_text, ' | '.join(d.transport_labels), d.id, *keys]
            for d in sorted(s.days, key=lambda x: (x.ordinal, x.id))]
    _sheet(book, 'Lịch trình', 'Mỗi ngày / biến thể một dòng. Ô dài giữ toàn bộ nội dung; đọc thêm ở thanh công thức.',
           ['Tên tour', 'Ngày thứ', 'Tiêu đề ngày', 'Nội dung ngày đầy đủ',
            'Phương tiện / nhãn nguồn', 'ID ngày', *KEY_HEADERS], days,
           [38, 12, 52, 120, 34, 38, 18, 38, 38], hidden=(6, 7, 8, 9), wrap=(1, 3, 4, 5))
    departures, prices, shared_rows = [], [], []
    for d in s.departures:
        departures.append([s.title, d.source_schedule_id, _local(d.departure_at), d.departure_raw,
                           d.seats, d.seats_raw, _amount(d.price_from), d.price_from_raw, d.currency,
                           d.features, d.booking_href, _local(d.observed_at), d.id, *keys])
        for c in d.price_cells:
            prices.append([s.title, d.source_schedule_id, _local(d.departure_at), d.departure_raw,
                           c.price_label, c.age_label or 'Ô chung', ' | '.join(c.applicable_labels),
                           _amount(c.amount), c.raw_value, c.currency,
                           'Có — không nhân giá' if c.colspan > 1 else 'Không',
                           c.rowspan, c.colspan, c.row_index, c.column_index, c.id, d.id, *keys])
            if c.colspan > 1:
                shared_rows.append(4 + len(prices))
    _sheet(book, 'Lịch khởi hành', 'Một dòng / observation; giữ xung đột và ngày thiếu. Booking chỉ tham chiếu.',
           ['Tên tour', 'ID lịch', 'Khởi hành (VN)', 'Ngày — nguyên văn', 'Chỗ còn', 'Chỗ — nguyên văn',
            'Giá từ', 'Giá — nguyên văn', 'Tiền tệ', 'Đặc điểm', 'Booking — tham chiếu',
            'Quan sát lúc (VN)', 'ID observation', *KEY_HEADERS], departures,
           [38, 13, 23, 24, 12, 18, 19, 24, 12, 40, 65, 23, 38, 18, 38, 38],
           hidden=(13, 14, 15, 16), wrap=(1, 8, 10, 11), money=(7,), dates=(3, 12))
    ws = _sheet(book, 'Giá chi tiết', 'Một dòng / ô giá. Ô chung tô vàng, không nhân giá; 0đ không suy diễn miễn phí.',
                ['Tên tour', 'ID lịch', 'Khởi hành (VN)', 'Ngày — nguyên văn', 'Loại giá', 'Nhóm tuổi',
                 'Nhóm áp dụng', 'Số tiền', 'Giá — nguyên văn', 'Tiền tệ', 'Ô chung', 'Rowspan', 'Colspan',
                 'Dòng nguồn', 'Cột nguồn', 'ID ô giá', 'ID observation', *KEY_HEADERS], prices,
                [38, 13, 23, 24, 28, 26, 65, 19, 23, 12, 25, 12, 12, 13, 13, 38, 38, 18, 38, 38],
                hidden=tuple(range(12, 21)), wrap=(1, 5, 6, 7, 9, 11), money=(8,), dates=(3,))
    for ri in shared_rows:
        for cell in ws[ri][:11]:
            cell.fill = PatternFill('solid', fgColor='FFF3D6')
        ws.cell(ri, 11).comment = Comment('Một ô nguồn gộp nhiều cột; không nhân số tiền.', 'Đối soát')
    media = [[s.title, m.role, LABELS.get(m.section_key, m.section_key), m.day_ordinal,
              m.caption or m.alt, m.url, m.alt, m.original_url, m.id, *keys] for m in s.media]
    links = defaultdict(set)
    for item in [*s.sections, *s.days]:
        context = LABELS.get(item.key, item.key) if hasattr(item, 'key') else item.label
        for link in BeautifulSoup(item.content_html, 'html.parser').select('a[href]'):
            if link['href']:
                links[(urljoin(s.canonical_url, link['href']), link.get_text(' ', strip=True))].add(context)
    for (url, label), contexts in sorted(links.items()):
        media.append([s.title, 'Booking (tham chiếu)' if '/booking' in urlsplit(url).path else 'Link nội dung',
                      ' | '.join(sorted(contexts)), None, label, url, '', '', '', *keys])
    _sheet(book, 'Ảnh và link', 'Chỉ URL, không tải ảnh. Link trùng nhãn/đích được gom; booking không hyperlink.',
           ['Tên tour', 'Loại', 'Thuộc mục', 'Ngày thứ', 'Mô tả', 'URL', 'Alt ảnh',
            'URL nguyên văn', 'ID media', *KEY_HEADERS], media,
           [38, 25, 32, 12, 45, 85, 45, 55, 38, 18, 38, 38],
           hidden=(8, 9, 10, 11, 12), wrap=(1, 2, 3, 5, 6, 7), urls=(6,))
    book.active = 0
    return book


def export_excel(storage, run_id, output_dir, version=None):
    snapshot = storage.get_snapshot(run_id, version)
    if snapshot is None:
        raise ValueError('No snapshot for Excel export; use JSON report')
    book = _workbook(snapshot, storage.get_run(run_id))
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
