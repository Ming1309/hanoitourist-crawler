"""Complete committed detail captures without selecting or acquiring work.

Callers own the crawl lock and checkpoint the attempt before acquisition. Daily
completion preserves separate parse/publication commits; scoped retry publishes
inside the snapshot transaction; recovery only uses already committed evidence.
Catalogue identity binding and accepted-baseline updates remain in Storage.
"""

import psycopg

from . import pipeline
from ..reporting.diagnostics import observe, phase
from ..models import now
from ..acquisition.scope import tour_url


def complete_daily(storage, batch_id, tour_id, run_id):
    """Return the published identity, or None after a recorded identity rejection."""
    if storage.get_run(run_id)['capture_status'] != 'failed':
        try:
            pipeline.parse_run(storage, run_id)
        except Exception:
            # Preserve daily behavior: parse_run records the failure independently.
            pass
    try:
        return storage.apply_attempt(batch_id, tour_id, run_id)
    except ValueError:
        storage.update_batch_tour(batch_id, tour_id, outcome='failed',
                                  errors=['Identity publication failed; baseline retained'])
        storage._update_tour(tour_id, requires_refresh=True)
        return None


def complete_retry(storage, batch_id, target, run_id, *, attempted, selected):
    """Atomically parse and publish within the selected, verified retry identities.

    attempted and selected describe the caller's selection; this module never
    expands or mutates them. Return the published identity, or None on rejection.
    """
    with storage.connection.transaction():
        if storage.get_run(run_id)['capture_status'] != 'failed':
            try:
                pipeline.parse_run(storage, run_id)
            except psycopg.Error:
                raise
            except Exception:
                # Typed parse failure is durable only if this transaction commits.
                pass
        with phase(storage, run_id, 'publication'):
            snapshot = storage.get_snapshot(run_id)
            run = storage.get_run(run_id)
            try:
                if snapshot and snapshot.status == 'complete' and run['capture_status'] == 'complete':
                    tour = storage.get_tour(target['tour_id'])
                    canonical = tour_url(snapshot.canonical_url)
                    match = storage.get_tour_by_source_id(snapshot.source_tour_id)
                    identity = selected.get(match['id']) if match else target
                    if (identity is None or canonical not in identity['aliases'] or
                            (tour['source_tour_id'] and tour['source_tour_id'] != snapshot.source_tour_id) or
                            (match and match['id'] != target['tour_id'] and match['id'] in attempted)):
                        raise ValueError('Publication outside verified identity')
                return storage.apply_attempt(batch_id, target['tour_id'], run_id)
            except ValueError:
                storage.update_batch_tour(batch_id, target['tour_id'], outcome='failed',
                                          errors=['Scoped identity publication rejected; baseline retained'])
                storage._update_tour(target['tour_id'], requires_refresh=True)
                manifest = storage.get_run(run_id)['manifest']
                observe(manifest, 'identity_scope', 'failed')
                storage.update_run(run_id, manifest=manifest)
                return None


def _complete_stopped(storage, batch_id, tour_id, run_id, *, clock, capture_error, attempt_error):
    run = storage.get_run(run_id)
    if run['capture_status'] == 'running':
        storage.update_run(run_id, capture_status='failed', finished_at=clock(),
                           errors=run['errors'] + [capture_error])
    storage.update_batch_tour(batch_id, tour_id, outcome='failed', errors=[attempt_error])
    storage._update_tour(tour_id, requires_refresh=True)


def complete_daily_stopped(storage, batch_id, tour_id, run_id, *, clock=now):
    """Fail this invocation's stopped daily attempt without parsing or publication."""
    _complete_stopped(storage, batch_id, tour_id, run_id, clock=clock,
                      capture_error='Daily crawl interrupted; checkpoints retained',
                      attempt_error='Daily crawl stopped before publication')


def complete_retry_stopped(storage, batch_id, tour_id, run_id, *, clock=now):
    """Fail a pending retry selected by its caller; never parse or publish.

    The caller holds the crawl lock and supplies its latest attempt reference.
    Preserve independent checkpoint writes rather than introducing a transaction.
    """
    _complete_stopped(storage, batch_id, tour_id, run_id, clock=clock,
                      capture_error='Scoped retry interrupted; checkpoints retained',
                      attempt_error='Scoped retry stopped before atomic publication')


def complete_recovery(storage, batch_id, tour_id, run_id):
    """Terminalize an abandoned capture and publish committed evidence; never parse."""
    run = storage.get_run(run_id)
    if run['capture_status'] == 'running':
        storage.update_run(run_id, capture_status='partial' if storage.load_raw(run_id) else 'failed',
                           finished_at=now(), errors=run['errors'] + ['Abandoned capture; checkpoints retained'])
    try:
        return storage.apply_attempt(batch_id, tour_id, run_id)
    except ValueError:
        storage.update_batch_tour(batch_id, tour_id, outcome='failed',
                                  errors=['Abandoned identity publication failed; baseline retained'])
        return None
