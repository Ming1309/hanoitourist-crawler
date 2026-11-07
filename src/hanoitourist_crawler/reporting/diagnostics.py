"""Bounded opt-in observations and private, append-only diagnostic reports.

Findings describe observed failures, never infer a root cause from a timeout or
from a coincident JavaScript error. Raw bytes are copied only into private files.
"""
import json
import os
import platform
import re
import time
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from ..models import json_value, now, uid

SCHEMA = 'diagnostics-v1'
LIMIT = 256


def safe_url(value):
    try:
        u = urlsplit(str(value))
        if u.scheme and u.scheme not in {'https', 'http'}:
            return '[redacted-url]'
        return urlunsplit((u.scheme, u.hostname or '', u.path[:2048], '', ''))
    except ValueError:
        return '[invalid-url]'


def observe(manifest, step, status, *, elapsed=None, exception=None, facts=None):
    diagnostic = manifest.setdefault('diagnostics', {'schema': SCHEMA, 'events': [], 'overflow': 0})
    events = diagnostic['events']
    # Facts are supplied by code, not exception messages, headers or page text.
    event = {'id': uid(), 'step': step[:96], 'status': status, 'at': now().isoformat()}
    if elapsed is not None:
        event['elapsed_seconds'] = round(elapsed, 4)
    if exception is not None:
        event['exception_type'] = type(exception).__name__
    if facts:
        event['facts'] = facts
    if status == 'failed':
        # Preserve the innermost failed operation even when bounded events overflow.
        diagnostic.setdefault('first_failed_operation', event)
    elif status == 'complete':
        diagnostic['last_successful_operation'] = event
    if len(events) >= LIMIT:
        diagnostic['overflow'] += 1
    else:
        events.append(event)


@contextmanager
def phase(storage, run_id, step):
    manifest = storage.get_run(run_id).get('manifest') or {}
    if manifest.get('diagnostics', {}).get('schema') != SCHEMA:
        yield
        return
    start = time.monotonic()
    observe(manifest, step, 'start')
    storage.update_run(run_id, manifest=manifest)
    try:
        yield
    except BaseException as exc:
        # Reload: nested operations can have appended observations.
        manifest = storage.get_run(run_id)['manifest']
        observe(manifest, step, 'failed', elapsed=time.monotonic()-start, exception=exc)
        storage.update_run(run_id, manifest=manifest)
        raise
    else:
        manifest = storage.get_run(run_id)['manifest']
        observe(manifest, step, 'complete', elapsed=time.monotonic()-start)
        storage.update_run(run_id, manifest=manifest)


def classify(run, snapshot, documents):
    """Pure classifier. Verified locators are local to exactly this persisted run."""
    raw = {d.id: d for d in documents if d.run_id == run['id'] and sha256(d.body).hexdigest() == d.sha256}
    findings, gaps = [], []
    for nav in run.get('manifest', {}).get('navigation', []):
        d = raw.get(nav.get('raw_document_id'))
        if (d and nav.get('owned_primary') is True and nav.get('terminal') is True
                and d.kind == 'http_document' and d.role == 'tour' and d.method in {'GET', 'HEAD'}
                and d.url == nav.get('url') and d.http_status == nav.get('status') and d.http_status >= 400):
            findings.append({'label': 'source_unavailable' if d.http_status in {404, 410} else 'primary_http_failure',
                             'status': d.http_status, 'evidence': [d.id], 'sha256': d.sha256})
    diagnostic = run.get('manifest', {}).get('diagnostics', {})
    if diagnostic.get('schema') != SCHEMA:
        gaps.append('Source legacy: không có observations theo bước; không suy nguyên nhân từ TimeoutError.')
    events = list(diagnostic.get('events', [])[:LIMIT])
    first_failure = diagnostic.get('first_failed_operation')
    if first_failure and first_failure not in events:
        events.append(first_failure)
    for event in events:
        if (event.get('step') == 'supported_schedule_scope' and event.get('status') == 'complete'
                and event.get('facts', {}).get('count') == 0):
            findings.append({'label': 'unsupported_schedule_layout_observed', 'evidence': [event.get('id')],
                             'limitation': 'Observation applies only to this run, not historical source attempts.'})
        if event.get('status') != 'failed':
            continue
        step = event.get('step')
        label = {'parse': 'parse_failure', 'validation': 'validation_failure',
                 'identity_scope': 'identity_publication_rejected',
                 'root_wait': 'interaction_wait_failure',
                 'schedule_row_or_empty_wait': 'interaction_wait_failure',
                 'accordion_wait': 'interaction_wait_failure', 'dialog_wait': 'interaction_wait_failure',
                 'price_expansion_wait': 'interaction_wait_failure',
                 'publication': 'publication_failure', 'robots': 'robots_operation_failure'}.get(step, 'operation_failure')
        findings.append({'label': label, 'step': step, 'exception_type': event.get('exception_type'),
                         'evidence': [event.get('id')]})
    if snapshot and snapshot.status != 'complete':
        findings.append({'label': 'incomplete_snapshot', 'evidence': [snapshot.id],
                         'consequence_only': run['capture_status'] != 'complete'})
    if not findings:
        findings.append({'label': 'not_reproduced' if run['capture_status'] == 'complete' and snapshot
                         and snapshot.status == 'complete' else 'unknown', 'evidence': [run['id']]})
    return {'observed_failure': findings, 'root_cause': 'chưa xác định', 'evidence_gaps': gaps}


def private_dir(path):
    path = Path(path)
    # Do not change permissions of user-owned export/catalogue directories.
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for item in reversed(missing):
        item.mkdir(mode=0o700)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError('Diagnostic directory must be private (0700)')
    return path


def atomic_private(path, body):
    path = Path(path)
    temporary = path.parent / ('.tmp-' + uid())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def bounded_text(value):
    value = re.sub(r'(?<=:price:)[^\s<>\"\']+|https?://[^\s<>\"\']+|//[^\s<>\"\']+|/(?!/)[^\s<>\"\']+|(?<![\w/])[\w.-]+/[^\s<>\"\']+',
                   lambda match: safe_url(match[0]), str(value), flags=re.I)
    value = ''.join(c for c in value if c >= ' ' or c == '\n')
    return value[:2048]


def sanitized(value):
    """Bound untrusted persisted text; URLs never expose userinfo/query/fragment."""
    if isinstance(value, str):
        return bounded_text(value)
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            key = bounded_text(k)
            suffix = 1
            while key in result:
                suffix += 1
                key = f'{bounded_text(k)} [{suffix}]'
            if isinstance(v, str) and (k == 'url' or str(k).endswith(('_url', '_href', '_href_raw'))):
                v = safe_url(v)
            result[key] = sanitized(v)
        return result
    if isinstance(value, list):
        return [sanitized(v) for v in value]
    return value


def write_report(root, payload, documents=()):
    directory = private_dir(Path(root) / uid())
    payload = {**payload, 'schema': 'scoped-retry-report-v1', 'report_at': now(),
               'report_runtime': {'python': platform.python_version()},
               'limitation': 'Scoped retry; discovery not run; catalogue coverage not verified. '
                             'Retry success does not establish the historical root cause.'}
    artifacts = {}
    def save(name, body):
        atomic_private(directory / name, body)
        artifacts[name] = {'sha256': sha256(body).hexdigest(), 'bytes': len(body)}
    for document in documents:
        name = f'raw-{document.run_id}-{document.id}.bin'
        save(name, document.body)
        artifacts[name].update({'captured_at': json_value(document.captured_at), 'raw_document_id': document.id})
    save('report.json', json.dumps(sanitized(json_value(payload)), ensure_ascii=False, indent=2).encode())
    # Only code-controlled labels, UUIDs and query-free URLs enter Markdown.
    lines = ['# Báo cáo scoped retry', '', payload['limitation'], '',
             f"Trạng thái: {payload.get('status', 'unknown')}", '', 'Nguyên nhân gốc: **chưa xác định**.', '']
    for target in payload.get('targets', []):
        url = safe_url(target['requested_url'])
        lines += [f"## {url.replace('<', '&lt;').replace('>', '&gt;').replace('`', '&#96;')}",
                  f"- Entity: {target['tour_id']}", f"- Skip: {target.get('skip_reason') or 'không'}",
                  f"- Source runs: {', '.join(target.get('source_run_ids', []))}",
                  f"- Retry runs: {', '.join(target.get('retry_run_ids', []))}",
                  '- Xem report.json để đối chiếu accepted/display refs và evidence.', '']
    save('REPORT.md', '\n'.join(lines).encode())
    atomic_private(directory / 'manifest.json', json.dumps({'schema': SCHEMA, 'artifacts': artifacts}, indent=2).encode())
    return str(directory / 'report.json')
