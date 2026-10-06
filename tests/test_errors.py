from datetime import datetime

import pytest

from hanoitourist_crawler.errors import capture_errors, error_message, parse_failure


def test_parse_failure_is_safe_and_versioned():
    error = parse_failure('0.1.2', ValueError('secret-password'))
    assert error['stage'] == 'parse' and error['parser_version'] == '0.1.2'
    assert error['error_type'] == 'ValueError'
    assert datetime.fromisoformat(error['occurred_at']).utcoffset() is not None
    assert 'secret-password' not in str(error)
    assert capture_errors([error, 'Parse failed (ValueError), parser=old-parser']) == []


@pytest.mark.parametrize('error', [
    'robots denied', 'network parse failed', 'Parse failed (ValueError), parser=',
    {'stage': 'parse'}, {'stage': 'unknown', 'message': 'unknown stage'},
    {'stage': 'capture', 'message': 'capture issue'},
    {'stage': 'parse', 'parser_version': 'old', 'error_type': 'ValueError',
     'occurred_at': 'invalid', 'message': 'invalid record'}, None,
])
def test_unknown_errors_are_not_ignored(error):
    assert capture_errors([error]) == [error_message(error)]


def test_mixed_errors():
    assert capture_errors(['robots denied', parse_failure('old', ValueError()),
                           'Parse failed (ValueError), parser=old',
                           {'stage': 'capture', 'message': 'request failed'}]) == ['robots denied', 'request failed']
