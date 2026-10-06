import subprocess
import sys
from types import SimpleNamespace

import pytest
from hanoitourist_crawler.cli import main
from hanoitourist_crawler.models import uid


def test_help_needs_no_database(monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    with pytest.raises(SystemExit) as exc: main(['--help'])
    assert exc.value.code == 0
    result = subprocess.run([sys.executable, '-m', 'hanoitourist_crawler', '--help'], capture_output=True)
    assert result.returncode == 0


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
