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
