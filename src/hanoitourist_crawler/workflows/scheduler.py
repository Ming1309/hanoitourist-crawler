"""Daily wall-clock scheduling with one durable nearest-slot catch-up."""

import signal
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .incremental import crawl_domestic
from ..models import now
from ..persistence.storage import Storage


def latest_slot(at, schedule_time='00:00', schedule_timezone='Asia/Ho_Chi_Minh'):
    if at.utcoffset() is None:
        raise ValueError('Scheduler clock must be timezone aware')
    zone = ZoneInfo(schedule_timezone)
    local = at.astimezone(zone)
    hour, minute = map(int, schedule_time.split(':'))
    slot = datetime(local.year, local.month, local.day, hour, minute, tzinfo=zone)
    if slot > local:
        slot -= timedelta(days=1)
    return slot.astimezone(timezone.utc)


def schedule(config, *, clock=now, sleeper=time.sleep, storage_factory=Storage, runner=crawl_domestic):
    previous = signal.getsignal(signal.SIGTERM)

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    failures = 0
    try:
        while True:
            storage = None
            try:
                storage = storage_factory(config.database_url)
                slot = latest_slot(clock(), config.schedule_time, config.schedule_timezone)
                result = runner(config, storage, scheduled_for=slot)
                failures = 0
                if result.batch_id or result.status == 'busy':
                    print(f'schedule slot={slot.isoformat()} batch={result.batch_id or "none"} status={result.status}', flush=True)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                failures = min(failures + 1, 5)
                print(f'Scheduler operation failed ({type(exc).__name__}); credentials are not logged.', flush=True)
            finally:
                if storage:
                    storage.close()
            sleeper(min(300, config.scheduler_poll_interval * (2 ** failures)))
    except KeyboardInterrupt:
        print('Scheduler stopped; committed checkpoints retained.', flush=True)
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)
