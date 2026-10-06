"""Classify run history without confusing old parse failures with capture issues."""

import re
from datetime import datetime

from .models import now

LEGACY_PARSE_FAILURE = r'Parse failed \([A-Za-z_]\w*\), parser=\S+'


def parse_failure(version, exc):
    error_type = type(exc).__name__
    return {'stage': 'parse', 'parser_version': version, 'error_type': error_type,
            'occurred_at': now().isoformat(),
            'message': f'Parse failed ({error_type}), parser={version}'}


def _is_parse_failure(error):
    if isinstance(error, str):
        return re.fullmatch(LEGACY_PARSE_FAILURE, error) is not None
    if not isinstance(error, dict) or error.get('stage') != 'parse':
        return False
    if not all(isinstance(error.get(k), str) and error[k]
               for k in ('parser_version', 'error_type', 'occurred_at', 'message')):
        return False
    try:
        return datetime.fromisoformat(error['occurred_at']).utcoffset() is not None
    except ValueError:
        return False


def error_message(error):
    if isinstance(error, str):
        return error
    if isinstance(error, dict) and isinstance(error.get('message'), str) and error['message']:
        return error['message']
    return 'Unclassified run error'


def capture_errors(errors):
    return [error_message(error) for error in errors if not _is_parse_failure(error)]
