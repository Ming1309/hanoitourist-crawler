"""Domestic incremental state machine with committed per-tour checkpoints."""

import time
from dataclasses import replace
from datetime import timedelta

from .attempts import complete_daily, complete_daily_stopped, complete_recovery
from ..acquisition.collector import Collector, RequestPacer
from ..config import DEFAULT_TOUR_URL
from ..acquisition.discovery import discover_domestic
from ..models import BatchResult, now
from .pipeline import collect_run
from ..acquisition.scope import tour_url


def selection_reason(tour, at, days, *, discovered, terminal):
    if not discovered and tour['availability'] == 'unavailable':
        return None
    if not discovered and terminal and tour['latest_attempt_run_id']:
        return 'missing_listing'
    # A budget cutoff can leave registered tours without any attempt to retry.
    if (tour['availability'] == 'pending' and not tour['current_snapshot_id']
            and not tour['latest_attempt_run_id'] and not tour['requires_refresh']):
        return 'new'
    if not discovered and not tour['requires_refresh']:
        return None
    if tour['requires_refresh']:
        return 'retry'
    if tour['availability'] == 'unavailable':
        return 'reactivated'
    if not tour['current_snapshot_id']:
        return 'new'
    checked = tour['last_successful_check_at']
    if checked is None or at >= checked + timedelta(days=days):
        return 'due'
    return None


def _export(storage, batch_id, output_dir):
    from ..reporting.excel import export_batch_excel
    try:
        return export_batch_excel(storage, batch_id, output_dir)
    except Exception as exc:
        storage.update_batch(batch_id, export_status='failed', export_errors=[f'Excel export failed ({type(exc).__name__})'])
        raise


def recover_batches(storage, output_dir, *, batch_id=None):
    # Caller owns the crawl lock; scoped repair must not recover other batches.
    for batch in storage.pending_batches():
        if batch_id is not None and batch['id'] != batch_id:
            continue
        if batch['status'] == 'running':
            for row in storage.batch_tours(batch['id']):
                if row['outcome'] != 'pending' or not row['attempt_run_ids']:
                    continue
                run_id = row['attempt_run_ids'][-1]
                complete_recovery(storage, batch['id'], row['tour_id'], run_id)
            if batch['discovery_run_id']:
                run = storage.get_run(batch['discovery_run_id'])
                if run['capture_status'] == 'running':
                    storage.update_run(run['id'], capture_status='partial', finished_at=now(),
                                       errors=run['errors'] + ['Abandoned discovery; checkpoints retained'])
            storage.freeze_batch(batch['id'], 'partial', batch['errors'] + ['Abandoned batch; committed results recovered'])
        try:
            _export(storage, batch['id'], output_dir)
        except Exception:
            # Failed export is a terminal diagnostic, not a reason to recrawl.
            pass


def crawl_domestic(config, storage, *, scheduled_for=None, discovery=discover_domestic,
                   collector_class=Collector, clock=now):
    storage.preflight_incremental()
    with storage.crawl_lock() as acquired:
        if not acquired:
            return BatchResult(None, 'busy')
        recover_batches(storage, config.export_dir)
        handled = storage.last_handled_slot()
        if scheduled_for is not None and handled is not None and scheduled_for <= handled:
            return BatchResult(None, 'handled')
        batch_id = storage.create_batch(scheduled_for)
        deadline = time.monotonic() + config.batch_timeout
        wall_deadline = time.time() + config.batch_timeout
        pacer = RequestPacer(config.request_interval)
        errors, seen, attempted = [], set(), set()
        interrupted = False
        status = 'failed'
        try:
            historical = storage.connection.execute('SELECT id FROM tour_snapshots WHERE canonical_url=%s LIMIT 1',
                                                     (DEFAULT_TOUR_URL,)).fetchone()
            if historical:
                storage.register_tour(DEFAULT_TOUR_URL, '', batch_id, listed=False)
            discovery_id = storage.create_run(config.domestic_listing_url)
            storage.update_batch(batch_id, discovery_run_id=discovery_id)
            budget = min(config.discovery_timeout, max(0.001, deadline - time.monotonic()))
            result = discovery(replace(config, discovery_timeout=budget), storage, discovery_id, pacer=pacer)
            errors.extend(result.errors)
            terminal = result.status == 'complete' and result.terminal
            storage.update_batch(batch_id, discovery_complete=terminal, stage='details')
            for card in result.cards:
                if card['identity_error']:
                    continue
                state = storage.register_tour(card['url'], card['title'], batch_id, card['source_tour_id'])
                seen.add(state['id'])

            def process(tour, reason):
                if tour['superseded_by_id'] or tour['id'] in attempted:
                    return
                remaining = min(deadline - time.monotonic(), wall_deadline - time.time())
                if remaining <= 0:
                    raise TimeoutError('Batch budget exceeded')
                requested = tour_url(tour['canonical_url'])
                run_id = storage.create_run(requested)
                storage.begin_attempt(batch_id, tour['id'], run_id, reason)
                attempted.add(tour['id'])
                collect_run(replace(config, tour_url=requested, run_timeout=min(config.run_timeout, remaining)),
                            storage, run_id, collector_class=collector_class, pacer=pacer)
                target_id = complete_daily(storage, batch_id, tour['id'], run_id)
                if target_id is None:
                    errors.append('Tour identity publication failed; baseline retained')
                else:
                    attempted.add(target_id)
                    if tour['id'] in seen:
                        seen.add(target_id)

            # Bind successful aliases before deciding which known tours are missing.
            for selected in storage.list_tours():
                tour = storage.get_tour(selected['id'])
                if tour['superseded_by_id'] or tour['id'] in attempted or tour['id'] not in seen:
                    continue
                reason = selection_reason(tour, clock(), config.recheck_days, discovered=True, terminal=terminal)
                if reason:
                    process(tour, reason)
                else:
                    row = next(r for r in storage.batch_tours(batch_id) if r['tour_id'] == tour['id'])
                    if row['outcome'] == 'pending' and not row['attempt_run_ids']:
                        storage.update_batch_tour(batch_id, tour['id'], outcome='skipped')
            for selected in storage.list_tours():
                tour = storage.get_tour(selected['id'])
                if tour['superseded_by_id'] or tour['id'] in seen or tour['id'] in attempted:
                    continue
                if terminal:
                    storage._update_tour(tour['id'], is_listed=False)
                reason = selection_reason(tour, clock(), config.recheck_days, discovered=False, terminal=terminal)
                if reason:
                    storage.ensure_batch_tour(batch_id, tour['id'])
                    process(tour, reason)
            rows = storage.batch_tours(batch_id)
            problems = any(r['outcome'] in {'failed', 'partial'} for r in rows)
            status = 'complete' if terminal and not problems and not errors else ('partial' if result.cards or rows else 'failed')
        except KeyboardInterrupt:
            interrupted = True
            errors.append('Interrupted batch; committed checkpoints retained')
            status = 'partial'
        except Exception as exc:
            errors.append(f'Batch stopped ({type(exc).__name__}); committed checkpoints retained')
            status = 'partial' if storage.batch_tours(batch_id) else 'failed'
        for row in storage.batch_tours(batch_id):
            if row['outcome'] == 'pending' and row['attempt_run_ids']:
                complete_daily_stopped(storage, batch_id, row['tour_id'], row['attempt_run_ids'][-1])
        batch = storage.freeze_batch(batch_id, status, errors)
        try:
            paths = _export(storage, batch_id, config.export_dir)
        except Exception:
            paths = {}
            status = 'failed'
        if interrupted:
            raise KeyboardInterrupt
        return BatchResult(batch_id, status, batch['counts'], paths)
