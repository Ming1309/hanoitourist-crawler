"""Manual CLI. No credentials are echoed in failure messages."""

import argparse
from uuid import UUID

from .config import Config
from .reporting.export import export_run
from .reporting.excel import export_excel, export_batch_excel
from .workflows.incremental import crawl_domestic
from .workflows.scheduler import schedule
from .errors import error_message
from .workflows.pipeline import crawl, parse_run, CrawlBusy
from .persistence.storage import Storage


def arguments():
    parser = argparse.ArgumentParser(description='Domestic incremental Hanoitourist crawler with PostgreSQL staging')
    commands = parser.add_subparsers(dest='command', required=True)
    db = commands.add_parser('db')
    db.add_subparsers(dest='db_command', required=True).add_parser('init')
    capture = commands.add_parser('crawl')
    capture.add_argument('--headed', action='store_true')
    capture.add_argument('--output-dir')
    domestic = commands.add_parser('crawl-domestic')
    domestic.add_argument('--headed', action='store_true')
    domestic.add_argument('--output-dir')
    domestic.add_argument('--url', action='append',
                          help='Crawl only this tour URL immediately; repeat for multiple; skips discovery and TTL')
    retry = commands.add_parser('retry-domestic', help='Retry explicitly selected frozen detail failures; no discovery')
    retry.add_argument('--source-batch-id', type=lambda v: str(UUID(v)), required=True)
    retry.add_argument('--url', action='append', required=True)
    retry.add_argument('--output-dir')
    retry.add_argument('--headed', action='store_true')
    report = commands.add_parser('retry-report', help='Rebuild a private diagnostic report offline')
    report.add_argument('--batch-id', type=lambda v: str(UUID(v)), required=True)
    report.add_argument('--output-dir')
    batch_export = commands.add_parser('export-batch')
    batch_export.add_argument('--batch-id', type=lambda v: str(UUID(v)), required=True)
    batch_export.add_argument('--output-dir')
    commands.add_parser('schedule')
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
        if args.command == 'schedule':
            return schedule(config)
        storage = Storage(config.database_url)
        if args.command == 'retry-report':
            from .workflows.retry import retry_report
            path, finished = retry_report(storage, args.batch_id, config.export_dir)
            print(f'batch={args.batch_id} report={path} finished={finished}')
            return 0 if finished else 2
        if args.command == 'retry-domestic':
            from .workflows.retry import retry_domestic
            result = retry_domestic(config, storage, args.source_batch_id, args.url)
            print(f'batch={result.batch_id or "none"} status={result.status}', flush=True)
            for key, path in result.paths.items():
                print(f'{key}={path}')
            return {'handled': 0, 'partial': 2, 'busy': 2, 'failed': 1}[result.status]
        if args.command == 'crawl-domestic':
            result = (crawl_domestic(config, storage, urls=args.url) if args.url
                      else crawl_domestic(config, storage))
            print(f'batch={result.batch_id or "none"} status={result.status}')
            if result.batch_id:
                batch = storage.get_batch(result.batch_id)
                if args.url:
                    print(f'mode=explicit-target export={batch["export_status"]}')
                else:
                    print(f'discovery_terminal={batch["discovery_complete"]} export={batch["export_status"]}')
                print(' '.join(f'{key}={result.counts.get(key, 0)}' for key in
                    ('new', 'updated', 'unavailable', 'reactivated', 'unchanged', 'skipped', 'partial', 'failed')))
                for key, path in result.paths.items():
                    print(f'{key}={path}')
            return {'complete': 0, 'partial': 2, 'busy': 2, 'handled': 0, 'failed': 1}[result.status]
        if args.command == 'export-batch':
            paths = export_batch_excel(storage, args.batch_id, config.export_dir)
            batch = storage.get_batch(args.batch_id)
            print(f'batch={args.batch_id} status={batch["status"]} delta={paths["delta"]} full={paths["full"]}')
            return {'complete': 0, 'partial': 2, 'failed': 1}[batch['status']]
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
    except CrawlBusy:
        print('Another crawl is running; no requests made.')
        return 2
    except KeyboardInterrupt:
        print('Interrupted; committed staging checkpoints retained.')
        return 130
    except ValueError as exc:
        # Retry input can originate in untrusted persisted URLs/context. Never echo it.
        if args.command in {'retry-domestic', 'retry-report'}:
            print('Invalid scoped retry input/configuration; no credentials logged.')
        else:
            print(f'Invalid input/configuration: {exc}' if 'postgres' not in str(exc).lower()
                  else 'Invalid input/configuration.')
        return 1
    except Exception as exc:
        print(f'Operation failed ({type(exc).__name__}); check DB/configuration. Credentials are not logged.')
        return 1
    finally:
        if storage:
            storage.close()
