"""Small parameterized PostgreSQL repository with independent raw commits."""

from contextlib import contextmanager
from dataclasses import fields
from importlib.resources import files
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .. import COLLECTOR_VERSION
from ..models import Day, Departure, Media, PriceCell, RawDocument, Section, Snapshot, json_value, now, uid

JSON_FIELDS = {'headers', 'manifest', 'coverage', 'errors', 'warnings', 'provenance',
               'transport_labels', 'applicable_labels', 'counts', 'export_manifest', 'export_errors',
               'attempt_run_ids', 'change_kinds', 'frozen_state'}
CHILDREN = {'sections': (Section, 'tour_sections'), 'days': (Day, 'itinerary_days'),
            'media': (Media, 'tour_media'), 'departures': (Departure, 'departure_observations')}


class Storage:
    def __init__(self, database_url, *, schema=None):
        self.connection = psycopg.connect(database_url, autocommit=True, row_factory=dict_row)
        if schema:
            from psycopg import sql
            self.connection.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))

    def close(self):
        self.connection.close()

    def init_schema(self):
        with self.connection.transaction():
            for name in ('001_initial.sql', '002_incremental.sql'):
                script = files('hanoitourist_crawler.persistence').joinpath('sql', name).read_text()
                self.connection.execute(script)

    def preflight(self):
        self.connection.execute('SELECT id FROM crawl_runs LIMIT 0')

    def create_run(self, url):
        run_id = uid()
        self.connection.execute(
            'INSERT INTO crawl_runs (id,requested_url,started_at,collector_version,capture_status) '
            "VALUES (%s,%s,%s,%s,'running')", (run_id, url, now(), COLLECTOR_VERSION))
        return run_id

    def update_run(self, run_id, **values):
        allowed = {'final_url', 'finished_at', 'capture_status', 'parse_status', 'manifest', 'coverage', 'errors'}
        if not values or not values.keys() <= allowed:
            raise ValueError('Invalid run update')
        params = [Jsonb(json_value(v)) if k in JSON_FIELDS else v for k, v in values.items()]
        self.connection.execute('UPDATE crawl_runs SET ' + ','.join(f'{k}=%s' for k in values)
                                + ' WHERE id=%s', (*params, run_id))

    def get_run(self, run_id):
        row = self.connection.execute('SELECT * FROM crawl_runs WHERE id=%s', (run_id,)).fetchone()
        if not row:
            raise ValueError('Unknown crawl run')
        return self._clean(row)

    @staticmethod
    def _clean(row):
        return {k: str(v) if isinstance(v, UUID) else v for k, v in row.items()}

    def _insert(self, table, obj, *, extra=None, exclude=()):
        data = {f.name: getattr(obj, f.name) for f in fields(obj) if f.name not in exclude}
        data.update(extra or {})
        params = [Jsonb(json_value(v)) if k in JSON_FIELDS else v for k, v in data.items()]
        self.connection.execute(f'INSERT INTO {table} ({",".join(data)}) '
                                f'VALUES ({",".join(["%s"] * len(data))})', params)

    def append_raw(self, document):
        self._insert('raw_documents', document)

    def load_raw(self, run_id):
        rows = self.connection.execute('SELECT * FROM raw_documents WHERE run_id=%s ORDER BY sequence',
                                       (run_id,)).fetchall()
        return [RawDocument(**self._clean(row)) for row in rows]

    def get_snapshot(self, run_id, version=None):
        query = 'SELECT * FROM tour_snapshots WHERE run_id=%s'
        params = [run_id]
        if version:
            query += ' AND parser_version=%s'
            params.append(version)
        row = self.connection.execute(query + ' ORDER BY parsed_at DESC LIMIT 1', params).fetchone()
        if not row:
            return None
        return self._hydrate_snapshot(row)

    def get_snapshot_by_id(self, snapshot_id):
        row = self.connection.execute('SELECT * FROM tour_snapshots WHERE id=%s', (snapshot_id,)).fetchone()
        return self._hydrate_snapshot(row) if row else None

    def _hydrate_snapshot(self, row):
        result = Snapshot(**self._clean(row))
        for attr, (cls, table) in CHILDREN.items():
            rows = self.connection.execute(f'SELECT * FROM {table} WHERE snapshot_id=%s ORDER BY '
                                           + ('departure_at,source_schedule_id,observed_at,id' if attr == 'departures' else 'ordinal,id'),
                                           (result.id,)).fetchall()
            values = []
            for row in rows:
                data = self._clean(row)
                data.pop('snapshot_id')
                obj = cls(**data)
                if attr == 'departures':
                    cells = self.connection.execute('SELECT * FROM price_cells WHERE departure_id=%s '
                                                    'ORDER BY row_index,column_index', (obj.id,)).fetchall()
                    for cell in cells:
                        data = self._clean(cell)
                        data.pop('departure_id')
                        obj.price_cells.append(PriceCell(**data))
                values.append(obj)
            setattr(result, attr, values)
        return result

    def persist_snapshot(self, snapshot):
        with self.connection.transaction():
            # Serializes concurrent replay of the same run; prevents partial duplicate children.
            self.connection.execute('SELECT id FROM crawl_runs WHERE id=%s FOR UPDATE', (snapshot.run_id,))
            existing = self.get_snapshot(snapshot.run_id, snapshot.parser_version)
            if existing:
                return existing
            self._insert('tour_snapshots', snapshot, exclude=CHILDREN.keys())
            for attr, (_, table) in CHILDREN.items():
                for obj in getattr(snapshot, attr):
                    self._insert(table, obj, extra={'snapshot_id': snapshot.id},
                                 exclude=('price_cells',) if attr == 'departures' else ())
                    if attr == 'departures':
                        for cell in obj.price_cells:
                            self._insert('price_cells', cell, extra={'departure_id': obj.id})
        return self.get_snapshot(snapshot.run_id, snapshot.parser_version)

    def preflight_incremental(self):
        self.preflight()
        for table in ('crawl_batches', 'tours', 'tour_urls',
                      'crawl_batch_tours', 'scheduler_state'):
            if not self.connection.execute('SELECT to_regclass(%s) AS name', (table,)).fetchone()['name']:
                raise ValueError('Incremental schema missing; run db init')

    @contextmanager
    def crawl_lock(self):
        key = "hashtextextended(current_database()||':'||current_schema()||':hanoitourist:crawl',0)"
        acquired = self.connection.execute(f'SELECT pg_try_advisory_lock({key}) AS acquired').fetchone()['acquired']
        try:
            yield acquired
        finally:
            if acquired and not self.connection.closed:
                self.connection.execute(f'SELECT pg_advisory_unlock({key})')

    def create_batch(self, scheduled_for=None):
        batch_id = uid()
        self.connection.execute('INSERT INTO crawl_batches(id,origin,scheduled_for,started_at) '
                                'VALUES (%s,%s,%s,%s)',
                                (batch_id, 'scheduled' if scheduled_for else 'manual', scheduled_for, now()))
        return batch_id

    def get_batch(self, batch_id):
        row = self.connection.execute('SELECT * FROM crawl_batches WHERE id=%s', (batch_id,)).fetchone()
        if not row:
            raise ValueError('Unknown domestic batch')
        return self._clean(row)

    def update_batch(self, batch_id, **values):
        allowed = {'discovery_run_id', 'finished_at', 'status', 'stage', 'discovery_complete',
                   'counts', 'errors', 'export_status', 'export_manifest', 'export_errors'}
        if not values or not values.keys() <= allowed:
            raise ValueError('Invalid batch update')
        params = [Jsonb(json_value(v)) if k in JSON_FIELDS else v for k, v in values.items()]
        # The first successful publication receipt is canonical. A conditional SQL
        # update also rejects stale/concurrent success or failure acknowledgements.
        condition = " AND export_manifest='{}'::jsonb" if values.keys() & {
            'export_status', 'export_manifest', 'export_errors'} else ''
        self.connection.execute('UPDATE crawl_batches SET ' + ','.join(f'{k}=%s' for k in values)
                                + ' WHERE id=%s' + condition, (*params, batch_id))

    def list_tours(self):
        rows = self.connection.execute('SELECT * FROM tours WHERE superseded_by_id IS NULL '
                                       'ORDER BY canonical_url,id').fetchall()
        return [self._clean(row) for row in rows]

    def get_tour(self, tour_id):
        row = self.connection.execute('SELECT * FROM tours WHERE id=%s', (tour_id,)).fetchone()
        if not row:
            raise ValueError('Unknown domestic tour')
        return self._clean(row)

    def tour_aliases(self, tour_id):
        rows = self.connection.execute('SELECT url_key FROM tour_urls WHERE tour_id=%s ORDER BY url_key',
                                       (tour_id,)).fetchall()
        return [row['url_key'] for row in rows]

    def get_tour_by_source_id(self, source_id):
        row = self.connection.execute('SELECT * FROM tours WHERE source_tour_id=%s '
                                      'AND superseded_by_id IS NULL', (source_id,)).fetchone()
        return self._clean(row) if row else None

    def _update_tour(self, tour_id, **values):
        allowed = {'source_tour_id', 'canonical_url', 'listing_title', 'is_listed', 'availability',
                   'availability_reason', 'availability_changed_at', 'current_snapshot_id',
                   'latest_attempt_run_id', 'last_attempt_at', 'last_successful_check_at',
                   'last_seen_batch_id', 'requires_refresh', 'fingerprint', 'fingerprint_version', 'superseded_by_id'}
        if not values or not values.keys() <= allowed:
            raise ValueError('Invalid tour update')
        self.connection.execute('UPDATE tours SET ' + ','.join(f'{k}=%s' for k in values)
                                + ' WHERE id=%s', (*values.values(), tour_id))

    def register_tour(self, url, title, batch_id, source_id=None, *, listed=True):
        from ..processing.fingerprint import fingerprint, FINGERPRINT_VERSION
        with self.connection.transaction():
            existing = self.connection.execute('SELECT t.* FROM tour_urls u '
                'JOIN tours t ON t.id=u.tour_id WHERE u.url_key=%s', (url,)).fetchone()
            if existing:
                tour_id = str(existing['id'])
            else:
                history = self.connection.execute('SELECT s.id,r.finished_at,r.id AS run_id,r.capture_status '
                    'FROM tour_snapshots s JOIN crawl_runs r ON r.id=s.run_id '
                    'WHERE (r.requested_url=%s OR s.canonical_url=%s OR (%s::text IS NOT NULL AND s.source_tour_id=%s)) '
                    "ORDER BY (s.status='complete' AND r.capture_status='complete') DESC,"
                    'COALESCE(r.finished_at,r.started_at) DESC,s.parsed_at DESC,s.id DESC LIMIT 1',
                    (url, url, source_id, source_id)).fetchone()
                snapshot = self.get_snapshot_by_id(history['id']) if history else None
                sid = snapshot.source_tour_id if snapshot else None
                matched = self.connection.execute('SELECT id FROM tours WHERE source_tour_id=%s '
                    'AND superseded_by_id IS NULL', (sid,)).fetchone() if sid else None
                if matched:
                    tour_id = str(matched['id'])
                else:
                    tour_id = uid()
                    self.connection.execute('INSERT INTO tours(id,canonical_url,listing_title) VALUES (%s,%s,%s)',
                                            (tour_id, url, title))
                    if snapshot:
                        complete = snapshot.status == 'complete' and history['capture_status'] == 'complete'
                        self._update_tour(tour_id, source_tour_id=sid,
                            current_snapshot_id=snapshot.id if complete else None,
                            latest_attempt_run_id=str(history['run_id']),
                            availability='available' if complete else 'pending',
                            last_successful_check_at=history['finished_at'] if complete else None,
                            fingerprint=fingerprint(snapshot) if complete else None,
                            fingerprint_version=FINGERPRINT_VERSION if complete else None)
                self.connection.execute('INSERT INTO tour_urls(url_key,tour_id,first_seen_at) VALUES (%s,%s,%s)',
                                        (url, tour_id, now()))
            if listed:
                self._update_tour(tour_id, is_listed=True, listing_title=title, last_seen_batch_id=batch_id)
                self.connection.execute('UPDATE tour_urls SET last_seen_batch_id=%s WHERE url_key=%s',
                                        (batch_id, url))
            self.ensure_batch_tour(batch_id, tour_id, discovered=listed)
            return self.get_tour(tour_id)

    def ensure_batch_tour(self, batch_id, tour_id, *, discovered=False):
        tour = self.get_tour(tour_id)
        self.connection.execute('INSERT INTO crawl_batch_tours(batch_id,tour_id,discovered,previous_snapshot_id,'
            'previous_availability,previous_fingerprint) VALUES (%s,%s,%s,%s,%s,%s) '
            'ON CONFLICT(batch_id,tour_id) DO UPDATE SET discovered=crawl_batch_tours.discovered OR EXCLUDED.discovered',
            (batch_id, tour_id, discovered, tour['current_snapshot_id'], tour['availability'], tour['fingerprint']))

    def batch_tours(self, batch_id):
        rows = self.connection.execute('SELECT * FROM crawl_batch_tours WHERE batch_id=%s ORDER BY tour_id',
                                       (batch_id,)).fetchall()
        return [self._clean(row) for row in rows]

    def update_batch_tour(self, batch_id, tour_id, **values):
        allowed = {'discovered', 'selection_reason', 'attempt_run_ids', 'outcome', 'errors', 'result_snapshot_id',
                   'display_snapshot_id', 'result_availability', 'result_fingerprint', 'change_kinds', 'frozen_state'}
        if not values or not values.keys() <= allowed:
            raise ValueError('Invalid batch tour update')
        params = [Jsonb(json_value(v)) if k in JSON_FIELDS else v for k, v in values.items()]
        self.connection.execute('UPDATE crawl_batch_tours SET ' + ','.join(f'{k}=%s' for k in values)
                                + ' WHERE batch_id=%s AND tour_id=%s', (*params, batch_id, tour_id))

    def begin_attempt(self, batch_id, tour_id, run_id, reason):
        with self.connection.transaction():
            self.ensure_batch_tour(batch_id, tour_id)
            self.connection.execute('UPDATE crawl_batch_tours SET selection_reason=%s,'
                'attempt_run_ids=attempt_run_ids || %s WHERE batch_id=%s AND tour_id=%s',
                (reason, Jsonb([run_id]), batch_id, tour_id))
            self._update_tour(tour_id, latest_attempt_run_id=run_id, last_attempt_at=now())

    def bind_identity(self, batch_id, tour_id, snapshot):
        from ..acquisition.scope import tour_url
        current = self.get_tour(tour_id)
        if current['source_tour_id'] and current['source_tour_id'] != snapshot.source_tour_id:
            raise ValueError('Tour identity changed; baseline retained')
        target = self.connection.execute('SELECT id FROM tours WHERE source_tour_id=%s '
            'AND superseded_by_id IS NULL', (snapshot.source_tour_id,)).fetchone()
        target_id = str(target['id']) if target else tour_id
        canonical = tour_url(snapshot.canonical_url)
        alias = self.connection.execute('SELECT tour_id FROM tour_urls WHERE url_key=%s', (canonical,)).fetchone()
        if alias and str(alias['tour_id']) not in {tour_id, target_id}:
            raise ValueError('Conflicting canonical tour identity')
        if target_id != tour_id:
            self.ensure_batch_tour(batch_id, target_id)
            source_row = next(r for r in self.batch_tours(batch_id) if r['tour_id'] == tour_id)
            target_row = next(r for r in self.batch_tours(batch_id) if r['tour_id'] == target_id)
            self.update_batch_tour(batch_id, target_id, discovered=source_row['discovered'] or target_row['discovered'],
                selection_reason=source_row['selection_reason'],
                attempt_run_ids=list(dict.fromkeys(target_row['attempt_run_ids'] + source_row['attempt_run_ids'])))
            self.connection.execute('UPDATE tour_urls SET tour_id=%s WHERE tour_id=%s', (target_id, tour_id))
            self._update_tour(tour_id, superseded_by_id=target_id)
            self.update_batch_tour(batch_id, tour_id, outcome='merged')
            self._update_tour(target_id, latest_attempt_run_id=current['latest_attempt_run_id'],
                              last_attempt_at=current['last_attempt_at'], is_listed=current['is_listed'],
                              last_seen_batch_id=current['last_seen_batch_id'])
        self.connection.execute('INSERT INTO tour_urls(url_key,tour_id,first_seen_at) VALUES (%s,%s,%s) '
            'ON CONFLICT(url_key) DO UPDATE SET tour_id=EXCLUDED.tour_id', (canonical, target_id, now()))
        self._update_tour(target_id, source_tour_id=snapshot.source_tour_id, canonical_url=canonical)
        return target_id

    def apply_attempt(self, batch_id, tour_id, run_id):
        from ..processing.fingerprint import fingerprint, FINGERPRINT_VERSION
        with self.connection.transaction():
            row = next(r for r in self.batch_tours(batch_id) if r['tour_id'] == tour_id)
            if row['outcome'] != 'pending':
                return tour_id
            run = self.get_run(run_id)
            snapshot = self.get_snapshot(run_id)
            tour = self.get_tour(tour_id)
            while tour['superseded_by_id']:
                tour = self.get_tour(tour['superseded_by_id'])
            if snapshot and snapshot.status == 'complete' and run['capture_status'] == 'complete':
                if tour['source_tour_id'] and tour['source_tour_id'] != snapshot.source_tour_id:
                    raise ValueError('Tour identity changed; baseline retained')
                tour = self.get_tour_by_source_id(snapshot.source_tour_id) or tour
            # Captures are serialized; start time stays truthful when recovery finishes an old run now.
            newer = self.connection.execute('SELECT 1 FROM crawl_runs r WHERE r.started_at>%s AND ('
                'r.id=(SELECT run_id FROM tour_snapshots WHERE id=%s) OR EXISTS ('
                'SELECT 1 FROM crawl_batch_tours b WHERE b.tour_id=%s '
                "AND b.outcome IN ('changed','unchanged','unavailable','partial','failed') "
                'AND b.attempt_run_ids ? r.id::text)) LIMIT 1',
                (run['started_at'], tour['current_snapshot_id'], tour['id'])).fetchone()
            if newer:
                self.update_batch_tour(batch_id, tour_id, outcome='skipped', change_kinds=[],
                    result_snapshot_id=tour['current_snapshot_id'], result_availability=tour['availability'],
                    result_fingerprint=tour['fingerprint'],
                    errors=run['errors'] + ['Stale attempt ignored; newer published state retained'])
                if tour_id != tour['id']:
                    self._update_tour(tour_id, requires_refresh=True)
                return tour['id']
            navigation = run.get('manifest', {}).get('navigation', [])
            terminal = navigation[-1] if navigation else {}
            raw = {d.id: d for d in self.load_raw(run_id)}
            evidence = raw.get(terminal.get('raw_document_id'))
            gone = (terminal.get('owned_primary') is True and terminal.get('terminal') is True
                    and terminal.get('status') in {404, 410} and evidence is not None
                    and evidence.kind == 'http_document' and evidence.role == 'tour'
                    and evidence.method in {'GET', 'HEAD'}
                    and evidence.http_status == terminal['status'] and evidence.url == terminal.get('url'))
            changes, outcome = [], 'failed'
            if gone:
                tour = self.get_tour(tour_id)
                if tour['availability'] != 'unavailable':
                    changes = ['unavailable']
                self._update_tour(tour_id, availability='unavailable',
                    availability_reason=f'http_{terminal["status"]}', availability_changed_at=now(), requires_refresh=False)
                outcome = 'unavailable'
            elif snapshot and snapshot.status == 'complete' and run['capture_status'] == 'complete':
                tour_id = self.bind_identity(batch_id, tour_id, snapshot)
                tour = self.get_tour(tour_id)
                digest = fingerprint(snapshot)
                baseline = next(r for r in self.batch_tours(batch_id) if r['tour_id'] == tour_id)
                if not baseline['previous_snapshot_id']:
                    changes.append('new')
                elif baseline['previous_fingerprint'] != digest:
                    changes.append('updated')
                if baseline['previous_availability'] == 'unavailable':
                    changes.append('reactivated')
                self._update_tour(tour_id, availability='available', availability_reason='',
                    availability_changed_at=now() if tour['availability'] != 'available' else tour['availability_changed_at'],
                    current_snapshot_id=snapshot.id, last_successful_check_at=run['finished_at'],
                    fingerprint=digest, fingerprint_version=FINGERPRINT_VERSION, requires_refresh=False)
                outcome = 'changed' if changes else 'unchanged'
            else:
                self._update_tour(tour_id, requires_refresh=True)
                outcome = 'partial' if snapshot or run['capture_status'] == 'partial' else 'failed'
            tour = self.get_tour(tour_id)
            self.update_batch_tour(batch_id, tour_id, outcome=outcome, change_kinds=changes,
                result_snapshot_id=tour['current_snapshot_id'], result_availability=tour['availability'],
                result_fingerprint=tour['fingerprint'], errors=run['errors'] + (snapshot.warnings if snapshot else []))
            return tour_id

    def _partial_display_snapshot(self, tour):
        row = self.connection.execute('SELECT s.id FROM tour_snapshots s JOIN crawl_runs r ON r.id=s.run_id '
            "WHERE s.status='partial' AND (%s::text IS NULL OR s.source_tour_id=%s) AND ("
            'EXISTS (SELECT 1 FROM tour_urls u WHERE u.tour_id=%s '
            'AND (u.url_key=r.requested_url OR u.url_key=s.canonical_url)) OR '
            'EXISTS (SELECT 1 FROM crawl_batch_tours b WHERE b.tour_id=%s '
            'AND b.attempt_run_ids ? r.id::text) OR r.id=%s) '
            'ORDER BY COALESCE(r.finished_at,r.started_at) DESC,s.parsed_at DESC,s.id DESC LIMIT 1',
            (tour['source_tour_id'] or None, tour['source_tour_id'] or None,
             tour['id'], tour['id'], tour['latest_attempt_run_id'])).fetchone()
        return str(row['id']) if row else None

    def freeze_batch(self, batch_id, status, errors=()):
        with self.connection.transaction():
            batch = self.get_batch(batch_id)
            if batch['stage'] == 'frozen':
                return batch
            for tour in self.list_tours():
                self.ensure_batch_tour(batch_id, tour['id'])
                row = next(r for r in self.batch_tours(batch_id) if r['tour_id'] == tour['id'])
                display = tour['current_snapshot_id']
                if not display:
                    display = self._partial_display_snapshot(tour)
                self.update_batch_tour(batch_id, tour['id'], frozen_state=tour, display_snapshot_id=display,
                    result_availability=tour['availability'], result_fingerprint=tour['fingerprint'],
                    outcome='skipped' if row['outcome'] == 'pending' and not row['attempt_run_ids'] else row['outcome'])
            active = [r for r in self.batch_tours(batch_id) if r['frozen_state'] is not None]
            counts = {}
            for row in active:
                for key in dict.fromkeys([row['outcome'], *row['change_kinds']]):
                    counts[key] = counts.get(key, 0) + 1
            self.update_batch(batch_id, status=status, stage='frozen', finished_at=now(), counts=counts, errors=list(errors))
            if batch['scheduled_for']:
                self.handle_slot(batch['scheduled_for'])
            return self.get_batch(batch_id)

    def pending_batches(self):
        rows = self.connection.execute("SELECT * FROM crawl_batches WHERE status='running' "
            "OR export_status='pending' ORDER BY started_at,id").fetchall()
        return [self._clean(row) for row in rows]

    def last_handled_slot(self):
        row = self.connection.execute("SELECT last_handled_slot FROM scheduler_state WHERE job_key='domestic'").fetchone()
        return row['last_handled_slot'] if row else None

    def handle_slot(self, slot):
        self.connection.execute("INSERT INTO scheduler_state(job_key,last_handled_slot,updated_at) VALUES ('domestic',%s,%s) "
            'ON CONFLICT(job_key) DO UPDATE SET last_handled_slot=GREATEST(scheduler_state.last_handled_slot,EXCLUDED.last_handled_slot),'
            'updated_at=EXCLUDED.updated_at', (slot, now()))
