"""Orchestration; all parsing/replay is performed from committed staging."""

from . import PARSER_VERSION
from .collector import Collector
from .errors import parse_failure
from .parser import parse
from .validation import validate


def parse_run(storage, run_id, *, version=PARSER_VERSION):
    existing = storage.get_snapshot(run_id, version)
    if existing:
        return existing
    run = storage.get_run(run_id)
    documents = storage.load_raw(run_id)
    try:
        snapshot = validate(parse(documents, run_id, version=version), documents, run)
        snapshot = storage.persist_snapshot(snapshot)
        storage.update_run(run_id, parse_status=snapshot.status, coverage=snapshot.coverage)
        return snapshot
    except Exception as exc:
        errors = list(run.get('errors', [])) + [parse_failure(version, exc)]
        storage.update_run(run_id, parse_status='failed', errors=errors)
        raise


def crawl(config, storage, *, collector_class=Collector):
    storage.preflight()
    run_id = storage.create_run(config.tour_url)
    collector = collector_class(config, storage, run_id)
    collector.collect()
    if storage.get_run(run_id)['capture_status'] != 'failed':
        try:
            parse_run(storage, run_id)
        except Exception:
            # Error is recorded in the run; CLI still exports the failure report.
            pass
    return run_id
