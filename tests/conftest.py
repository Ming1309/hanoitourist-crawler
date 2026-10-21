import os
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from hanoitourist_crawler.models import RawDocument, uid
from hanoitourist_crawler.config import DEFAULT_TOUR_URL
from hanoitourist_crawler.persistence.storage import Storage

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
def capture_history(storage, documents):
    from dataclasses import replace
    from hanoitourist_crawler.processing.parser import parse
    from hanoitourist_crawler.workflows.pipeline import parse_run
    from hanoitourist_crawler.models import now

    def capture(*, url=DEFAULT_TOUR_URL, source_id='8140', at=None, price=None, status='complete'):
        run_id = storage.create_run(url)
        for original in documents:
            body = original.body.replace(DEFAULT_TOUR_URL.encode(), url.encode()).replace(b'8140', source_id.encode())
            if price:
                body = body.replace(b'7,680,000', price.encode())
            storage.append_raw(replace(original, id=uid(), run_id=run_id, body=body, url=url))
        snapshot = parse(storage.load_raw(run_id), run_id)
        first = storage.load_raw(run_id)[0]
        storage.update_run(run_id, capture_status=status, finished_at=at or now(), manifest={
            'actions': {'initial': 'captured'}, 'inventory': snapshot.coverage['discovered'],
            'navigation': [{'url': url, 'status': 200, 'raw_document_id': first.id}]})
        return run_id, parse_run(storage, run_id)
    return capture


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


@pytest.fixture
def http_servers():
    """Native HTTP redirects, isolated loopback endpoints and server-side hits."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from types import SimpleNamespace

    servers = []
    def create(routes=None):
        state = SimpleNamespace(routes=routes or {}, hits=[])
        class Handler(BaseHTTPRequestHandler):
            def respond(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                state.hits.append({'method':self.command,'path':self.path,'body':body,
                                   'headers':dict(self.headers)})
                response = state.routes.get((self.command,self.path), (200,{},b'local endpoint'))
                status, headers, content = response(self) if callable(response) else response
                self.send_response(status)
                for key,value in headers.items(): self.send_header(key,value)
                self.send_header('Content-Length',str(len(content)))
                self.end_headers()
                if self.command != 'HEAD':
                    try: self.wfile.write(content)
                    except (BrokenPipeError, ConnectionResetError): pass
            do_GET = do_POST = do_HEAD = respond
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = Thread(target=server.serve_forever,daemon=True)
        thread.start()
        servers.append((server,thread))
        state.url = f'http://127.0.0.1:{server.server_port}'
        return state
    try:
        yield create
    finally:
        for server,thread in reversed(servers):
            server.shutdown(); server.server_close(); thread.join(timeout=5)
