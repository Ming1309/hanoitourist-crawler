from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from hanoitourist_crawler.config import Config
from hanoitourist_crawler.models import BatchResult
from hanoitourist_crawler.workflows.scheduler import latest_slot, schedule


def test_vietnam_midnight_in_utc():
    at = datetime(2026,10,6,17,0,tzinfo=timezone.utc)
    assert latest_slot(at) == at
    assert latest_slot(at.replace(hour=16)) == datetime(2026,10,5,17,tzinfo=timezone.utc)


def test_changed_hour():
    at = datetime(2026,10,6,17,0,tzinfo=timezone.utc)
    assert latest_slot(at,'01:00') == datetime(2026,10,5,18,tzinfo=timezone.utc)


def test_naive_clock_rejected():
    with pytest.raises(ValueError): latest_slot(datetime(2026,1,1))


def test_restart_sleep_reopens_connection_and_catches_only_latest(capsys):
    calls, closed = [], []
    ticks = iter([datetime(2026,10,6,18,tzinfo=timezone.utc), datetime(2026,10,10,18,tzinfo=timezone.utc)])
    def factory(url):
        return SimpleNamespace(close=lambda:closed.append(True))
    def runner(config, storage, scheduled_for):
        calls.append(scheduled_for)
        return BatchResult('batch', 'complete')
    def sleep(seconds):
        if len(calls) == 2: raise KeyboardInterrupt
    assert schedule(Config(), clock=lambda:next(ticks), sleeper=sleep, storage_factory=factory, runner=runner) == 130
    assert len(calls) == len(closed) == 2
    assert calls[-1] == datetime(2026,10,10,17,tzinfo=timezone.utc)


def test_failure_does_not_log_secret(capsys):
    def factory(url): raise RuntimeError('secret-password')
    def stop(seconds): raise KeyboardInterrupt
    assert schedule(Config(), storage_factory=factory, sleeper=stop) == 130
    assert 'secret-password' not in capsys.readouterr().out
