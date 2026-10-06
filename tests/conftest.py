import os
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from hanoitourist_crawler.models import RawDocument, uid
from hanoitourist_crawler.config import DEFAULT_TOUR_URL
from hanoitourist_crawler.storage import Storage

FIXTURES = Path(__file__).parent / 'fixtures'


@pytest.fixture
def documents():
    run_id = uid()
    specs = [('tour_source.html','http_document','tour'), ('tour_rendered.html','dom','tour_state'),
             ('schedule_dialog.json','ajax','schedule'), ('schedule_rendered.html','dom','tour_state')]
    return [RawDocument(run_id, i + 1, kind, role, name, DEFAULT_TOUR_URL, (FIXTURES / name).read_bytes(),
                        content_type='application/json' if name.endswith('.json') else 'text/html')
            for i, (name, kind, role) in enumerate(specs)]


@pytest.fixture
def storage():
    url = os.environ.get('TEST_DATABASE_URL')
    if not url:
        pytest.skip('TEST_DATABASE_URL not set; isolated PostgreSQL integration not run')
    schema = 'crawler_test_' + uid().replace('-', '')
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    repository = Storage(url, schema=schema)
    repository.init_schema()
    try:
        yield repository
    finally:
        repository.close()
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


@pytest.fixture
def chromium_available():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
            browser.close()
        except Exception:
            pytest.skip('Chromium unavailable; install with python -m playwright install chromium')
    return True


@pytest.fixture
def chromium():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception:
            pytest.skip('Chromium unavailable; install with python -m playwright install chromium')
        try:
            yield browser
        finally:
            browser.close()
