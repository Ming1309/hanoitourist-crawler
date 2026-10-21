import json
import stat
from dataclasses import replace

import pytest

from hanoitourist_crawler.reporting.diagnostics import classify, observe, safe_url, write_report
from hanoitourist_crawler.models import RawDocument, uid


def test_timeout_is_unknown_not_network_or_javascript():
    run = {'id': uid(), 'capture_status': 'failed', 'manifest': {}, 'errors': ['TimeoutError']}
    result = classify(run, None, [])
    assert result['root_cause'] == 'chưa xác định'
    assert result['observed_failure'][0]['label'] == 'unknown'
    assert result['evidence_gaps']


@pytest.mark.parametrize('owned,kind,role,method,url_match,expected', [
    (True, 'http_document', 'tour', 'GET', True, 'source_unavailable'),
    (False, 'http_document', 'tour', 'GET', True, 'unknown'),
    (True, 'ajax', 'schedule', 'GET', True, 'unknown'),
    (True, 'http_document', 'tour', 'POST', True, 'unknown'),
    (True, 'http_document', 'tour', 'GET', False, 'unknown'),
])
def test_unavailable_requires_exact_primary_evidence(owned, kind, role, method, url_match, expected):
    rid = uid()
    d = RawDocument(rid, 1, kind, role, 'initial', 'https://hanoitourist.vn/a', b'gone', http_status=404, method=method)
    run = {'id': rid, 'capture_status': 'failed', 'manifest': {'navigation': [
        {'owned_primary': owned, 'terminal': True, 'status': 404, 'raw_document_id': d.id,
         'url': d.url if url_match else d.url + 'other'}]}}
    assert classify(run, None, [d])['observed_failure'][0]['label'] == expected
    assert classify(run, None, [replace(d, run_id=uid())])['observed_failure'][0]['label'] == 'unknown'


def test_bounded_events_no_exception_text():
    manifest = {}
    for i in range(300):
        observe(manifest, 'root_wait', 'failed', exception=ValueError('secret-token'))
    assert len(manifest['diagnostics']['events']) == 256
    assert manifest['diagnostics']['overflow'] == 44
    assert 'secret-token' not in json.dumps(manifest)


def test_private_atomic_append_only_reports(tmp_path):
    payload = {'status': 'handled', 'targets': []}
    a = write_report(tmp_path / 'private', payload)
    b = write_report(tmp_path / 'private', payload)
    assert a != b
    from pathlib import Path
    assert stat.S_IMODE(Path(a).stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(a).parent.stat().st_mode) == 0o700
    assert (Path(a).parent / 'manifest.json').exists()
    assert not list(Path(a).parent.glob('.tmp-*'))
    assert safe_url('https://user:pass@hanoitourist.vn/tour?token=secret#password') == 'https://hanoitourist.vn/tour'
