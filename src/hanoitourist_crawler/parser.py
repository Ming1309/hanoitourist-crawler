"""Offline parser of committed source/DOM/Drupal command payloads."""

import json
import re
from hashlib import sha256
from functools import lru_cache
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from . import PARSER_VERSION
from .models import Day, Departure, Media, PriceCell, Section, Snapshot, json_value
from .normalize import departure_time, integer, money, text
from .selectors import ROOT, SCHEDULE_ID_PATTERN, SECTIONS, SIDEBAR, TITLE


def sources(document):
    if document.kind == 'robots' or (document.http_status and document.http_status >= 400):
        return []
    body = document.body.decode(document.encoding or 'utf-8', errors='replace')
    if document.kind == 'ajax' and ('json' in document.content_type or body.lstrip().startswith('[')):
        commands = json.loads(body)
        if not isinstance(commands, list):
            return []
        return [(c['data'], f'$[{i}].data') for i, c in enumerate(commands)
                if isinstance(c, dict) and c.get('command') in {'insert', 'openDialog'}
                and isinstance(c.get('data'), str)]
    return [(body, '')]


def evidence(document, locator, prefix=''):
    return {'raw_document_id': document.id, 'locator': (prefix + ' :: ' if prefix else '') + locator,
            'captured_at': document.captured_at.isoformat()}


def _schedule_identity(href, base_url):
    if not href:
        return None
    try:
        url = urlsplit(urljoin(base_url, href))
    except ValueError:
        return None
    if url.scheme != 'https' or url.netloc != 'hanoitourist.vn':
        return None
    match = re.fullmatch(SCHEDULE_ID_PATTERN, url.path)
    return (match[1], match[2]) if match else None


def _locator(node):
    parts = []
    while isinstance(node, Tag) and node.name != '[document]':
        position = 1 + sum(isinstance(s, Tag) for s in node.previous_siblings)
        parts.append(f'{node.name}:nth-child({position})')
        node = node.parent
    return ' > '.join(reversed(parts))


def _schedule_rows(soup):
    root = soup.select_one(ROOT)
    scopes = [root.select_one('#lichkhoihanh')] if root and root.select_one('#lichkhoihanh') else []
    scopes += soup.select('[role=dialog]:has(.view-id-lich)')
    if not root and not scopes:
        scopes = [s for s in soup.select('.view-id-lich') if not s.find_parent(class_='view-id-lich')]
    for scope in scopes:
        for ti, table in enumerate(scope.select('table.footable'), 1):
            if table.find_parent('table'):
                continue
            table_rows = table.select(':scope > tbody > tr') or table.select(':scope > tr')
            for row in table_rows:
                cells = row.select(':scope > td')
                if 'footable-detail-row' in row.get('class', []) or not cells:
                    continue
                # Empty UI placeholders are not schedules; unknown data rows are.
                has_data = row.select_one('a, table, img, iframe, video, input, '
                                           'td.ngay-khoi-hanh, td.cho-con-lai, td.gia-tu')
                if not has_data and (not text(row) or (len(cells) == 1 and
                        (cells[0].select_one('.view-empty') or 'footable-empty' in row.get('class', [])
                         or 'footable-empty' in cells[0].get('class', [])))):
                    continue
                ri = 1 + sum(isinstance(s, Tag) for s in row.previous_siblings)
                yield row, _locator(scope), ti, ri


def _row_descriptor(row, scope, table_index, row_index, document, prefix, source_tour_id):
    links = [a for a in row.select('a') if a.find_parent('table') is row.find_parent('table')]
    link = next((a for a in links if '/booking' in a.get('href', '')), links[-1] if links else None)
    href = link.get('href', '') if link else ''
    identity = _schedule_identity(href, document.url if document else 'https://hanoitourist.vn/')
    error = ('missing_booking_link' if not href else 'unrecognized_booking_url') if not identity else ''
    if identity and (not source_tour_id or identity[0] != source_tour_id):
        error = 'unexpected_tour_id' if source_tour_id else 'unknown_tour_id'
    table = price_table(row)
    locator = _locator(row)
    key = [document.id if document else '', prefix, scope, table_index, row_index]
    return {'row_key': sha256(json.dumps(key).encode()).hexdigest(), 'scope': scope,
            'table_index': table_index, 'row_index': row_index, 'booking_href_raw': href,
            'departure_raw': text(row.select_one(':scope > td.ngay-khoi-hanh')),
            'source_tour_id': identity[0] if identity else None,
            'source_schedule_id': identity[1] if identity else None,
            'price_cells': len(table.select('td')) if table else 0, 'identity_error': error,
            'provenance': [evidence(document, locator, prefix)] if document else []}


def price_table(row):
    found = row.select_one('.table-giave table')
    if found is None:
        sibling = row.find_next_sibling('tr')
        if sibling and 'footable-detail-row' in sibling.get('class', []):
            found = sibling.select_one('.table-giave table')
    return found


def inventory(html, *, document=None, prefix='', source_tour_id=''):
    """Discover rows before identity filtering; unresolved rows remain evidence."""
    soup = BeautifulSoup(html, 'html.parser')
    root = soup.select_one(ROOT)
    source_tour_id = root.get('data-history-node-id', '') if root else source_tour_id
    sections = [s for s in SECTIONS if root and root.select_one('#' + s)]
    days = [text(b) for b in root.select('#lichtrinh .accordion-button')] if root else []
    departures, schedule_rows = {}, []
    for row, scope, ti, ri in _schedule_rows(soup):
        record = _row_descriptor(row, scope, ti, ri, document, prefix, source_tour_id)
        schedule_rows.append(record)
        if not record['identity_error']:
            sid = record['source_schedule_id']
            departures[sid] = max(departures.get(sid, 0), record['price_cells'])
    return {'sections': sections, 'days': days, 'departures': departures, 'schedule_rows': schedule_rows}


def merge_inventory(target, incoming):
    for key in ('sections', 'days'):
        target.setdefault(key, [])
        for value in incoming.get(key, []):
            if value not in target[key]:
                target[key].append(value)
    target.setdefault('departures', {})
    for key, count in incoming.get('departures', {}).items():
        target['departures'][key] = max(target['departures'].get(key, 0), count)
    target.setdefault('schedule_rows', [])
    known = {r['row_key'] for r in target['schedule_rows']}
    for record in incoming.get('schedule_rows', []):
        if record['row_key'] not in known:
            target['schedule_rows'].append(record)
            known.add(record['row_key'])


def clean_content(node):
    result = BeautifulSoup(str(node), 'html.parser')
    for excluded in result.select('script, style, form, nav, .slick-cloned, .footable-detail-row'):
        excluded.decompose()
    return str(result), text(result)


@lru_cache(maxsize=64)
def _content_signature(content_html, base_url):
    """Canonicalize verified accordion/image loading state, not stored HTML."""
    soup = BeautifulSoup(content_html, 'html.parser')
    targets = tuple((text(a), urljoin(base_url, a['href'])) for a in soup.select('a[href]'))
    for node in soup.select('.accordion-button, .accordion-collapse, .accordion-body'):
        node.attrs.pop('aria-expanded', None)
        classes = [c for c in node.get('class', []) if c not in {'show', 'collapsed'}]
        if classes:
            node['class'] = classes
        else:
            node.attrs.pop('class', None)
        if {'accordion-collapse', 'accordion-body'} & set(classes) and node.has_attr('style'):
            styles = [s.strip() for s in node['style'].split(';') if s.strip()
                      and s.split(':', 1)[0].strip().lower() != 'display']
            if styles:
                node['style'] = ';'.join(styles)
            else:
                node.attrs.pop('style', None)
    for node in soup.select('img[class]'):
        classes = [c for c in node['class'] if c not in {'lazyload', 'lazyloaded', 'ls-is-cached'}]
        if classes:
            node['class'] = classes
        else:
            node.attrs.pop('class', None)
    return sha256(str(soup).encode()).hexdigest(), targets


def _merge_content(snapshot, items, item, key):
    base_url = snapshot.canonical_url
    _, targets = _content_signature(item.content_html, base_url)
    for old in items:
        old_key = old.key if isinstance(old, Section) else f'day:{old.ordinal}'
        if old_key == key and old.content_text == item.content_text:
            if _content_signature(old.content_html, base_url)[1] != targets:
                snapshot.warnings.append(f'Conflicting section link targets retained for {key}')
    return _merge(items, item, lambda value: (
        value.key if isinstance(value, Section) else f'day:{value.ordinal}',
        value.title, getattr(value, 'state', ''),
        _content_signature(value.content_html, base_url)[0]))


def _merge(items, item, signature):
    for existing in items:
        if signature(existing) == signature(item):
            for p in item.provenance:
                if p not in existing.provenance:
                    existing.provenance.append(p)
            return existing
    items.append(item)
    return item


def parse(documents, run_id, *, version=PARSER_VERSION):
    snapshot = Snapshot(run_id, version)
    inventory_all = {'sections': [], 'days': [], 'departures': {}, 'schedule_rows': []}
    parsed_rows = {}
    for document in documents:
        for html, prefix in sources(document):
            incoming = inventory(html, document=document, prefix=prefix, source_tour_id=snapshot.source_tour_id)
            merge_inventory(inventory_all, incoming)
            soup = BeautifulSoup(html, 'html.parser')
            root = soup.select_one(ROOT)
            if root:
                source_id = root.get('data-history-node-id', '')
                if snapshot.source_tour_id and snapshot.source_tour_id != source_id:
                    snapshot.warnings.append('Different tour identity in staging')
                    continue
                snapshot.source_tour_id = source_id
                title = root.select_one(TITLE)
                if title and not snapshot.title:
                    snapshot.title = text(title)
                    snapshot.provenance['title'] = [evidence(document, ROOT + ' ' + TITLE, prefix)]
                canonical = soup.select_one('link[rel=canonical]')
                if canonical and not snapshot.canonical_url:
                    snapshot.canonical_url = urljoin(document.url, canonical.get('href', ''))
                elif not snapshot.canonical_url:
                    snapshot.canonical_url = document.url
                for key in SECTIONS:
                    node = root.select_one('#' + key)
                    if node:
                        contents = [n for n in node.select('.view-content')
                                    if not n.find_parent(class_='view-content')]
                        body, value = clean_content(''.join(str(n) for n in contents) if contents else node)
                        title_node = node.select_one('.header-tour h3')
                        section = Section(key, SECTIONS.index(key), text(title_node), body, value,
                                          'present' if value else 'empty', [evidence(document, '#' + key, prefix)])
                        _merge_content(snapshot, snapshot.sections, section, key)
                content_root = root.select_one('.field--name-field-tour-content-markup')
                if content_root:
                    for ordinal, node in enumerate(content_root.select(':scope > *'), len(SECTIONS)):
                        key = node.get('id') or f'unknown-{ordinal}'
                        if key not in SECTIONS and node.name not in {'script', 'style', 'form', 'nav'}:
                            body, value = clean_content(node)
                            _merge_content(snapshot, snapshot.sections, Section(key, ordinal, text(node.find(re.compile('^h[1-6]$'))),
                                    body, value, 'present' if value else 'empty',
                                    [evidence(document, '#' + key if node.get('id') else
                                              f'.field--name-field-tour-content-markup > :nth-child({ordinal-len(SECTIONS)+1})',
                                              prefix)]), key)
                            if f'Unknown tour section: {key}' not in snapshot.warnings:
                                snapshot.warnings.append(f'Unknown tour section: {key}')
                for index, node in enumerate(root.select('#lichtrinh .accordion-item'), 1):
                    heading, body = node.select_one('.accordion-button'), node.select_one('.accordion-body')
                    if body is None:
                        snapshot.warnings.append(f'Missing itinerary body {index}')
                        continue
                    body_html, value = clean_content(body)
                    label = text(heading)
                    day = Day(index, label, label, body_html, value,
                              list(dict.fromkeys(i.get('title', '') for i in body.select('img[title]'))),
                              [evidence(document, f'#lichtrinh .accordion-item:nth-child({index})', prefix)])
                    _merge_content(snapshot, snapshot.days, day, f'day:{index}')
                summary = root.select_one('#block-tourcommenttitle')
                if summary and not snapshot.review_summary:
                    snapshot.review_summary = text(summary)
                    snapshot.provenance['review_summary'] = [evidence(document, '#block-tourcommenttitle', prefix)]
                sidebar = soup.select_one(SIDEBAR)
                if sidebar and snapshot.source_tour_id in str(sidebar) and not snapshot.sidebar_text:
                    snapshot.sidebar_text = text(sidebar)
                    snapshot.provenance['sidebar'] = [evidence(document, SIDEBAR, prefix)]
                    raw_price = text(sidebar.select_one('.lich-gia-value'))
                    snapshot.promotional_price_raw = raw_price
                    snapshot.promotional_price, snapshot.promotional_currency = money(raw_price)
                    duration = re.search(r'(\d+) ngày\s+(\d+) đêm', snapshot.sidebar_text)
                    if duration:
                        snapshot.duration_text = duration[0]
                        snapshot.duration_days, snapshot.duration_nights = int(duration[1]), int(duration[2])
                    for detail in sidebar.select('.detail-item-value'):
                        value = text(detail)
                        if value.startswith('Từ:'):
                            snapshot.origin_text = text(detail.select_one('strong'))
                        if value.startswith('Số chỗ còn nhận:'):
                            snapshot.seats_raw = text(detail.select_one('strong'))
                            snapshot.seats = integer(snapshot.seats_raw)
                _media(snapshot, root, document, prefix)
            for (row, _, _, _), record in zip(_schedule_rows(soup), incoming['schedule_rows']):
                observation = _departure(snapshot, row, record, document, prefix)
                if observation:
                    parsed_rows[record['row_key']] = observation.id
    for key in SECTIONS:
        if not any(s.key == key for s in snapshot.sections):
            snapshot.sections.append(Section(key, SECTIONS.index(key), '', '', '', 'missing', []))
    snapshot.coverage = {'discovered': inventory_all, 'parsed_schedule_rows': parsed_rows,
                         'source_timezone': 'Asia/Ho_Chi_Minh',
                         'timezone_inferred_from_source': True}
    if not snapshot.title or not snapshot.source_tour_id:
        raise ValueError('No recognizable tour document in staging')
    return snapshot


def _media(snapshot, root, document, prefix):
    for index, node in enumerate(root.select('img, iframe, video source'), 1):
        if node.find_parent('form') or node.find_parent(class_='slick-cloned'):
            continue
        urls = [node.get(k) for k in ('data-src', 'src') if node.get(k)]
        urls += [v.strip().split()[0] for v in node.get('data-srcset', node.get('srcset', '')).split(',') if v.strip()]
        parent = node.find_parent(id=True)
        section = parent.get('id') if parent else None
        day = node.find_parent(class_='accordion-item')
        day_number = None
        if day:
            label = text(day.select_one('.accordion-button'))
            match = re.search(r'NGÀY\s+(\d+)', label, re.I)
            day_number = int(match[1]) if match else None
        role = 'icon' if node.get('title') else ('thumbnail' if node.find_parent(class_='view-tva-nav')
               else 'gallery' if node.find_parent(class_='view-tva-div') else 'embed' if node.name != 'img' else 'content')
        for raw in dict.fromkeys(urls):
            url = urljoin(document.url, raw)
            if not url.startswith(('https://', 'http://')):
                continue
            item = Media(role, url, raw, node.get('alt', ''), node.get('title', ''), index, section, day_number,
                         [evidence(document, f'{ROOT} {node.name}[src="{raw}"]', prefix)])
            _merge(snapshot.media, item, lambda m: (m.role, m.url, m.day_ordinal))


def _departure(snapshot, row, record, document, prefix):
    if record['identity_error'] or record['source_tour_id'] != snapshot.source_tour_id:
        snapshot.warnings.append(f'Unparsed schedule row {record["row_key"]}: '
                                 f'{record["identity_error"] or "unexpected_tour_id"}; '
                                 f'{record["departure_raw"]}; raw={document.id}')
        return None
    sid = record['source_schedule_id']
    locator = _locator(row)
    prov = list(record['provenance'])
    date_raw, seats_raw, raw_price = (text(row.select_one(s)) for s in
                                    ('td.ngay-khoi-hanh', 'td.cho-con-lai', 'td.gia-tu'))
    amount, currency = money(raw_price)
    departure = Departure(sid, urljoin(document.url, record['booking_href_raw']), date_raw, departure_time(date_raw),
                          seats_raw, integer(seats_raw), raw_price, amount, currency,
                          text(row.select_one('td.dac-diem')), document.state_key, document.captured_at, prov)
    table = price_table(row)
    if table:
        table_rows = table.select(':scope > tbody > tr') or table.select(':scope > tr')
        headers = [text(h) for h in table_rows[0].select(':scope > th')][1:] if table_rows else []
        occupied = {}
        for ri, table_row in enumerate(table_rows[1:], 1):
            label = text(table_row.select_one('th'))
            ci = 0
            for cell in table_row.select(':scope > td'):
                while occupied.get((ri, ci)):
                    ci += 1
                span, rowspan = int(cell.get('colspan', 1)), int(cell.get('rowspan', 1))
                raw = text(cell)
                value, curr = money(raw)
                applies = headers[ci:ci + span]
                departure.price_cells.append(PriceCell(ri, ci, label, applies[0] if span == 1 and applies else None,
                    raw, value, curr, rowspan, span, applies,
                    [evidence(document, locator + f' .table-giave tr[{ri}] td[column={ci}]', prefix)]))
                for rr in range(ri, ri + rowspan):
                    for cc in range(ci, ci + span):
                        occupied[rr, cc] = True
                ci += span
    if not date_raw or departure.departure_at is None:
        snapshot.warnings.append(f'Unparsed departure date: {sid} {date_raw}; '
                                 f'raw={document.id}; locator={prov[0]["locator"]}')
    if raw_price and amount is None:
        snapshot.warnings.append(f'Unparsed price-from: {sid} {raw_price}')
    for cell in departure.price_cells:
        if cell.raw_value and cell.amount is None:
            snapshot.warnings.append(f'Unparsed price cell: {sid} {cell.raw_value}')
    def signature(d):
        values = (d.source_schedule_id, d.departure_raw, d.seats_raw, d.price_from_raw, d.features,
                  [(c.row_index, c.column_index, c.price_label, c.age_label, c.raw_value, c.rowspan, c.colspan)
                   for c in d.price_cells])
        return sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()
    existing = _merge(snapshot.departures, departure, signature)
    if existing is not departure:
        for old, new in zip(existing.price_cells, departure.price_cells):
            old.provenance.extend(p for p in new.provenance if p not in old.provenance)
    return existing
