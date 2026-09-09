import json
from pathlib import Path

from tokenledger.db import TokenDatabase
from tokenledger.models import DiscoveredFile
from tokenledger.providers.codex import CodexAdapter

PARENT = '11111111-1111-1111-1111-111111111111'
CHILD = '22222222-2222-2222-2222-222222222222'


def record(total, last=100, stamp='2026-09-01T00:00:00Z', quota=False):
    payload = {'type': 'token_count', 'info': {
        'last_token_usage': {'input_tokens': last, 'total_tokens': last},
        'total_token_usage': {'input_tokens': total, 'total_tokens': total}}}
    if quota:
        payload['rate_limits'] = {'primary': {'used_percent': 10}}
    return {'timestamp': stamp, 'payload': payload}


def write_log(home, identity, records, parent=None, archived=False):
    path = home / '.codex' / ('archived_sessions' if archived else 'sessions') / f'rollout-{identity}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {'id': identity}
    if parent:
        meta['forked_from_id'] = parent
    rows = [{'type': 'session_meta', 'payload': meta}, *records]
    path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')
    return path


def parse(home, path):
    return CodexAdapter(home).parse_file(DiscoveredFile(path, 'synthetic'))


def test_identical_calls_at_same_timestamp_are_not_dropped(tmp_path):
    path = write_log(tmp_path, PARENT, [record(100, quota=True), record(200), record(300)])
    events = parse(tmp_path, path).events
    assert len(events) == 3
    assert sum(e.total_tokens for e in events) == 300
    assert len({e.event_id for e in events}) == 3


def test_repeated_cumulative_snapshot_is_not_a_new_call(tmp_path):
    path = write_log(tmp_path, PARENT, [record(100), record(100, stamp='2026-09-01T00:01:00Z', quota=True), record(200)])
    parsed = parse(tmp_path, path)
    assert sum(e.total_tokens for e in parsed.events) == 200
    assert len(parsed.quotas) == 1


def test_fork_inherited_prefix_not_counted_twice(tmp_path):
    write_log(tmp_path, PARENT, [record(100), record(200)])
    path = write_log(tmp_path, CHILD, [record(100), record(200), record(250, last=50)], parent=PARENT)
    parsed = parse(tmp_path, path)
    assert sum(e.total_tokens for e in parsed.events) == 50
    assert parsed.metadata['inherited_events_skipped'] == 2


def test_trimmed_fork_does_not_add_inherited_cumulative_baseline(tmp_path):
    write_log(tmp_path, PARENT, [record(100), record(200)])
    path = write_log(tmp_path, CHILD, [record(250, last=50), record(300, last=50)], parent=PARENT)
    assert sum(e.total_tokens for e in parse(tmp_path, path).events) == 100


def test_missing_parent_does_not_invent_cumulative_history(tmp_path):
    path = write_log(tmp_path, CHILD, [record(500000, last=50), record(500050, last=50)], parent=PARENT)
    assert sum(e.total_tokens for e in parse(tmp_path, path).events) == 100


def test_counter_reset_starts_another_segment(tmp_path):
    path = write_log(tmp_path, PARENT, [record(100), record(200), record(20, last=20), record(50, last=30)])
    assert sum(e.total_tokens for e in parse(tmp_path, path).events) == 250


def save(db, fid, parsed):
    db.replace_file(fid, 'codex', 'synthetic', 1, 1, parsed, '2026-09-01T00:00:00Z')


def insert_legacy_rows(db, file_id, rows):
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO file_states(file_id,agent,path_hint,mtime_ns,size_bytes,last_scan,status,message) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (file_id, 'codex', 'synthetic', 1, 1, '2026-09-01T00:00:00Z', 'ready', ''),
        )
        for index, (occurred_at, total) in enumerate(rows):
            connection.execute(
                """
                INSERT INTO usage_events(
                  event_id,file_id,agent,route,platform,model,session_id,occurred_at,
                  input_tokens,cached_input_tokens,cache_write_tokens,output_tokens,
                  reasoning_tokens,total_tokens
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f'legacy-{index}', file_id, 'codex', '原生订阅', 'OpenAI',
                    '未识别模型', PARENT, occurred_at, total, 0, 0, 0, 0, total,
                ),
            )


def test_shortened_log_retains_verified_events_and_original_dates(tmp_path):
    db = TokenDatabase(tmp_path / 'data.db')
    path = write_log(tmp_path, PARENT, [record(100), record(200), record(300)])
    save(db, 'file', parse(tmp_path, path))
    path = write_log(tmp_path, PARENT, [record(300, stamp='2026-09-02T00:00:00Z')])
    save(db, 'file', parse(tmp_path, path))
    rows = db.usage_rows()
    assert len(rows) == 3
    assert sum(e['total_tokens'] for e in rows) == 300
    assert all(e['occurred_at'].startswith('2026-09-01') for e in rows)
    assert db.provider_states() == []
    with db.connect() as c:
        assert c.execute("SELECT event_count FROM file_states WHERE file_id='file'").fetchone()[0] == 3


def test_archived_or_moved_log_is_counted_once(tmp_path):
    db = TokenDatabase(tmp_path / 'data.db')
    path = write_log(tmp_path, PARENT, [record(100), record(200)])
    save(db, 'old', parse(tmp_path, path))
    path = write_log(tmp_path, PARENT, [record(100), record(200)], archived=True)
    save(db, 'new', parse(tmp_path, path))
    db.remove_missing_files('codex', {'new'})
    assert sum(e['total_tokens'] for e in db.usage_rows()) == 200
    assert len(db.usage_rows()) == 2
    db.remove_missing_files('codex', set())
    assert sum(e['total_tokens'] for e in db.usage_rows()) == 200


def test_later_parent_evidence_removes_inherited_rows(tmp_path):
    db = TokenDatabase(tmp_path / 'data.db')
    path = write_log(tmp_path, CHILD, [record(100), record(200), record(250, last=50)], parent=PARENT)
    save(db, 'child', parse(tmp_path, path))
    write_log(tmp_path, PARENT, [record(100), record(200)])
    save(db, 'child', parse(tmp_path, path))
    assert sum(e['total_tokens'] for e in db.usage_rows()) == 50


def test_shortened_legacy_history_is_kept_without_duplicate_current_event(tmp_path):
    db = TokenDatabase(tmp_path / 'data.db')
    path = write_log(tmp_path, PARENT, [record(100, stamp='2026-09-01T00:00:00Z')])
    insert_legacy_rows(db, 'file', [
        ('2026-09-01T00:00:00Z', 100),
        ('2026-09-01T00:01:00Z', 200),
    ])
    save(db, 'file', parse(tmp_path, path))
    rows = db.usage_rows()
    assert len(rows) == 2
    assert sum(row['total_tokens'] for row in rows) == 300
    with db.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM usage_events WHERE event_id LIKE 'codex-v2:h:%'"
        ).fetchone()[0] == 1


def test_inherited_signature_removes_old_legacy_copy(tmp_path):
    db = TokenDatabase(tmp_path / 'data.db')
    write_log(tmp_path, PARENT, [record(100), record(200)])
    path = write_log(tmp_path, CHILD, [record(100), record(200), record(250, last=50)], parent=PARENT)
    insert_legacy_rows(db, 'child', [
        ('2026-09-01T00:00:00Z', 100),
        ('2026-09-01T00:00:00Z', 100),
        ('2026-09-01T00:00:00Z', 50),
    ])
    save(db, 'child', parse(tmp_path, path))
    rows = db.usage_rows()
    assert len(rows) == 1
    assert rows[0]['total_tokens'] == 50


def test_trimmed_fork_migration_uses_parent_evidence(tmp_path):
    db = TokenDatabase(tmp_path / 'data.db')
    write_log(tmp_path, PARENT, [record(100), record(200)])
    path = write_log(tmp_path, CHILD, [record(250, last=50)], parent=PARENT)
    insert_legacy_rows(db, 'child', [
        ('2026-09-01T00:00:00Z', 100),
        ('2026-09-01T00:00:00Z', 100),
        ('2026-09-01T00:00:00Z', 50),
    ])
    save(db, 'child', parse(tmp_path, path))
    rows = db.usage_rows()
    assert len(rows) == 1
    assert rows[0]['total_tokens'] == 50
