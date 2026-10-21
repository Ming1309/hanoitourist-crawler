"""Atomic, human-readable exports from persisted database snapshots."""

import json
import os
import tempfile
from pathlib import Path

from ..models import json_value


def export_run(storage, run_id, output_dir, version=None):
    run = storage.get_run(run_id)
    snapshot = storage.get_snapshot(run_id, version)
    if version and snapshot is None:
        raise ValueError('Requested parser version not found')
    documents = storage.load_raw(run_id)
    payload = {'run': json_value(run), 'raw_manifest': [
        {k: v for k, v in json_value(d).items() if k != 'body'} for d in documents]}
    if snapshot:
        data = json_value(snapshot)
        grouped = {}
        for observation in data.pop('departures'):
            grouped.setdefault(observation['source_schedule_id'], []).append(observation)
        data['departures'] = [{'source_schedule_id': sid, 'observations': values} for sid, values in grouped.items()]
        payload['snapshot'] = data
    else:
        payload['snapshot'] = None
    directory = Path(output_dir) / run_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ((snapshot.parser_version if snapshot else 'report') + '.json')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, delete=False) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return path, snapshot.status if snapshot else 'failed'
