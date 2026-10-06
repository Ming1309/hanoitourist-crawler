"""Shared typed contracts; amounts are Decimal, times are timezone aware."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4


def uid() -> str:
    return str(uuid4())


def now() -> datetime:
    return datetime.now(timezone.utc)


def json_value(value):
    if hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, ".2f")
    return value


@dataclass
class RawDocument:
    run_id: str
    sequence: int
    kind: str
    role: str
    state_key: str
    url: str
    body: bytes
    id: str = field(default_factory=uid)
    method: str = "GET"
    http_status: int | None = None
    content_type: str = "text/html"
    encoding: str = "utf-8"
    headers: dict = field(default_factory=dict)
    captured_at: datetime = field(default_factory=now)
    sha256: str = ""

    def __post_init__(self):
        self.sha256 = sha256(self.body).hexdigest()


@dataclass
class Section:
    key: str
    ordinal: int
    title: str
    content_html: str
    content_text: str
    state: str
    provenance: list
    id: str = field(default_factory=uid)


@dataclass
class Day:
    ordinal: int
    label: str
    title: str
    content_html: str
    content_text: str
    transport_labels: list
    provenance: list
    id: str = field(default_factory=uid)


@dataclass
class Media:
    role: str
    url: str
    original_url: str
    alt: str
    caption: str
    ordinal: int
    section_key: str | None
    day_ordinal: int | None
    provenance: list
    id: str = field(default_factory=uid)


@dataclass
class PriceCell:
    row_index: int
    column_index: int
    price_label: str
    age_label: str | None
    raw_value: str
    amount: Decimal | None
    currency: str | None
    rowspan: int
    colspan: int
    applicable_labels: list
    provenance: list
    id: str = field(default_factory=uid)


@dataclass
class Departure:
    source_schedule_id: str
    booking_href: str
    departure_raw: str
    departure_at: datetime | None
    seats_raw: str
    seats: int | None
    price_from_raw: str
    price_from: Decimal | None
    currency: str | None
    features: str
    state_key: str
    observed_at: datetime
    provenance: list
    price_cells: list[PriceCell] = field(default_factory=list)
    id: str = field(default_factory=uid)


@dataclass
class Snapshot:
    run_id: str
    parser_version: str
    title: str = ""
    canonical_url: str = ""
    source_tour_id: str = ""
    duration_text: str = ""
    duration_days: int | None = None
    duration_nights: int | None = None
    origin_text: str = ""
    promotional_price_raw: str = ""
    promotional_price: Decimal | None = None
    promotional_currency: str | None = None
    seats_raw: str = ""
    seats: int | None = None
    review_summary: str = ""
    sidebar_text: str = ""
    status: str = "partial"
    coverage: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    sections: list[Section] = field(default_factory=list)
    days: list[Day] = field(default_factory=list)
    media: list[Media] = field(default_factory=list)
    departures: list[Departure] = field(default_factory=list)
    id: str = field(default_factory=uid)
    parsed_at: datetime = field(default_factory=now)
