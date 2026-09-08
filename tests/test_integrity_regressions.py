from datetime import datetime, timezone, date
import sqlite3

from tokenledger.analytics import _quota_for_agent, _reconcile_account_rollups, build_dashboard
from tokenledger.db import TokenDatabase
from tokenledger.models import QuotaSnapshot, UsageEvent, ParsedFile, ProviderProbe
from tokenledger.models import DiscoveredFile
from pathlib import Path
from tokenledger.pricing import cost_summary, estimate_token_cost
from tokenledger import analytics
from tokenledger.scanner import ScanCoordinator
from types import SimpleNamespace
from tokenledger.providers.antigravity import AntigravityAdapter
from tokenledger.api import bind_local_server, local_server_url
from tokenledger.config import AppConfig
import threading
import urllib.request
import urllib.error
import json
import pytest


def test_expired_quota_is_not_a_new_full_window():
    quota, _ = _quota_for_agent([dict(status='fresh', label='weekly', remaining_percent=35,
        resets_at='2026-09-01T00:00:00Z', updated_at='2026-08-31T00:00:00Z')],
        datetime(2026, 9, 8, tzinfo=timezone.utc))
    assert quota['status'] == 'stale'
    assert quota['remaining_percent'] is None
    assert quota['used_percent'] is None


def test_independent_coverage_and_unknown_identity_survive():
    base = dict(agent='claude', local_date=date(2026, 9, 1), route='CC Switch',
                model='model', platform='p1', total_tokens=100, usage_scope='session')
    rows = [base, {**base, 'platform': 'p2', 'usage_scope': 'account_daily', 'total_tokens': 80},
            {**base, 'platform': '平台未识别', 'usage_scope': 'account_daily', 'total_tokens': 70}]
    kept, _ = _reconcile_account_rollups(rows)
    assert sum(r['total_tokens'] for r in kept) == 250


def test_cache_write_and_partial_pricing():
    price = estimate_token_cost('claude-sonnet-4-6', 1_000_000, 200_000, 0,
                                cache_write_tokens=300_000)
    assert abs(price['cost_usd'] - 2.685) < 1e-9
    result = cost_summary([dict(model='claude-sonnet-4-6', input_tokens=100, total_tokens=100),
                           dict(model='unknown', total_tokens=200)])
    assert result['has_unpriced_usage']
    assert result['unpriced_tokens'] == 200
    assert result['cost_cny_text'].startswith('部分估算')
    assert not estimate_token_cost('deepseek-made-up', 100, 0, 0)['cost_known']


def test_upgrade_clears_only_derived_sensitive_data(tmp_path):
    path = tmp_path / 'ledger.db'
    db = TokenDatabase(path)
    event = UsageEvent('e', 'antigravity', 'r', 'p', 'm', 's', '2026-09-01T00:00:00Z', total_tokens=123)
    db.replace_file('f', 'antigravity', 'fixture', 1, 1, ParsedFile(events=[event]), '2026-09-01')
    db.update_provider('antigravity', ProviderProbe('ready', 'a', 'fixture', '',
                       {'top_sessions': [{'title': 'private prompt'}]}), '2026-09-01')
    db.save_quotas([QuotaSnapshot('old', 'antigravity', 'fabricated', 'fresh', 86.8)])
    with db.connect() as con:
        con.execute("DELETE FROM app_meta WHERE key='migration:2.4.3'")
    upgraded = TokenDatabase(path)
    assert upgraded.usage_rows()[0]['total_tokens'] == 123
    assert upgraded.provider_states()[0]['metadata'] == {}
    assert 'antigravity' not in upgraded.latest_quotas()
    upgraded.save_quotas([QuotaSnapshot('new', 'antigravity', 'server', 'fresh', 25)])
    assert TokenDatabase(path).latest_quotas()['antigravity'][0]['remaining_percent'] == 25


def test_replace_quota_set_removes_deleted_provider_only(tmp_path):
    db = TokenDatabase(tmp_path / 'ledger.db')
    db.save_quotas([QuotaSnapshot('c', 'codex', 'c', 'fresh', 50),
                    QuotaSnapshot('a', 'claude', 'a', 'fresh', 40)])
    db.save_quotas([], replace_agent='claude')
    assert set(db.latest_quotas()) == {'codex'}


def test_external_database_write_invalidates_dashboard(tmp_path):
    db = TokenDatabase(tmp_path / 'ledger.db')
    event = UsageEvent('e', 'codex', 'r', 'p', 'm', 's', '2026-09-01T00:00:00Z', total_tokens=1)
    db.replace_file('f', 'codex', 'fixture', 1, 1, ParsedFile(events=[event]), '2026-09-01')
    assert build_dashboard(db, {}, 'Asia/Shanghai', None, None)['summary']['total'] == 1
    with sqlite3.connect(db.path) as con:
        con.execute('UPDATE usage_events SET total_tokens=2')
    assert build_dashboard(db, {}, 'Asia/Shanghai', None, None)['summary']['total'] == 2


def test_quota_metadata_uses_same_expiration_rules(tmp_path):
    db = TokenDatabase(tmp_path / 'ledger.db')
    db.update_provider('claude', ProviderProbe('ready', 'c', 'fixture', '', {
        'budget_windows': [dict(label='old', status='fresh', remaining_percent=60,
            updated_at='2000-01-01T00:00:00Z', resets_at='2000-01-02T00:00:00Z')]
    }), '2026-09-01')
    dash = build_dashboard(db, {}, 'Asia/Shanghai', None, None)
    quota = next(a for a in dash['agents'] if a['id'] == 'claude')['quota']
    assert quota['status'] == 'stale'
    assert quota['remaining_percent'] is None


def test_time_bucket_invalidates_without_log_changes(tmp_path, monkeypatch):
    db = TokenDatabase(tmp_path / 'ledger.db')
    monkeypatch.setattr(analytics, 'monotonic', lambda: 30)
    build_dashboard(db, {}, 'Asia/Shanghai', None, None)
    prior = analytics._CACHE.key
    monkeypatch.setattr(analytics, 'monotonic', lambda: 46)
    build_dashboard(db, {}, 'Asia/Shanghai', None, None)
    assert analytics._CACHE.key != prior


def test_scanner_authoritative_empty_quota_removes_old_set(tmp_path):
    db = TokenDatabase(tmp_path / 'ledger.db')
    db.save_quotas([QuotaSnapshot('old', 'claude', 'removed provider', 'fresh', 40)])
    adapter = SimpleNamespace(descriptor=SimpleNamespace(id='claude', name='Claude'),
        discover_files=lambda: [], probe=lambda: ProviderProbe('empty', 'c', 'fixture', '',
        {'budget_windows': []}))
    ScanCoordinator(db, [adapter]).scan_sync()
    assert 'claude' not in db.latest_quotas()


def test_antigravity_does_not_invent_missing_model(tmp_path):
    path = tmp_path / '.gemini' / 'antigravity' / 'conversations' / 'synthetic.db'
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as con:
        con.execute('CREATE TABLE gen_metadata(idx INTEGER, data BLOB)')
        con.execute('INSERT INTO gen_metadata VALUES(1, ?)', (b'\x0a\x04\x22\x02\x10\x0a',))
    value = AntigravityAdapter(tmp_path)._get_conversation_gen_metadata('synthetic')[1]
    assert value['model'] == '模型未记录'
    assert value['uncached'] == 10


def test_theme_endpoint_validates_and_returns_success(tmp_path, monkeypatch):
    monkeypatch.setattr('tokenledger.native.apply_window_theme', lambda *args: True)
    db = TokenDatabase(tmp_path / 'ledger.db')
    server = bind_local_server(AppConfig(tmp_path, tmp_path, tmp_path, port=0), db, ScanCoordinator(db, []))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def post(theme):
        return urllib.request.urlopen(urllib.request.Request(
            local_server_url(server) + 'api/theme?theme=' + theme, data=b'',
            headers={'X-Token-Ledger-Request': 'same-origin'}), timeout=2)
    try:
        for theme in ('dark', 'light'):
            with post(theme) as response:
                assert json.load(response) == {'ok': True, 'theme': theme}
        with pytest.raises(urllib.error.HTTPError) as error:
            post('invalid')
        assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_antigravity_probe_reuses_stats_but_refreshes_quota(tmp_path, monkeypatch):
    log = tmp_path / '.gemini/antigravity/brain/synthetic/.system_generated/logs/transcript.jsonl'
    log.parent.mkdir(parents=True)
    log.write_text('{"type":"USER_INPUT","content":"synthetic"}\n', encoding='utf-8')
    adapter = AntigravityAdapter(tmp_path)
    monkeypatch.setattr(adapter, '_fetch_quota_windows', lambda: [])
    initial = adapter.probe()
    assert initial.metadata['models'] == ['模型未记录']
    monkeypatch.setattr(adapter, '_fetch_quota_windows', lambda: [QuotaSnapshot('q', 'antigravity', 'server', 'fresh', 25)])
    original_open = Path.open
    def no_transcript_read(path, *args, **kwargs):
        if path == log:
            raise AssertionError('unchanged transcript must not be reread')
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', no_transcript_read)
    cached = adapter.probe()
    assert cached.metadata['budget_windows'][0]['remaining_percent'] == 25
    assert cached.metadata['activity_count'] == initial.metadata['activity_count']


def test_antigravity_changes_during_parse_are_not_marked_indexed(tmp_path, monkeypatch):
    log = tmp_path / '.gemini/antigravity/brain/synthetic/.system_generated/logs/transcript.jsonl'
    log.parent.mkdir(parents=True)
    log.write_text('{"type":"USER_INPUT","content":"synthetic"}\n', encoding='utf-8')
    adapter = AntigravityAdapter(tmp_path)
    monkeypatch.setattr(adapter, '_fetch_quota_windows', lambda: [])
    file = DiscoveredFile(log, 'fixture')
    adapter.parse_file(file)
    log.write_text('{"type":"USER_INPUT","content":"changed synthetic text"}\n', encoding='utf-8')
    adapter.mark_parsed(file)
    assert adapter.should_reparse(file)
