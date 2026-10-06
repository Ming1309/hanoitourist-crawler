"""Small parameterized PostgreSQL repository with independent raw commits."""

from dataclasses import fields
from importlib.resources import files
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import COLLECTOR_VERSION
from .models import Day, Departure, Media, PriceCell, RawDocument, Section, Snapshot, json_value, now, uid

JSON_FIELDS = {'headers', 'manifest', 'coverage', 'errors', 'warnings', 'provenance',
               'transport_labels', 'applicable_labels'}
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
        script = files('hanoitourist_crawler').joinpath('sql/001_initial.sql').read_text()
        with self.connection.transaction():
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
