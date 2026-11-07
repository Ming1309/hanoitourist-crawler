import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from hanoitourist_crawler.cli import main
from hanoitourist_crawler.models import uid


@pytest.mark.parametrize('entry_point', ['console', 'module'])
def test_installed_entry_points_outside_repository(entry_point, tmp_path):
    env = dict(os.environ)
    for key in ('DATABASE_URL', 'TEST_DATABASE_URL', 'PYTHONPATH'):
        env.pop(key, None)
    if entry_point == 'console':
        executable = Path(sys.executable).with_name('hanoitourist')
        assert executable.is_file()
        prefix = [str(executable)]
    else:
        prefix = [sys.executable, '-m', 'hanoitourist_crawler']
    for command in ([], ['db'], ['crawl'], ['crawl-domestic'], ['retry-domestic'],
                    ['retry-report'], ['export-batch'], ['schedule'], ['parse'], ['export']):
        result = subprocess.run(prefix + command + ['--help'], cwd=tmp_path, env=env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert 'usage:' in result.stdout


def test_help_needs_no_database(monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    with pytest.raises(SystemExit) as exc: main(['--help'])
    assert exc.value.code == 0
    result = subprocess.run([sys.executable, '-m', 'hanoitourist_crawler', '--help'], capture_output=True)
    assert result.returncode == 0


@pytest.mark.parametrize('command', ['retry-domestic', 'retry-report'])
def test_retry_help_needs_no_database(command, monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    with pytest.raises(SystemExit) as exc:
        main([command, '--help'])
    assert exc.value.code == 0


@pytest.mark.parametrize('argv', [
    ['retry-domestic'], ['retry-domestic', '--source-batch-id', 'bad', '--url', 'https://hanoitourist.vn/a'],
    ['retry-domestic', '--source-batch-id', '00000000-0000-0000-0000-000000000000'],
    ['retry-report'], ['retry-report', '--batch-id', 'bad'],
])
def test_retry_required_arguments(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize('status,code', [('handled', 0), ('partial', 2), ('busy', 2)])
def test_retry_cli_to_engine(monkeypatch, status, code, tmp_path):
    from hanoitourist_crawler.models import BatchResult
    monkeypatch.setenv('DATABASE_URL', 'postgresql://local/example')
    fake = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr('hanoitourist_crawler.cli.Storage', lambda *a: fake)
    calls = []
    def engine(config, storage, batch, urls):
        calls.append((config, storage, batch, urls))
        return BatchResult(None, status)
    monkeypatch.setattr('hanoitourist_crawler.workflows.retry.retry_domestic', engine)
    bid = uid()
    assert main(['retry-domestic', '--source-batch-id', bid, '--url', 'https://hanoitourist.vn/a',
                 '--url', 'https://hanoitourist.vn/b', '--output-dir', str(tmp_path), '--headed']) == code
    assert calls[0][0].headless is False
    assert calls[0][1:] == (fake, bid, ['https://hanoitourist.vn/a', 'https://hanoitourist.vn/b'])


def test_excel_export_argument():
    from hanoitourist_crawler.cli import arguments
    run_id = uid()
    args = arguments().parse_args(['export', '--run-id', run_id, '--format', 'xlsx'])
    assert args.format == 'xlsx' and args.run_id == run_id
    assert arguments().parse_args(['export', '--run-id', run_id]).format == 'json'


def test_secret_is_not_logged(monkeypatch, capsys):
    monkeypatch.setenv('DATABASE_URL', 'postgresql://user:secret-password@bad/db')
    monkeypatch.setattr('hanoitourist_crawler.cli.Storage', lambda *a: (_ for _ in ()).throw(RuntimeError('secret-password')))
    assert main(['db','init']) == 1
    assert 'secret-password' not in capsys.readouterr().out


@pytest.mark.parametrize('status,code', [('complete',0),('partial',2),('failed',1)])
def test_exit_codes(monkeypatch, tmp_path, status, code):
    monkeypatch.setenv('DATABASE_URL','postgresql://local/example')
    fake = SimpleNamespace(close=lambda:None, get_snapshot=lambda *a:None, get_run=lambda *a:{'errors':[]})
    monkeypatch.setattr('hanoitourist_crawler.cli.Storage',lambda *a:fake)
    monkeypatch.setattr('hanoitourist_crawler.cli.crawl',lambda *a:uid())
    monkeypatch.setattr('hanoitourist_crawler.cli.export_run',lambda *a:(tmp_path/'report.json',status))
    assert main(['crawl']) == code


@pytest.mark.parametrize('status,code', [('complete', 0), ('partial', 2)])
def test_cli_excel_export(monkeypatch, tmp_path, capsys, status, code):
    monkeypatch.setenv('DATABASE_URL', 'postgresql://local/example')
    calls = []
    fake = SimpleNamespace(close=lambda: None, get_snapshot=lambda *a: None,
                           get_run=lambda *a: {'errors': []})
    monkeypatch.setattr('hanoitourist_crawler.cli.Storage', lambda *a: fake)
    monkeypatch.setattr('hanoitourist_crawler.cli.export_run',
                        lambda *a: (_ for _ in ()).throw(AssertionError('must not export JSON')))
    def excel(*args):
        calls.append(args)
        return tmp_path / 'old.xlsx', status
    monkeypatch.setattr('hanoitourist_crawler.cli.export_excel', excel)
    run_id = uid()
    assert main(['export', '--run-id', run_id, '--format', 'xlsx',
                 '--parser-version', 'old', '--output-dir', str(tmp_path)]) == code
    assert calls == [(fake, run_id, tmp_path, 'old')]
    assert 'xlsx=' in capsys.readouterr().out


def test_cli_excel_failure(monkeypatch, capsys):
    monkeypatch.setenv('DATABASE_URL', 'postgresql://local/example')
    closed = []
    fake = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr('hanoitourist_crawler.cli.Storage', lambda *a: fake)
    monkeypatch.setattr('hanoitourist_crawler.cli.export_excel',
                        lambda *a: (_ for _ in ()).throw(OSError('secret-password')))
    assert main(['export', '--run-id', uid(), '--format', 'xlsx']) == 1
    assert closed == [True] and 'secret-password' not in capsys.readouterr().out


def test_interrupted(monkeypatch):
    monkeypatch.setenv('DATABASE_URL','postgresql://local/example')
    monkeypatch.setattr('hanoitourist_crawler.cli.Storage',lambda *a:(_ for _ in ()).throw(KeyboardInterrupt()))
    assert main(['crawl']) == 130


def test_cli_formats_mixed_run_errors(monkeypatch, tmp_path, capsys):
    from hanoitourist_crawler.errors import parse_failure
    monkeypatch.setenv('DATABASE_URL', 'postgresql://local/example')
    errors = ['robots denied', parse_failure('old', ValueError('secret-password'))]
    fake = SimpleNamespace(close=lambda: None, get_snapshot=lambda *a: None,
                           get_run=lambda *a: {'errors': errors})
    monkeypatch.setattr('hanoitourist_crawler.cli.Storage', lambda *a: fake)
    monkeypatch.setattr('hanoitourist_crawler.cli.crawl', lambda *a: uid())
    monkeypatch.setattr('hanoitourist_crawler.cli.export_run', lambda *a: (tmp_path / 'report.json', 'failed'))
    assert main(['crawl']) == 1
    output = capsys.readouterr().out
    assert 'ERROR: robots denied' in output and 'ERROR: Parse failed (ValueError), parser=old' in output
    assert 'secret-password' not in output


def test_domestic_arguments():
    from hanoitourist_crawler.cli import arguments
    args = arguments().parse_args(['crawl-domestic', '--headed', '--output-dir', 'out'])
    assert args.headed and args.output_dir == 'out' and args.url is None
    urls = ['https://hanoitourist.vn/tour-a', 'https://hanoitourist.vn/tour-b']
    args = arguments().parse_args(['crawl-domestic', '--url', urls[0], '--url', urls[1]])
    assert args.url == urls
    batch_id = uid()
    assert arguments().parse_args(['export-batch', '--batch-id', batch_id]).batch_id == batch_id
    assert arguments().parse_args(['schedule']).command == 'schedule'


@pytest.mark.parametrize('status,code', [('complete',0),('partial',2),('failed',1),('busy',2)])
def test_domestic_exit_codes(monkeypatch, tmp_path, status, code):
    from hanoitourist_crawler.models import BatchResult
    monkeypatch.setenv('DATABASE_URL', 'postgresql://local/example')
    batch_id = uid() if status != 'busy' else None
    fake = SimpleNamespace(close=lambda:None,get_batch=lambda *a:{'discovery_complete':True,'export_status':'complete'})
    monkeypatch.setattr('hanoitourist_crawler.cli.Storage',lambda *a:fake)
    monkeypatch.setattr('hanoitourist_crawler.cli.crawl_domestic',lambda *a:BatchResult(batch_id,status,{},{}))
    assert main(['crawl-domestic','--output-dir',str(tmp_path)]) == code


@pytest.mark.parametrize('urls,expected', [
    (None, {}),
    (['https://hanoitourist.vn/a'], {'urls': ['https://hanoitourist.vn/a']}),
    (['https://hanoitourist.vn/a', 'https://hanoitourist.vn/b'],
     {'urls': ['https://hanoitourist.vn/a', 'https://hanoitourist.vn/b']}),
])
def test_domestic_urls_are_forwarded(monkeypatch, urls, expected):
    from hanoitourist_crawler.models import BatchResult
    monkeypatch.setenv('DATABASE_URL', 'postgresql://local/example')
    monkeypatch.setattr('hanoitourist_crawler.cli.Storage',
                        lambda *a: SimpleNamespace(close=lambda: None))
    calls = []
    monkeypatch.setattr('hanoitourist_crawler.cli.crawl_domestic',
                        lambda *a, **k: (calls.append((a, k)), BatchResult(None, 'handled'))[1])
    argv = ['crawl-domestic'] + (sum((['--url', url] for url in urls), []) if urls else [])
    assert main(argv) == 0
    assert calls[0][1] == expected


def test_explicit_cli_reports_mode(monkeypatch, capsys):
    from hanoitourist_crawler.models import BatchResult
    monkeypatch.setenv('DATABASE_URL', 'postgresql://local/example')
    batch_id = uid()
    fake = SimpleNamespace(close=lambda: None,
                           get_batch=lambda *a: {'discovery_complete': False, 'export_status': 'complete'})
    monkeypatch.setattr('hanoitourist_crawler.cli.Storage', lambda *a: fake)
    monkeypatch.setattr('hanoitourist_crawler.cli.crawl_domestic',
                        lambda *a, **k: BatchResult(batch_id, 'partial', {'changed': 1}, {}))
    assert main(['crawl-domestic', '--url', 'https://hanoitourist.vn/a']) == 2
    output = capsys.readouterr().out
    assert 'mode=explicit-target' in output and 'export=complete' in output
    assert 'discovery_terminal' not in output


def test_html_partial_cli_reports_incomplete_discovery(monkeypatch, tmp_path, capsys):
    from hanoitourist_crawler.models import BatchResult
    monkeypatch.setenv('DATABASE_URL', 'postgresql://local/example')
    batch_id = uid()
    fake = SimpleNamespace(close=lambda:None, get_batch=lambda *a:{'discovery_complete':False,'export_status':'complete'})
    monkeypatch.setattr('hanoitourist_crawler.cli.Storage', lambda *a:fake)
    monkeypatch.setattr('hanoitourist_crawler.cli.crawl_domestic', lambda *a:BatchResult(
        batch_id, 'partial', {'new':1}, {'full':tmp_path/'full.xlsx','delta':tmp_path/'delta.xlsx'}))
    assert main(['crawl-domestic']) == 2
    output = capsys.readouterr().out
    assert 'status=partial' in output and 'discovery_terminal=False' in output and 'new=1' in output
    assert 'full=' in output and 'delta=' in output


def test_legacy_busy_is_partial(monkeypatch):
    from hanoitourist_crawler.workflows.pipeline import CrawlBusy
    monkeypatch.setenv('DATABASE_URL', 'postgresql://local/example')
    monkeypatch.setattr('hanoitourist_crawler.cli.Storage',lambda *a:SimpleNamespace(close=lambda:None))
    monkeypatch.setattr('hanoitourist_crawler.cli.crawl',lambda *a:(_ for _ in ()).throw(CrawlBusy()))
    assert main(['crawl']) == 2
