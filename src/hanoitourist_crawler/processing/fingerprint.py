"""Versioned business-content fingerprint; capture metadata is not a change."""

import json
from dataclasses import fields
from datetime import datetime, timezone
from hashlib import sha256

from ..models import json_value
from .parser import _content_signature

FINGERPRINT_VERSION = 'v1'
METADATA = {'id', 'run_id', 'parser_version', 'parsed_at', 'observed_at', 'provenance',
            'state_key', 'coverage', 'warnings', 'status'}


def _canonical(value):
    if hasattr(value, '__dataclass_fields__'):
        result = {f.name: _canonical(getattr(value, f.name)) for f in fields(value) if f.name not in METADATA}
        return result
    if isinstance(value, list):
        values = [_canonical(item) for item in value]
        encoded = {json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':')): item for item in values}
        return [encoded[key] for key in sorted(encoded)]
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return json_value(value)


def fingerprint(snapshot):
    projection = _canonical(snapshot)
    for attr in ('sections', 'days'):
        items = []
        for item in getattr(snapshot, attr):
            data = _canonical(item)
            data['content_html'] = _content_signature(item.content_html, snapshot.canonical_url)[0]
            items.append(data)
        projection[attr] = _canonical(items)
    encoded = json.dumps({'version': FINGERPRINT_VERSION, 'content': projection},
                         ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return sha256(encoded).hexdigest()
