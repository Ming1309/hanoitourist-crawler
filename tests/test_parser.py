import json
from copy import deepcopy
from dataclasses import replace

import pytest
from bs4 import BeautifulSoup

from hanoitourist_crawler.models import json_value
from hanoitourist_crawler.parser import parse
from conftest import FIXTURES


def test_full_fixture(documents):
    expected = json.loads((FIXTURES / 'expected_snapshot.json').read_text())
    snapshot = parse(documents, documents[0].run_id)
    for key in ('title', 'source_tour_id', 'duration_days', 'duration_nights', 'origin_text'):
        assert getattr(snapshot, key) == expected[key]
    assert str(snapshot.promotional_price) == expected['promotional_price']
    assert len(snapshot.days) == expected['days']
    assert len({m.url for m in snapshot.media if m.role == 'gallery'}) == expected['gallery_images']
    actual = sorted([[d.source_schedule_id, d.departure_raw, str(d.price_from)] for d in snapshot.departures])
    assert actual == sorted(expected['departures'])
    for departure in snapshot.departures:
        assert len(departure.price_cells) == expected['price_cells_per_departure']
        shared = [c for c in departure.price_cells if c.colspan == 4]
        assert len(shared) == 1 and shared[0].age_label is None and shared[0].amount == 0
        assert len(shared[0].applicable_labels) == 4
        assert all(c.provenance for c in departure.price_cells)
    assert any(s.key == 'dieukien' and s.state == 'empty' for s in snapshot.sections)
    assert 'VinWonders' in next(s.content_text for s in snapshot.sections if s.key == 'khongbaogom')
    assert all('retail' not in m.url for m in snapshot.media)


def test_conflicting_price_retained(documents):
    changed = replace(documents[0], id='different-document', sequence=99,
                      body=documents[0].body.replace(b'7,680,000', b'7,690,000'))
    result = parse([documents[0], changed], documents[0].run_id)
    assert len([d for d in result.departures if d.source_schedule_id == '8141']) == 2


def test_unknown_section_and_missing_date(documents):
    original = documents[0]
    body = original.body.decode().replace('<div id="diemnoibat"', '<div id="extra"><h3>Extra</h3><p>New content</p></div><div id="diemnoibat"')
    body = body.replace('08/10/2026 12:00', 'invalid date')
    result = parse([replace(original, body=body.encode())], original.run_id)
    assert any(s.key == 'extra' and 'New content' in s.content_text for s in result.sections)
    assert any('Unparsed departure' in w for w in result.warnings)


def test_parser_does_not_need_browser_or_network(documents, monkeypatch):
    import httpx
    monkeypatch.setattr(httpx.Client, 'get', lambda *a, **k: (_ for _ in ()).throw(AssertionError('network')))
    a = parse([documents[0]], documents[0].run_id)
    assert a.title and len(a.departures) == 9


def test_all_content_blocks_and_unknown_tag_preserved(documents):
    from bs4 import BeautifulSoup
    doc = documents[0]
    soup = BeautifulSoup(doc.body, 'html.parser')
    added = BeautifulSoup('<div class="view-content">Second media description</div>', 'html.parser')
    soup.select_one('#videos').append(added)
    extra = BeautifulSoup('<section><h3>New policy</h3><p>Do not lose this</p></section>', 'html.parser')
    soup.select_one('.field--name-field-tour-content-markup').append(extra)
    result = parse([replace(doc, body=str(soup).encode())], doc.run_id)
    assert 'Second media description' in next(s.content_text for s in result.sections if s.key == 'videos')
    assert any('Do not lose this' in s.content_text for s in result.sections)
    assert any('Unknown tour section' in w for w in result.warnings)


def test_rowspan_and_new_price_label(documents):
    from bs4 import BeautifulSoup
    doc = documents[0]
    soup = BeautifulSoup(doc.body, 'html.parser')
    table = soup.select_one('#lichkhoihanh .table-giave table')
    price_rows = table.select(':scope > tbody > tr')
    price_rows[1].select_one('th').string = 'Giá mới'
    price_rows[1].select_one('td')['rowspan'] = '2'
    price_rows[2].select_one('td').decompose()
    result = parse([replace(doc, body=str(soup).encode())], doc.run_id)
    departure = next(d for d in result.departures if d.source_schedule_id == '8141')
    assert len(departure.price_cells) == 12
    assert departure.price_cells[0].rowspan == 2 and departure.price_cells[0].price_label == 'Giá mới'
    surcharge = next(c for c in departure.price_cells if c.price_label == 'Phụ thu')
    assert surcharge.column_index == 1 and surcharge.age_label == 'Trẻ em (5-11 tuổi)'


def test_missing_section_seats_zero_and_contact_price(documents):
    from bs4 import BeautifulSoup
    doc = documents[0]
    soup = BeautifulSoup(doc.body, 'html.parser')
    soup.select_one('#dieukien').decompose()
    soup.select_one('#lichkhoihanh td.cho-con-lai').string = '0'
    soup.select_one('#lichkhoihanh td.gia-tu').string = 'Liên hệ'
    result = parse([replace(doc, body=str(soup).encode())], doc.run_id)
    departure = next(d for d in result.departures if d.source_schedule_id == '8141')
    assert departure.seats == 0 and departure.price_from is None and departure.price_from_raw == 'Liên hệ'
    assert any(s.key == 'dieukien' and s.state == 'missing' for s in result.sections)


@pytest.mark.parametrize('href', [
    '/tour/8140/lich/8141/booking/',
    '/tour/8140/lich/8141/booking?ref=page',
    '/tour/8140/lich/8141/booking#prices',
    'https://hanoitourist.vn/tour/8140/lich/8141/booking/?ref=page#prices',
    'tour/8140/lich/8141/booking',
])
def test_booking_url_variants_keep_all_departures(documents, href):
    doc = documents[0]
    soup = BeautifulSoup(doc.body, 'html.parser')
    soup.select_one('#lichkhoihanh a[href*="/lich/8141/"]')['href'] = href
    changed = replace(doc, body=str(soup).encode())
    result = parse([changed], doc.run_id)
    assert len(result.departures) == 9
    assert sum(len(d.price_cells) for d in result.departures) == 117
    assert next(d for d in result.departures if d.source_schedule_id == '8141').booking_href.endswith(href.lstrip('/'))


@pytest.mark.parametrize('target', ['dieukien', 'extra', 'day'])
@pytest.mark.parametrize('second_url', ['/terms-b.pdf', '/terms-a.pdf?v=2', '/terms-a.pdf#new'])
def test_content_link_variants_are_not_text_deduplicated(documents, target, second_url):
    doc = documents[0]
    soup = BeautifulSoup(doc.body, 'html.parser')
    if target == 'extra':
        soup.select_one('.field--name-field-tour-content-markup').append(
            BeautifulSoup('<section id="extra"><p></p></section>', 'html.parser'))
    node = soup.select_one('#lichtrinh .accordion-body' if target == 'day' else '#' + target)
    node.clear()
    node.append(BeautifulSoup('<a href="/terms-a.pdf">Điều khoản tour</a>', 'html.parser'))
    first = replace(doc, body=str(soup).encode())
    second = replace(first, id='second-source', sequence=99,
                     body=first.body.replace(b'/terms-a.pdf', second_url.encode()))
    result = parse([first, second], doc.run_id)
    items = [d for d in result.days if d.ordinal == 1] if target == 'day' else [s for s in result.sections if s.key == target]
    assert len(items) == 2
    assert {BeautifulSoup(i.content_html, 'html.parser').select_one('a')['href'] for i in items} == {'/terms-a.pdf', second_url}
    assert [{p['raw_document_id'] for p in i.provenance} for i in items] == [{first.id}, {second.id}]
    assert any('link targets' in w for w in result.warnings)


@pytest.mark.parametrize('changed', [False, True])
def test_image_loading_classes_do_not_hide_changed_urls(documents, changed):
    doc = documents[0]
    soup = BeautifulSoup(doc.body, 'html.parser')
    body = soup.select_one('#lichtrinh .accordion-body')
    body.clear()
    body.append(BeautifulSoup('<img class="lazyload icon" src="/one.png" alt="Plane">', 'html.parser'))
    first = replace(doc, body=str(soup).encode())
    image = body.select_one('img')
    image['class'] = ['ls-is-cached', 'lazyloaded', 'icon']
    if changed:
        image['src'] = '/two.png'
    second = replace(first, id='second-image-source', body=str(soup).encode())
    snapshot = parse([first, second], doc.run_id)
    days = [d for d in snapshot.days if d.ordinal == 1]
    assert len(days) == (2 if changed else 1)
    assert 'lazyload' in days[0].content_html
    assert {p['raw_document_id'] for p in days[0].provenance} == ({first.id} if changed else {first.id, second.id})


def test_identical_html_merges_provenance(documents):
    first = documents[0]
    second = replace(first, id='identical-source', sequence=99)
    result = parse([first, second], first.run_id)
    assert len(result.days) == 4
    assert all({p['raw_document_id'] for p in d.provenance} == {first.id, second.id} for d in result.days)


def test_same_date_different_ids_remain_distinct(documents):
    doc = documents[0]
    changed = replace(doc, body=doc.body.replace(b'25/10/2026 12:00', b'08/10/2026 12:00'))
    result = parse([changed], doc.run_id)
    assert len(result.departures) == 9
    assert {d.source_schedule_id for d in result.departures if d.departure_raw == '08/10/2026 12:00'} == {'8141', '8142'}


def test_schedule_row_locators_match_raw_and_ignore_detail_rows(documents):
    from hanoitourist_crawler.parser import sources
    result = parse(documents, documents[0].run_id)
    records = result.coverage['discovered']['schedule_rows']
    # 9 source + 9 rendered + 9 AJAX + 18 page/dialog rendered rows.
    assert len(records) == 45
    assert len(result.departures) == 9
    raw = {d.id: d for d in documents}
    for row in records:
        assert len(row['provenance']) == 1
        provenance = row['provenance'][0]
        parts = provenance['locator'].split(' :: ')
        prefix, locator = ('', parts[0]) if len(parts) == 1 else parts
        html = next(html for html, p in sources(raw[provenance['raw_document_id']]) if p == prefix)
        found = BeautifulSoup(html, 'html.parser').select_one(locator)
        assert found and found.select_one(':scope > td.ngay-khoi-hanh')
        assert 'footable-detail-row' not in found.get('class', [])
        assert row['row_key'] in result.coverage['parsed_schedule_rows']


@pytest.mark.parametrize('change', ['rename_class', 'remove_class', 'remove_cell'])
def test_missing_date_selector_never_hides_schedule_row(documents, change):
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
    result = parse([changed], doc.run_id)
    assert len(result.coverage['discovered']['schedule_rows']) == 9
    assert len(result.departures) == 9
    departure = next(d for d in result.departures if d.source_schedule_id == '8141')
    assert departure.departure_at is None and departure.departure_raw == ''
    assert len(departure.price_cells) == 13 and departure.seats_raw == '9'
    record = next(r for r in result.coverage['discovered']['schedule_rows'] if r['source_schedule_id'] == '8141')
    assert record['provenance'] == departure.provenance
    found = BeautifulSoup(changed.body, 'html.parser').select_one(record['provenance'][0]['locator'])
    assert found and found.select_one('a[href*="/lich/8141/"]')
    assert any('Unparsed departure date' in w for w in result.warnings)


def test_discovery_excludes_only_explicit_ui_rows(documents):
    doc = documents[0]
    soup = BeautifulSoup(doc.body, 'html.parser')
    tbody = soup.select_one('#lichkhoihanh table.footable > tbody')
    for html in ['<tr><th>Header</th></tr>', '<tr class="footable-detail-row"><td>Detail</td></tr>',
                 '<tr><td></td></tr>', '<tr><td><div class="view-empty">No results</div></td></tr>',
                 '<tr class="footable-empty"><td colspan="6">No results</td></tr>']:
        tbody.insert(0, BeautifulSoup(html, 'html.parser').tr)
    tbody.append(BeautifulSoup('<tr><td>Unrecognized data row</td></tr>', 'html.parser').tr)
    result = parse([replace(doc, body=str(soup).encode())], doc.run_id)
    assert len(result.coverage['discovered']['schedule_rows']) == 10
    assert len(result.departures) == 9
    assert any('Unparsed schedule row' in w for w in result.warnings)


@pytest.mark.parametrize('cell_class,content', [
    ('', '<a href="/tour/8140/lich/9999/booking">Chọn</a>'),
    ('ngay-khoi-hanh', '08/10/2026 12:00'),
    ('gia-tu', '100đ'),
    ('', '<span class="view-empty">No results</span><table><tr><td>100đ</td></tr></table>'),
])
def test_footable_empty_marker_cannot_hide_schedule_data(documents, cell_class, content):
    doc = documents[0]
    soup = BeautifulSoup(doc.body, 'html.parser')
    row = BeautifulSoup(f'<tr class="footable-empty"><td class="{cell_class}">{content}</td></tr>', 'html.parser').tr
    soup.select_one('#lichkhoihanh table.footable > tbody').append(row)
    result = parse([replace(doc, body=str(soup).encode())], doc.run_id)
    assert len(result.coverage['discovered']['schedule_rows']) == 10
    record = result.coverage['discovered']['schedule_rows'][-1]
    assert record['provenance']
    assert record['row_key'] in result.coverage['parsed_schedule_rows'] or record['identity_error']

