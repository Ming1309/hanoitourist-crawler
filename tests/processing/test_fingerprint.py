from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from hanoitourist_crawler.processing.fingerprint import fingerprint
from hanoitourist_crawler.models import uid
from hanoitourist_crawler.processing.parser import parse


def test_metadata_and_collection_order_are_not_changes(documents):
    first = parse(documents, documents[0].run_id)
    second = deepcopy(first)
    second.id = uid(); second.run_id = uid(); second.parser_version = 'other'
    second.parsed_at += timedelta(days=1)
    second.coverage = {}; second.warnings = ['capture diagnostic']
    for collection in (second.sections, second.days, second.media, second.departures):
        collection.reverse()
        for item in collection:
            item.id = uid(); item.provenance = []
    for item in second.departures:
        item.observed_at += timedelta(days=1); item.state_key = 'other'
        item.price_cells.reverse()
        for cell in item.price_cells:
            cell.id = uid(); cell.provenance = []
    assert fingerprint(first) == fingerprint(second)


@pytest.mark.parametrize('field', ['title','canonical_url','origin_text','duration_text','review_summary','sidebar_text','seats_raw'])
def test_scalar_content_changes(documents, field):
    first = parse(documents, documents[0].run_id)
    second = deepcopy(first)
    setattr(second, field, getattr(second, field) + ' changed')
    assert fingerprint(first) != fingerprint(second)


@pytest.mark.parametrize('kind', ['price','seats','day','link','image','rowspan','zero'])
def test_nested_business_content_changes(documents, kind):
    first = parse(documents, documents[0].run_id)
    second = deepcopy(first)
    if kind == 'price': second.departures[0].price_cells[0].amount += Decimal('1')
    if kind == 'seats': second.departures[0].seats = 0
    if kind == 'day': second.days[0].content_text += ' changed'
    if kind == 'link': second.sections[0].content_html += '<a href="/terms.pdf?v=2#terms">Terms</a>'
    if kind == 'image': second.media[0].url += '?v=2'
    if kind == 'rowspan': second.departures[0].price_cells[0].rowspan += 1
    if kind == 'zero': second.departures[0].price_cells[0].amount = None
    assert fingerprint(first) != fingerprint(second)


def test_verified_ui_state_does_not_change_content(documents):
    first = parse(documents, documents[0].run_id)
    second = deepcopy(first)
    for day in second.days:
        day.content_html = day.content_html.replace('aria-expanded="false"', 'aria-expanded="true"')
    assert fingerprint(first) == fingerprint(second)


def test_media_order_changes_are_retained(documents):
    first = parse(documents, documents[0].run_id)
    second = deepcopy(first)
    second.media[0].ordinal += 100
    assert fingerprint(first) != fingerprint(second)


@pytest.mark.postgres
def test_fingerprint_stable_through_postgres_timezone(storage, capture_history):
    run, persisted = capture_history()
    replay = parse(storage.load_raw(run), run)
    assert fingerprint(replay) == fingerprint(persisted)
