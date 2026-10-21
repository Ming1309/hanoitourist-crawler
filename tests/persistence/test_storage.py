import json
from dataclasses import replace, asdict
from datetime import datetime, timezone

import pytest
from hanoitourist_crawler import PARSER_VERSION
from hanoitourist_crawler.processing.parser import parse
from hanoitourist_crawler.models import json_value
from hanoitourist_crawler.config import DEFAULT_TOUR_URL

pytestmark = pytest.mark.postgres


def staged(storage, documents):
    run_id = storage.create_run(DEFAULT_TOUR_URL)
    copies = [replace(d, run_id=run_id) for d in documents]
    for doc in copies:
        storage.append_raw(doc)
    return run_id, copies


def test_history_and_roundtrip(storage, documents):
    first, docs = staged(storage, documents)
    storage.init_schema()
    snapshot = parse(docs, first)
    persisted = storage.persist_snapshot(snapshot)
    def canonical(value):
        if isinstance(value, dict):
            return {k: canonical(v) for k, v in value.items()}
        if isinstance(value, list):
            values = [canonical(v) for v in value]
            return sorted(values, key=lambda v: json.dumps(json_value(v), sort_keys=True)) if values and isinstance(values[0], dict) else values
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc)
        return value
    assert canonical(asdict(persisted)) == canonical(asdict(snapshot))
    assert storage.load_raw(first)[0].body == docs[0].body
    assert storage.load_raw(first)[0].sha256 == docs[0].sha256
    second, _ = staged(storage, [replace(d, id=__import__('uuid').uuid4().__str__()) for d in documents])
    assert first != second and len(storage.load_raw(first)) == len(storage.load_raw(second))
    repeated = storage.persist_snapshot(snapshot)
    assert repeated.id == persisted.id
    new = parse(docs, first, version='0.2.0')
    storage.persist_snapshot(new)
    assert storage.get_snapshot(first, PARSER_VERSION).id == persisted.id
    assert storage.get_snapshot(first).id == new.id


def test_rollback_keeps_staging(storage, documents):
    run_id, docs = staged(storage, documents)
    snapshot = parse(docs, run_id)
    snapshot.departures[0].price_cells[0].colspan = 0
    with pytest.raises(Exception):
        storage.persist_snapshot(snapshot)
    assert storage.get_snapshot(run_id) is None
    assert len(storage.load_raw(run_id)) == 4


def test_content_variants_and_row_coverage_roundtrip(storage, documents):
    from bs4 import BeautifulSoup
    from hanoitourist_crawler.models import uid
    from hanoitourist_crawler.processing.validation import validate
    run = storage.create_run(DEFAULT_TOUR_URL)
    soup = BeautifulSoup(documents[0].body, 'html.parser')
    for node in [soup.select_one('#dieukien'), soup.select_one('#lichtrinh .accordion-body')]:
        node.clear()
        node.append(BeautifulSoup('<a href="/terms-a.pdf">Terms</a>', 'html.parser'))
    first = replace(documents[0], run_id=run, body=str(soup).encode())
    second = replace(first, id=uid(), sequence=2, body=first.body.replace(b'/terms-a.pdf', b'/terms-b.pdf'))
    for doc in [first, second]:
        storage.append_raw(doc)
    snapshot = parse([first, second], run)
    validate(snapshot, [first, second], {'capture_status': 'complete', 'manifest': {'actions': {'initial': 'captured'}}})
    persisted = storage.persist_snapshot(snapshot)
    assert persisted.status == 'partial'
    assert persisted.coverage == snapshot.coverage
    for collection, key in [(persisted.sections, 'dieukien'), (persisted.days, 1)]:
        items = [i for i in collection if getattr(i, 'key', getattr(i, 'ordinal', None)) == key]
        assert len(items) == 2
        assert {BeautifulSoup(i.content_html, 'html.parser').select_one('a')['href'] for i in items} == {'/terms-a.pdf', '/terms-b.pdf'}
        assert {tuple(p['raw_document_id'] for p in i.provenance) for i in items} == {(first.id,), (second.id,)}


def test_missing_date_observation_and_coverage_roundtrip(storage, documents):
    from bs4 import BeautifulSoup
    from hanoitourist_crawler.processing.validation import validate
    soup = BeautifulSoup(documents[0].body, 'html.parser')
    soup.select_one('#lichkhoihanh td.ngay-khoi-hanh')['class'] = ['date-column-new']
    run, docs = staged(storage, [replace(documents[0], body=str(soup).encode())])
    snapshot = validate(parse(docs, run), docs, {'capture_status': 'complete', 'manifest': {'actions': {'initial': 'captured'}}})
    saved = storage.persist_snapshot(snapshot)
    assert saved.status == 'partial' and len(saved.departures) == 9
    assert saved.coverage == snapshot.coverage
    departure = next(d for d in saved.departures if d.source_schedule_id == '8141')
    assert departure.departure_at is None and departure.departure_raw == ''
    assert len(departure.price_cells) == 13
    assert storage.load_raw(run)[0].body == docs[0].body

