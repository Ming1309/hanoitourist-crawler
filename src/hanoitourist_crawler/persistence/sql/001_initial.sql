CREATE TABLE IF NOT EXISTS crawl_runs (
 id UUID PRIMARY KEY, requested_url TEXT NOT NULL, final_url TEXT,
 started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ,
 collector_version TEXT NOT NULL,
 capture_status TEXT NOT NULL CHECK (capture_status IN ('running','complete','partial','failed')),
 parse_status TEXT NOT NULL DEFAULT 'pending' CHECK (parse_status IN ('pending','complete','partial','failed')),
 manifest JSONB NOT NULL DEFAULT '{}', coverage JSONB NOT NULL DEFAULT '{}', errors JSONB NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS crawl_runs_url_time ON crawl_runs(requested_url,started_at);
CREATE TABLE IF NOT EXISTS raw_documents (
 id UUID PRIMARY KEY, run_id UUID NOT NULL REFERENCES crawl_runs(id), sequence INTEGER NOT NULL,
 kind TEXT NOT NULL CHECK (kind IN ('http_document','dom','ajax','robots')),
 role TEXT NOT NULL, state_key TEXT NOT NULL, url TEXT NOT NULL, method TEXT NOT NULL,
 http_status INTEGER, content_type TEXT NOT NULL, encoding TEXT NOT NULL,
 headers JSONB NOT NULL, captured_at TIMESTAMPTZ NOT NULL, body BYTEA NOT NULL, sha256 TEXT NOT NULL,
 UNIQUE(run_id,sequence)
);
CREATE TABLE IF NOT EXISTS tour_snapshots (
 id UUID PRIMARY KEY, run_id UUID NOT NULL REFERENCES crawl_runs(id), parser_version TEXT NOT NULL,
 parsed_at TIMESTAMPTZ NOT NULL, source_tour_id TEXT NOT NULL, canonical_url TEXT NOT NULL, title TEXT NOT NULL,
 duration_text TEXT NOT NULL, duration_days INTEGER, duration_nights INTEGER, origin_text TEXT NOT NULL,
 promotional_price_raw TEXT NOT NULL, promotional_price NUMERIC(18,2), promotional_currency TEXT,
 seats_raw TEXT NOT NULL, seats INTEGER, review_summary TEXT NOT NULL, sidebar_text TEXT NOT NULL,
 status TEXT NOT NULL CHECK (status IN ('complete','partial')), coverage JSONB NOT NULL,
 warnings JSONB NOT NULL, provenance JSONB NOT NULL, UNIQUE(run_id,parser_version)
);
CREATE INDEX IF NOT EXISTS tour_snapshots_run ON tour_snapshots(run_id,parsed_at);
CREATE TABLE IF NOT EXISTS tour_sections (
 id UUID PRIMARY KEY, snapshot_id UUID NOT NULL REFERENCES tour_snapshots(id),
 key TEXT NOT NULL, ordinal INTEGER NOT NULL, title TEXT NOT NULL, content_html TEXT NOT NULL,
 content_text TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('present','empty','missing')), provenance JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS tour_sections_snapshot ON tour_sections(snapshot_id);
CREATE TABLE IF NOT EXISTS itinerary_days (
 id UUID PRIMARY KEY, snapshot_id UUID NOT NULL REFERENCES tour_snapshots(id), ordinal INTEGER NOT NULL,
 label TEXT NOT NULL, title TEXT NOT NULL, content_html TEXT NOT NULL, content_text TEXT NOT NULL,
 transport_labels JSONB NOT NULL, provenance JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS itinerary_days_snapshot ON itinerary_days(snapshot_id);
CREATE TABLE IF NOT EXISTS tour_media (
 id UUID PRIMARY KEY, snapshot_id UUID NOT NULL REFERENCES tour_snapshots(id), role TEXT NOT NULL,
 url TEXT NOT NULL, original_url TEXT NOT NULL, alt TEXT NOT NULL, caption TEXT NOT NULL,
 ordinal INTEGER NOT NULL, section_key TEXT, day_ordinal INTEGER, provenance JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS tour_media_snapshot ON tour_media(snapshot_id);
CREATE TABLE IF NOT EXISTS departure_observations (
 id UUID PRIMARY KEY, snapshot_id UUID NOT NULL REFERENCES tour_snapshots(id), source_schedule_id TEXT NOT NULL,
 booking_href TEXT NOT NULL, departure_raw TEXT NOT NULL, departure_at TIMESTAMPTZ,
 seats_raw TEXT NOT NULL, seats INTEGER, price_from_raw TEXT NOT NULL, price_from NUMERIC(18,2),
 currency TEXT, features TEXT NOT NULL, state_key TEXT NOT NULL, observed_at TIMESTAMPTZ NOT NULL,
 provenance JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS departures_snapshot_schedule ON departure_observations(snapshot_id,source_schedule_id);
CREATE TABLE IF NOT EXISTS price_cells (
 id UUID PRIMARY KEY, departure_id UUID NOT NULL REFERENCES departure_observations(id),
 row_index INTEGER NOT NULL, column_index INTEGER NOT NULL, price_label TEXT NOT NULL, age_label TEXT,
 raw_value TEXT NOT NULL, amount NUMERIC(18,2), currency TEXT,
 rowspan INTEGER NOT NULL CHECK(rowspan > 0), colspan INTEGER NOT NULL CHECK(colspan > 0),
 applicable_labels JSONB NOT NULL, provenance JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS price_cells_departure ON price_cells(departure_id);
