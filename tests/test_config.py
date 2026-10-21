from pathlib import Path

import pytest
from hanoitourist_crawler.config import Config


def test_precedence(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://local/example")
    monkeypatch.setenv("EXPORT_DIR", "from-env")
    config = Config.load({"export_dir": "from-cli"}, dotenv=False)
    assert config.export_dir == Path("from-cli")
    assert config.max_states == 100


def test_missing_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        Config.load(dotenv=False)


@pytest.mark.parametrize("overrides", [
    {"tour_url": "http://hanoitourist.vn/tour-trong-nuoc"},
    {"tour_url": "https://example.com/tour"},
    {"max_states": 0}, {"headless": "maybe"}, {"max_retries": 3},
    {"run_timeout": "nan"}, {"request_interval": "inf"},
])
def test_invalid_config(overrides):
    with pytest.raises(ValueError):
        Config.load({"database_url": "postgresql://local/example", **overrides}, dotenv=False)


@pytest.mark.parametrize('overrides', [
    {'recheck_days':0}, {'batch_timeout':'nan'}, {'discovery_timeout':0}, {'max_listing_pages':0},
    {'schedule_time':'24:00'}, {'schedule_time':'0:00'}, {'schedule_timezone':'not/a-zone'},
    {'scheduler_poll_interval':0}, {'domestic_listing_url':'https://hanoitourist.vn/tour-nuoc-ngoai'},
])
def test_incremental_configuration_invalid(overrides):
    with pytest.raises(ValueError):
        Config.load({'database_url':'unused', **overrides}, dotenv=False)


def test_incremental_defaults_and_override(monkeypatch):
    monkeypatch.setenv('SCHEDULE_TIME','03:15')
    cfg=Config.load({'database_url':'unused','schedule_time':'01:30'},dotenv=False)
    assert cfg.recheck_days==7 and cfg.schedule_time=='01:30'
    assert cfg.schedule_timezone=='Asia/Ho_Chi_Minh' and cfg.max_listing_pages==50
