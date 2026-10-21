"""Orchestration; all parsing/replay is performed from committed staging."""

from .. import PARSER_VERSION
from ..acquisition.collector import Collector
from ..errors import parse_failure
from ..processing.parser import parse
from ..processing.validation import validate


def parse_run(storage, run_id, *, version=PARSER_VERSION):
    existing = storage.get_snapshot(run_id, version)
    if existing:
        return existing
    run = storage.get_run(run_id)
    documents = storage.load_raw(run_id)
    try:
        from ..reporting.diagnostics import phase
        with phase(storage, run_id, 'parse'):
            snapshot = parse(documents, run_id, version=version)
        with phase(storage, run_id, 'validation'):
            snapshot = validate(snapshot, documents, run)
        with phase(storage, run_id, 'persist'):
            snapshot = storage.persist_snapshot(snapshot)
        storage.update_run(run_id, parse_status=snapshot.status, coverage=snapshot.coverage)
        return snapshot
    except Exception as exc:
        errors = list(run.get('errors', [])) + [parse_failure(version, exc)]
        storage.update_run(run_id, parse_status='failed', errors=errors)
        raise


class CrawlBusy(RuntimeError):
    pass


def crawl(config, storage, *, collector_class=Collector):
    storage.preflight()
    with storage.crawl_lock() as acquired:
        if not acquired:
            raise CrawlBusy('Another crawl is running')
        run_id = storage.create_run(config.tour_url)
        return capture_run(config, storage, run_id, collector_class=collector_class)


def collect_run(config, storage, run_id, *, collector_class=Collector, pacer=None):
    """Acquire durable staging only; caller owns the lock and run checkpoint."""
    collector = (collector_class(config, storage, run_id, pacer=pacer) if pacer is not None
                 else collector_class(config, storage, run_id))
    collector.collect()
    return run_id


def capture_run(config, storage, run_id, *, collector_class=Collector, pacer=None):
    """Legacy standalone acquisition and replay, without catalogue publication."""
    collect_run(config, storage, run_id, collector_class=collector_class, pacer=pacer)
    if storage.get_run(run_id)['capture_status'] != 'failed':
        try:
            parse_run(storage, run_id)
        except Exception:
            # Error is recorded in the run; CLI still exports the failure report.
            pass
    return run_id
