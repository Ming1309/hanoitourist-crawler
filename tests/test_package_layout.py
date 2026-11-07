"""Installed import and resource contracts for the responsibility-based layout."""

from importlib import import_module
from importlib.util import find_spec
from importlib.resources import files


def assert_group_layout(group, modules):
    for name in modules:
        module = import_module(f'hanoitourist_crawler.{group}.{name}')
        assert module.__name__ == f'hanoitourist_crawler.{group}.{name}'
        assert find_spec(f'hanoitourist_crawler.{name}') is None


def test_processing_layout():
    assert_group_layout('processing', ('parser', 'normalize', 'validation', 'fingerprint'))


def test_workflows_layout():
    assert_group_layout('workflows', ('incremental', 'retry', 'attempts', 'pipeline', 'scheduler'))


def test_acquisition_layout():
    assert_group_layout('acquisition', ('discovery', 'collector', 'navigation', 'scope', 'selectors'))


def test_reporting_layout():
    assert_group_layout('reporting', ('excel', 'export', 'diagnostics'))


def test_persistence_layout():
    assert_group_layout('persistence', ('storage',))


def test_sql_resources_follow_persistence_layout():
    root = files('hanoitourist_crawler.persistence').joinpath('sql')
    assert {p.name for p in root.iterdir()} == {'001_initial.sql'}
    assert 'CREATE TABLE IF NOT EXISTS' in root.joinpath('001_initial.sql').read_text()
