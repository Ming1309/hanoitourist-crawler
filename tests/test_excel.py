from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook

from hanoitourist_crawler.excel import export_excel
from hanoitourist_crawler.parser import parse
from hanoitourist_crawler.models import json_value


@pytest.fixture
def excel_storage(documents):
    snapshot = parse(documents, documents[0].run_id)
    snapshot.status = 'complete'
    return SimpleNamespace(snapshot=snapshot, versions=[],
                           get_snapshot=lambda run, version=None: snapshot,
                           get_run=lambda run: {'started_at': documents[0].captured_at})


def values(ws, header):
    column = [c.value for c in ws[4]].index(header) + 1
    return [ws.cell(ri, column).value for ri in range(5, ws.max_row + 1)]


def test_full_excel_no_split(excel_storage, tmp_path):
    s = excel_storage.snapshot
    before = deepcopy(json_value(s))
    path, status = export_excel(excel_storage, s.run_id, tmp_path)
    book = load_workbook(path)
    assert status == 'complete' and path.name == s.parser_version + '.xlsx'
    assert book.sheetnames == ['Danh sách tour', 'Nội dung tour', 'Lịch trình',
                              'Lịch khởi hành', 'Giá chi tiết', 'Ảnh và link']
    assert book['Lịch trình'].max_row == 8  # Four days, one row each.
    assert values(book['Lịch trình'], 'Nội dung ngày đầy đủ') == [d.content_text for d in s.days]
    assert all('Đoạn' not in [c.value for c in ws[4]] for ws in book)
    assert book['Lịch khởi hành'].max_row == 13
    assert book['Giá chi tiết'].max_row == 121
    for ws in book:
        assert ws.freeze_panes and len(ws.tables) == 1
        assert any(d.hidden for d in ws.column_dimensions.values())
        assert all(c.data_type != 'f' for row in ws for c in row)
        assert not any(c.hyperlink and '/booking' in c.hyperlink.target for row in ws for c in row)
    assert values(book['Lịch khởi hành'], 'Khởi hành (VN)')[0] == s.departures[0].departure_at.astimezone(
        ZoneInfo('Asia/Ho_Chi_Minh')).replace(tzinfo=None)
    assert values(book['Giá chi tiết'], 'Số tiền')[0] == int(s.departures[0].price_cells[0].amount)
    assert values(book['Giá chi tiết'], 'Ô chung').count('Có — không nhân giá') == 9
    assert json_value(s) == before


def test_variants_formula_and_missing_values(excel_storage, tmp_path):
    s = excel_storage.snapshot
    day = s.days[0]
    day.content_text = '=HYPERLINK("https://example.org", "test")'
    s.days.append(replace(day, id='day-variant', content_text='Second full day variant',
                          content_html='<a href="/second.pdf">Terms</a>'))
    section = next(x for x in s.sections if x.key == 'dieukien')
    section.content_text = '- Keep bullet text'
    s.sections.append(replace(section, id='section-variant', content_text='Second terms',
                              content_html='<a href="/terms-b.pdf">Terms</a>'))
    s.departures.append(replace(s.departures[0], id='conflict', price_from=Decimal('1.25'),
                               price_cells=[], departure_raw='', departure_at=None, seats=None))
    s.status = 'partial'
    s.warnings = ['Missing departure date']
    path, status = export_excel(excel_storage, s.run_id, tmp_path)
    book = load_workbook(path)
    assert status == 'partial'
    assert set(values(book['Lịch trình'], 'Nội dung ngày đầy đủ')[:2]) == {day.content_text, 'Second full day variant'}
    assert '- Keep bullet text' in values(book['Nội dung tour'], 'Nội dung đầy đủ')
    assert 'Second terms' in values(book['Nội dung tour'], 'Nội dung đầy đủ')
    assert 'https://hanoitourist.vn/second.pdf' in values(book['Ảnh và link'], 'URL')
    assert 'https://hanoitourist.vn/terms-b.pdf' in values(book['Ảnh và link'], 'URL')
    assert book['Lịch khởi hành'].max_row == 14
    assert values(book['Lịch khởi hành'], 'Khởi hành (VN)')[-1] is None
    assert values(book['Lịch khởi hành'], 'Chỗ còn')[-1] is None
    assert values(book['Lịch khởi hành'], 'Giá từ')[-1] == 1.25
    assert values(book['Danh sách tour'], 'Cảnh báo') == ['Missing departure date']
    assert all(c.data_type != 'f' for ws in book for row in ws for c in row)


def test_large_money_preserves_precision(excel_storage, tmp_path):
    s = excel_storage.snapshot
    amount = Decimal('1234567890123456.78')
    s.departures[0].price_cells[0].amount = amount
    path, _ = export_excel(excel_storage, s.run_id, tmp_path)
    assert values(load_workbook(path)['Giá chi tiết'], 'Số tiền')[0] == str(amount)


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
    monkeypatch.setattr('hanoitourist_crawler.excel.os.replace',
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
    assert len(book.worksheets) == 6
    for ws in book.worksheets[1:]:
        assert ws.max_row == 4 and ws.auto_filter.ref
