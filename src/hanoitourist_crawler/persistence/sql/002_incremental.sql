CREATE TABLE IF NOT EXISTS domestic_batches (
 id UUID PRIMARY KEY, origin TEXT NOT NULL CHECK(origin IN ('manual','scheduled')),
 scheduled_for TIMESTAMPTZ, discovery_run_id UUID REFERENCES crawl_runs(id),
 started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ,
 status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running','complete','partial','failed')),
 stage TEXT NOT NULL DEFAULT 'discovery', discovery_complete BOOLEAN NOT NULL DEFAULT FALSE,
 counts JSONB NOT NULL DEFAULT '{}', errors JSONB NOT NULL DEFAULT '[]',
 export_status TEXT NOT NULL DEFAULT 'pending' CHECK(export_status IN ('pending','complete','failed')),
 export_manifest JSONB NOT NULL DEFAULT '{}', export_errors JSONB NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS domestic_tours (
 id UUID PRIMARY KEY, source_tour_id TEXT, canonical_url TEXT NOT NULL, listing_title TEXT NOT NULL DEFAULT '',
 is_listed BOOLEAN NOT NULL DEFAULT TRUE,
 availability TEXT NOT NULL DEFAULT 'pending' CHECK(availability IN ('pending','available','unavailable')),
 availability_reason TEXT NOT NULL DEFAULT '', availability_changed_at TIMESTAMPTZ,
 current_snapshot_id UUID REFERENCES tour_snapshots(id), latest_attempt_run_id UUID REFERENCES crawl_runs(id),
 last_attempt_at TIMESTAMPTZ, last_successful_check_at TIMESTAMPTZ,
 last_seen_batch_id UUID REFERENCES domestic_batches(id), requires_refresh BOOLEAN NOT NULL DEFAULT FALSE,
 fingerprint TEXT, fingerprint_version TEXT, superseded_by_id UUID REFERENCES domestic_tours(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS domestic_tours_identity ON domestic_tours(source_tour_id)
 WHERE source_tour_id IS NOT NULL AND superseded_by_id IS NULL;
CREATE INDEX IF NOT EXISTS domestic_tours_due ON domestic_tours(availability,last_successful_check_at)
 WHERE superseded_by_id IS NULL;
CREATE TABLE IF NOT EXISTS domestic_tour_urls (
 url_key TEXT PRIMARY KEY, tour_id UUID NOT NULL REFERENCES domestic_tours(id),
 first_seen_at TIMESTAMPTZ NOT NULL, last_seen_batch_id UUID REFERENCES domestic_batches(id)
);
CREATE TABLE IF NOT EXISTS domestic_batch_tours (
 batch_id UUID NOT NULL REFERENCES domestic_batches(id), tour_id UUID NOT NULL REFERENCES domestic_tours(id),
 discovered BOOLEAN NOT NULL DEFAULT FALSE, selection_reason TEXT NOT NULL DEFAULT 'skipped',
 attempt_run_ids JSONB NOT NULL DEFAULT '[]', outcome TEXT NOT NULL DEFAULT 'pending', errors JSONB NOT NULL DEFAULT '[]',
 previous_snapshot_id UUID REFERENCES tour_snapshots(id), result_snapshot_id UUID REFERENCES tour_snapshots(id),
 display_snapshot_id UUID REFERENCES tour_snapshots(id), previous_availability TEXT NOT NULL,
 result_availability TEXT, previous_fingerprint TEXT, result_fingerprint TEXT,
 change_kinds JSONB NOT NULL DEFAULT '[]', frozen_state JSONB,
 PRIMARY KEY(batch_id,tour_id)
);
CREATE TABLE IF NOT EXISTS scheduler_state (
 job_key TEXT PRIMARY KEY, last_handled_slot TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS domestic_batches_pending ON domestic_batches(status,export_status,started_at);
