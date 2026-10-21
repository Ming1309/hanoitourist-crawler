"""Manual, bounded retry of explicitly selected frozen detail failures.

No discovery, backlog selection, scheduler mutation or generic recovery is used.
"""
import time
from dataclasses import replace
from functools import wraps
from pathlib import Path
from uuid import UUID

from .attempts import complete_retry, complete_retry_stopped
from ..acquisition.collector import Collector, RequestPacer
from ..reporting.diagnostics import SCHEMA, classify, observe, private_dir, safe_url, write_report
from .incremental import _export
from ..models import BatchResult, json_value, now, uid
from .pipeline import collect_run
from ..acquisition.scope import tour_url
from ..acquisition.selectors import ROOT

CONTEXT = 'scoped-retry-v1'
CONFIG_FIELDS = ('headless', 'run_timeout', 'navigation_timeout', 'batch_timeout', 'request_interval',
                 'max_retries', 'max_document_bytes', 'max_run_bytes', 'max_pages', 'max_states')
LIMITATION = 'Scoped retry; discovery not run; catalogue coverage not verified'


class DiagnosticCollector(Collector):
    """Acquisition semantics unchanged; observations never become parse inputs."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        context = self.storage.get_run(self.run_id)['manifest']['retry_scope']
        self.allowed_urls = set(context['aliases'])
        self.manifest['retry_scope'] = context
        self.manifest['diagnostics'] = {'schema': SCHEMA, 'events': [], 'overflow': 0}

    def redirect_target(self, location, source):
        target = super().redirect_target(location, source)
        if tour_url(target) not in self.allowed_urls:
            raise ValueError('Redirect outside verified identity')
        return target

    def operation(self, step, function, *args, **kwargs):
        start = time.monotonic()
        selectors = {'root_wait': ROOT,
                     'supported_schedule_scope': '#lichkhoihanh .view-id-lich, #lichkhoihanh .footable, #lichkhoihanh .view-empty',
                     'schedule_row_or_empty_wait': '#lichkhoihanh td.ngay-khoi-hanh, #lichkhoihanh .view-empty',
                     'price_expansion_wait': '.table-giave'}
        facts = {'state_key': self.state, 'raw_sequence': self.sequence,
                 'remaining_seconds': max(0, min(self.deadline-time.monotonic(), self.wall_deadline-time.time()))}
        if step in selectors:
            facts['selector'] = selectors[step]
        observe(self.manifest, step, 'start', facts=facts)
        self.storage.update_run(self.run_id, manifest=self.manifest)
        try:
            result = function(*args, **kwargs)
        except BaseException as exc:
            observe(self.manifest, step, 'failed', elapsed=time.monotonic()-start, exception=exc, facts=facts)
            self.storage.update_run(self.run_id, manifest=self.manifest)
            raise
        if isinstance(result, (int, bool)):
            facts['count'] = result
        observe(self.manifest, step, 'complete', elapsed=time.monotonic()-start, facts=facts)
        self.storage.update_run(self.run_id, manifest=self.manifest)
        return result

    def page_ready(self):
        self.page.on('pageerror', lambda _: observe(self.manifest, 'pageerror', 'observed'))

    def failed_request(self, request):
        if request not in self.blocked and self.data_path(request.url):
            observe(self.manifest, 'requestfailed', 'observed', facts={'url': safe_url(request.url)})
        super().failed_request(request)


def _instrument(name):
    original = getattr(Collector, name)
    @wraps(original)
    def wrapped(self, *args, **kwargs):
        return self.operation('robots' if name == 'load_robots' else name, original, self, *args, **kwargs)
    return wrapped


for _name in ('load_robots', 'navigate', 'checkpoint', 'expand_prices', 'traverse', 'select_combinations', 'settle'):
    setattr(DiagnosticCollector, _name, _instrument(_name))


def _resolve(storage, tour_id):
    seen = set()
    while True:
        if tour_id in seen:
            raise ValueError('Cyclic superseded identity')
        seen.add(tour_id)
        tour = storage.get_tour(tour_id)
        if not tour['superseded_by_id']:
            return tour
        tour_id = tour['superseded_by_id']


def _baseline(tour):
    return {key: tour[key] for key in ('current_snapshot_id', 'latest_attempt_run_id',
                                      'last_successful_check_at', 'availability')}


def _skip(storage, tour, source_end):
    if tour['availability'] == 'unavailable':
        return 'currently_unavailable'
    sid = tour['current_snapshot_id']
    snapshot = storage.get_snapshot_by_id(sid) if sid else None
    if snapshot:
        run = storage.get_run(snapshot.run_id)
        watermark = tour['last_successful_check_at']
        if (snapshot.status == 'complete' and run['capture_status'] == 'complete' and watermark
                and run['finished_at'] and watermark == run['finished_at'] and watermark > source_end):
            return 'newer_success'
    return None


def select_targets(storage, source_batch_id, urls):
    source_batch_id = str(UUID(source_batch_id))
    batch = storage.get_batch(source_batch_id)
    if batch['stage'] != 'frozen' or not batch['finished_at']:
        raise ValueError('Source batch must be frozen and finished')
    requested = list(dict.fromkeys(tour_url(url) for url in urls))
    if not requested:
        raise ValueError('At least one URL is required')
    candidates = {}
    for row in storage.batch_tours(source_batch_id):
        if row['outcome'] not in {'partial', 'failed'}:
            continue
        if not row['attempt_run_ids'] or not row['frozen_state']:
            raise ValueError('Source detail failure has incomplete references')
        runs = [storage.get_run(str(UUID(rid))) for rid in row['attempt_run_ids']]
        source_urls = {tour_url(row['frozen_state']['canonical_url'])}
        source_urls.update(tour_url(run['requested_url']) for run in runs)
        for url in source_urls:
            if url in candidates and candidates[url]['tour_id'] != row['tour_id']:
                raise ValueError('Ambiguous source identity')
            candidates[url] = row
    if any(url not in candidates for url in requested):
        raise ValueError('Every URL must belong to a frozen partial/failed source row')
    targets, seen = [], set()
    for url in requested:
        row = candidates[url]
        tour = _resolve(storage, row['tour_id'])
        if tour['id'] in seen:
            next(t for t in targets if t['tour_id'] == tour['id'])['user_urls'].append(url)
            continue
        seen.add(tour['id'])
        aliases = set(tour_url(u) for u in storage.tour_aliases(tour['id']))
        effective = tour_url(tour['canonical_url'])
        if effective not in aliases:
            raise ValueError('Current transport identity has no verified alias')
        targets.append({'requested_url': url, 'user_urls': [url], 'effective_url': effective,
                        'tour_id': tour['id'], 'source_tour_id': row['tour_id'],
                        'source_outcome': row['outcome'], 'source_run_ids': list(row['attempt_run_ids']),
                        'source_display_snapshot_id': row['display_snapshot_id'],
                        'aliases': sorted(aliases), 'baseline': _baseline(tour),
                        'skip_reason': _skip(storage, tour, batch['finished_at'])})
    return targets


def _context(batch):
    records = [e for e in batch['errors'] if isinstance(e, dict) and e.get('schema') == CONTEXT]
    if len(records) != 1 or not isinstance(records[0].get('targets'), list):
        raise ValueError('Batch has no valid scoped retry context')
    context = records[0]
    UUID(context['source_batch_id'])
    for target in context['targets']:
        UUID(target['tour_id'])
        for run_id in target['source_run_ids']:
            UUID(run_id)
    return context


def _run_report(storage, run_id):
    run = storage.get_run(run_id)
    snapshot = storage.get_snapshot(run_id)
    documents = storage.load_raw(run_id)
    report = {'run_id': run_id, 'capture_status': run['capture_status'], 'parse_status': run['parse_status'],
              'started_at': run['started_at'], 'finished_at': run['finished_at'],
              'snapshot_id': snapshot.id if snapshot else None,
              'snapshot_status': snapshot.status if snapshot else None,
              'coverage': snapshot.coverage if snapshot else {},
              'findings': classify(run, snapshot, documents),
              'raw': [{'id': d.id, 'sha256': d.sha256, 'kind': d.kind, 'role': d.role,
                       'url': safe_url(d.url), 'method': d.method, 'http_status': d.http_status,
                       'captured_at': d.captured_at} for d in documents],
              'observations': run['manifest'].get('diagnostics', {}),
              'actions': run['manifest'].get('actions', {}),
              'collector_version': run['collector_version'],
              'parser_version': snapshot.parser_version if snapshot else None,
              'warning_count': len(snapshot.warnings) if snapshot else 0,
              'error_count': len(run['errors']),
              'error_locators': [{'run_id': run_id, 'error_index': i,
                                  'stage': 'parse' if isinstance(e, dict) and e.get('stage') == 'parse' else 'capture_or_legacy',
                                  'text_policy': 'Original retained in DB; arbitrary exception text not shared'}
                                 for i, e in enumerate(run['errors'])]}
    return report, documents


def _report_state(storage, batch_id):
    batch = storage.get_batch(batch_id)
    context = _context(batch)
    rows = {r['tour_id']: r for r in storage.batch_tours(batch_id)}
    targets, documents = [], []
    for original in context['targets']:
        target = dict(original)
        row = rows.get(target['tour_id'])
        target['retry_run_ids'] = row['attempt_run_ids'] if row else []
        target['retry_outcome'] = row['outcome'] if row else 'not_attempted'
        result_row = row
        if row and row['outcome'] == 'merged':
            result_row = next((r for r in rows.values() if r['frozen_state'] and
                               set(row['attempt_run_ids']) & set(r['attempt_run_ids'])), None)
        target['published_tour_id'] = result_row['tour_id'] if result_row else None
        target['display_after'] = result_row['display_snapshot_id'] if result_row else None
        # Frozen batch refs, not the latest failed snapshot or a later catalogue check.
        target['accepted_after'] = (result_row['frozen_state']['current_snapshot_id']
                                    if result_row and result_row['frozen_state'] else None)
        target['runs'] = []
        for rid in dict.fromkeys(target['source_run_ids'] + target['retry_run_ids']):
            report, raw = _run_report(storage, rid)
            target['runs'].append(report)
            documents.extend(raw)
        if not target['retry_run_ids'] and not target['skip_reason']:
            target['skip_reason'] = 'not_attempted_budget' if batch['stage'] == 'frozen' else 'pending'
        targets.append(target)
    return {'batch_id': batch_id, 'source_batch_id': context['source_batch_id'], 'status': batch['status'],
            'finished': batch['stage'] == 'frozen', 'config': context['config'], 'targets': targets,
            'export_status': batch['export_status'], 'export_error_count': len(batch['export_errors'])}, documents


def retry_report(storage, batch_id, output_dir):
    batch_id = str(UUID(batch_id))
    # A fresh read-only repeatable-read transaction; never parse/freeze/recover/export.
    with storage.connection.transaction():
        storage.connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        payload, documents = _report_state(storage, batch_id)
    path = write_report(Path(output_dir) / 'diagnostics' / 'retry' / batch_id, payload, documents)
    return path, payload['finished']


def retry_domestic(config, storage, source_batch_id, urls, *, collector_class=DiagnosticCollector, clock=now):
    storage.preflight_incremental()
    with storage.crawl_lock() as acquired:
        if not acquired:
            return BatchResult(None, 'busy')
        targets = select_targets(storage, source_batch_id, urls)
        source = storage.get_batch(source_batch_id)
        context = {'schema': CONTEXT, 'message': LIMITATION, 'source_batch_id': source['id'],
                   'source_finished_at': source['finished_at'], 'targets': targets,
                   'config': {k: getattr(config, k) for k in CONFIG_FIELDS}, 'selected_at': clock()}
        if all(t['skip_reason'] for t in targets):
            path = write_report(Path(config.export_dir) / 'diagnostics' / 'retry' / 'invocations' / uid(),
                                {'status': 'handled', 'targets': targets, 'context': context})
            return BatchResult(None, 'handled', paths={'report': path})
        # Validate every input before creating artifacts or any business rows.
        private_dir(Path(config.export_dir) / 'diagnostics' / 'retry')
        with storage.connection.transaction():
            batch_id = storage.create_batch()
            storage.update_batch(batch_id, stage='details', errors=[context])
        print(f'batch={batch_id} source_batch={source["id"]}', flush=True)
        deadline = time.monotonic() + min(config.batch_timeout, len(targets) * config.run_timeout)
        wall_deadline = time.time() + min(config.batch_timeout, len(targets) * config.run_timeout)
        pacer = RequestPacer(config.request_interval)
        attempted = set()
        interrupted = False
        fatal = None
        try:
            for target in targets:
                if target['skip_reason']:
                    continue
                current = _resolve(storage, target['tour_id'])
                if current['id'] != target['tour_id']:
                    raise ValueError('Identity changed during scoped retry')
                skip = _skip(storage, current, source['finished_at'])
                if skip or current['id'] in attempted:
                    continue
                remaining = min(deadline-time.monotonic(), wall_deadline-time.time())
                if remaining <= 0:
                    break
                with storage.connection.transaction():
                    run_id = storage.create_run(target['effective_url'])
                    storage.update_run(run_id, manifest={'retry_scope': {'aliases': target['aliases'],
                                                                        'tour_id': target['tour_id']}})
                    storage.begin_attempt(batch_id, target['tour_id'], run_id, 'retry')
                attempted.add(target['tour_id'])
                print(f'batch={batch_id} run={run_id} tour={target["tour_id"]}', flush=True)
                collect_run(replace(config, tour_url=target['effective_url'],
                                    run_timeout=min(config.run_timeout, remaining)), storage, run_id,
                            collector_class=collector_class, pacer=pacer)
                published_id = complete_retry(storage, batch_id, target, run_id, attempted=attempted,
                                              selected={t['tour_id']: t for t in targets})
                if published_id is not None:
                    attempted.add(published_id)
        except KeyboardInterrupt:
            interrupted = True
        except BaseException as exc:
            fatal = exc
        # Terminalize only this invocation. No recovery of older pending batches.
        for row in storage.batch_tours(batch_id):
            if row['outcome'] == 'pending' and row['attempt_run_ids']:
                rid = row['attempt_run_ids'][-1]
                complete_retry_stopped(storage, batch_id, row['tour_id'], rid, clock=clock)
        errors = [context, LIMITATION]
        if interrupted or fatal:
            errors.append('Scoped retry stopped; committed checkpoints retained')
        batch = storage.freeze_batch(batch_id, 'partial', errors)
        paths = {}
        export_failure = None
        try:
            paths = _export(storage, batch_id, config.export_dir)
        except Exception as exc:
            export_failure = exc
        try:
            path, _ = retry_report(storage, batch_id, config.export_dir)
            paths['report'] = path
        except Exception:
            print(f'batch={batch_id} report failed; use retry-report --batch-id {batch_id}', flush=True)
            raise
        if interrupted:
            raise KeyboardInterrupt
        if fatal:
            raise fatal
        if export_failure:
            raise export_failure
        return BatchResult(batch_id, 'partial', batch['counts'], paths)
