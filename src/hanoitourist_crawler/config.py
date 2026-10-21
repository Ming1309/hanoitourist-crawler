"""Validated CLI > environment > default configuration."""

import os
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

DEFAULT_TOUR_URL = "https://hanoitourist.vn/kham-pha-phu-quoc-hon-ngoc-vien-dong"


@dataclass(frozen=True)
class Config:
    database_url: str = ""
    tour_url: str = DEFAULT_TOUR_URL
    export_dir: Path = Path("exports")
    headless: bool = True
    navigation_timeout: float = 30.0
    run_timeout: float = 300.0
    max_states: int = 100
    max_pages: int = 50
    max_document_bytes: int = 5 * 1024 * 1024
    max_run_bytes: int = 50 * 1024 * 1024
    request_interval: float = 1.0
    max_retries: int = 2
    user_agent: str = "HanoitouristResearchCrawler/0.1"
    domestic_listing_url: str = 'https://hanoitourist.vn/tour-trong-nuoc'
    recheck_days: int = 7
    discovery_timeout: float = 300.0
    max_listing_pages: int = 50
    batch_timeout: float = 21600.0
    schedule_time: str = '00:00'
    schedule_timezone: str = 'Asia/Ho_Chi_Minh'
    scheduler_poll_interval: float = 60.0

    @classmethod
    def load(cls, overrides=None, *, dotenv=True):
        if dotenv:
            load_dotenv()
        defaults = cls()
        values = {}
        for field in fields(cls):
            default = getattr(defaults, field.name)
            key = "EXPORT_DIR" if field.name == "export_dir" else field.name.upper()
            value = os.environ.get(key, default)
            if overrides and overrides.get(field.name) is not None:
                value = overrides[field.name]
            if isinstance(default, bool):
                if str(value).lower() not in {"true", "false", "1", "0"}:
                    raise ValueError(f"Invalid {key}")
                value = str(value).lower() in {"true", "1"}
            elif isinstance(default, Path):
                value = Path(value)
            elif isinstance(default, (int, float)):
                value = type(default)(value)
            values[field.name] = value
        result = cls(**values)
        result.validate()
        return result

    def validate(self):
        if not self.database_url:
            raise ValueError("DATABASE_URL is required; configure PostgreSQL before running")
        url = urlsplit(self.tour_url)
        if (url.scheme != "https" or url.netloc != "hanoitourist.vn"
                or url.path.rstrip("/") != urlsplit(DEFAULT_TOUR_URL).path
                or url.query or url.fragment):
            raise ValueError("TOUR_URL must be the scoped HTTPS Hanoitourist tour URL")
        for name in ("navigation_timeout", "run_timeout", "max_states", "max_pages",
                     "max_document_bytes", "max_run_bytes", "recheck_days", "discovery_timeout",
                     "max_listing_pages", "batch_timeout", "scheduler_poll_interval"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not math.isfinite(self.request_interval) or self.request_interval < 0 or not 0 <= self.max_retries <= 2:
            raise ValueError("Invalid request interval or retry limit")
        if not self.user_agent.strip():
            raise ValueError("USER_AGENT must not be empty")
        if self.domestic_listing_url != 'https://hanoitourist.vn/tour-trong-nuoc':
            raise ValueError('DOMESTIC_LISTING_URL must be the domestic scope root')
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', self.schedule_time):
            raise ValueError('SCHEDULE_TIME must be HH:MM')
        try:
            ZoneInfo(self.schedule_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Invalid SCHEDULE_TIMEZONE') from None
