from __future__ import annotations

import sqlite3
from pathlib import Path

from tokenledger import native


def test_native_number_and_percent_formatting() -> None:
    assert native.compact_number(333_334_232) == "3.33 亿"
    assert native.compact_number(372_311) == "37.23 万"
    assert native.compact_number(999) == "999"
    assert native.percent(native.ratio_percent(0.9136)) == "91.4%"
    assert native.percent(None) == "未提供"


def test_native_metric_rows_keep_the_complete_token_breakdown() -> None:
    rows = native.metric_rows(
        {
            "input": 120,
            "cached_input": 80,
            "cache_write": 15,
            "output": 30,
            "reasoning": 12,
            "net_usage": 70,
        }
    )
    assert rows == [
        ("输入", "120"),
        ("缓存读取", "80"),
        ("缓存写入", "15"),
        ("输出", "30"),
        ("推理", "12"),
        ("净用量", "70"),
    ]


def test_native_migrates_legacy_database_with_sqlite_backup(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "legacy" / "token-ledger.db"
    source.parent.mkdir(parents=True)
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE marker(value TEXT)")
        connection.execute("INSERT INTO marker VALUES('ok')")
    target = tmp_path / "appdata" / "token-ledger.db"
    monkeypatch.setattr(native, "_source_database_candidates", lambda: [source])
    assert native.migrate_legacy_database(target) == source
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "ok"
