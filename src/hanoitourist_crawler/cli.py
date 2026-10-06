"""Manual CLI. No credentials are echoed in failure messages."""

import argparse
from uuid import UUID

from .config import Config
from .export import export_run
from .excel import export_excel
from .errors import error_message
from .pipeline import crawl, parse_run
from .storage import Storage


def arguments():
    parser = argparse.ArgumentParser(description='Crawl one scoped Hanoitourist tour into PostgreSQL')
    commands = parser.add_subparsers(dest='command', required=True)
    db = commands.add_parser('db')
    db.add_subparsers(dest='db_command', required=True).add_parser('init')
    capture = commands.add_parser('crawl')
    capture.add_argument('--headed', action='store_true')
    capture.add_argument('--output-dir')
    for name in ('parse', 'export'):
        sub = commands.add_parser(name)
        sub.add_argument('--run-id', type=lambda v: str(UUID(v)), required=True)
        sub.add_argument('--output-dir')
        if name == 'export':
            sub.add_argument('--parser-version')
            sub.add_argument('--format', choices=('json', 'xlsx'), default='json')
    return parser


def main(argv=None):
    args = arguments().parse_args(argv)
    storage = None
    try:
        overrides = {'export_dir': getattr(args, 'output_dir', None)}
        if getattr(args, 'headed', False):
            overrides['headless'] = False
        config = Config.load(overrides)
        storage = Storage(config.database_url)
        if args.command == 'db':
            storage.init_schema()
            print('PostgreSQL schema initialized (existing data retained).')
            return 0
        if args.command == 'crawl':
            run_id = crawl(config, storage)
        else:
            run_id = args.run_id
            if args.command == 'parse':
                try:
                    parse_run(storage, run_id)
                except Exception:
                    path, _ = export_run(storage, run_id, config.export_dir)
                    print(f'run={run_id} parse failed; report={path}')
                    return 1
        output_format = getattr(args, 'format', 'json')
        exporter = export_excel if output_format == 'xlsx' else export_run
        path, status = exporter(storage, run_id, config.export_dir, getattr(args, 'parser_version', None))
        snapshot = storage.get_snapshot(run_id, getattr(args, 'parser_version', None))
        print(f'run={run_id} status={status} parser={snapshot.parser_version if snapshot else "none"} '
              f'{output_format}={path}')
        if snapshot:
            print(f'schedules={snapshot.coverage["distinct_schedules"]} observations={snapshot.coverage["observations"]} '
                  f'price_cells={snapshot.coverage["price_cells"]}')
            for warning in snapshot.warnings:
                print(f'WARNING: {warning}')
        else:
            for error in storage.get_run(run_id)['errors']:
                print(f'ERROR: {error_message(error)}')
        return {'complete': 0, 'partial': 2, 'failed': 1}[status]
    except KeyboardInterrupt:
        print('Interrupted; committed staging checkpoints retained.')
        return 130
    except ValueError as exc:
        # Configuration error messages contain only field names, never DATABASE_URL.
        print(f'Invalid input/configuration: {exc}' if 'postgres' not in str(exc).lower()
              else 'Invalid input/configuration.')
        return 1
    except Exception as exc:
        print(f'Operation failed ({type(exc).__name__}); check DB/configuration. Credentials are not logged.')
        return 1
    finally:
        if storage:
            storage.close()
