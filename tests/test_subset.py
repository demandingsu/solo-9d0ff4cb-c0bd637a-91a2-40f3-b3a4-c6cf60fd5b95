"""子库生成测试：主键/外键约束保留、行内容一致、外键校验通过。"""
import sqlite3

from app.core import build_subset, resolve


def test_subset_preserves_pk_fk_and_rows(model, sample_db, tmp_path):
    res = resolve(model, {1}, set())
    assert res.ok
    out = tmp_path / "subset.db"
    counts = build_subset(sample_db, str(out), res)

    conn = sqlite3.connect(out)
    # 外键完整性
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    # 建表 DDL 保留主键与外键约束
    ddl_tests = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tests'"
    ).fetchone()[0].upper()
    assert "PRIMARY KEY" in ddl_tests
    assert "REFERENCES" in ddl_tests
    ddl_readings = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='readings'"
    ).fetchone()[0].upper()
    assert "PRIMARY KEY" in ddl_readings
    assert ddl_readings.count("REFERENCES") >= 3  # test/calibration/control 三个外键
    # 索引一并复制
    indexes = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
        )
    }
    assert "idx_readings_test" in indexes
    # 行主键保持原值，数量与解析结果一致
    for table, key in [
        ("groups", "group_ids"),
        ("tests", "test_ids"),
        ("readings", "reading_ids"),
        ("calibrations", "calibration_ids"),
    ]:
        ids = {r[0] for r in conn.execute(f"SELECT id FROM {table}")}
        assert ids == getattr(res, key)
        assert counts[table] == len(ids)
    conn.close()


def test_subset_rows_match_source(model, sample_db, tmp_path):
    """子库行内容与源库逐列一致。"""
    res = resolve(model, {1, 3}, set())
    out = tmp_path / "subset.db"
    build_subset(sample_db, str(out), res)
    src, sub = sqlite3.connect(sample_db), sqlite3.connect(out)
    for table in ("groups", "calibrations", "tests", "readings"):
        src_rows = src.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        sub_rows = sub.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        assert all(row in src_rows for row in sub_rows)
    src.close()
    sub.close()


def test_subset_foreign_keys_enforced(model, sample_db, tmp_path):
    """子库外键约束真实生效：手工插入违规行应被拒绝。"""
    res = resolve(model, {1}, set())
    out = tmp_path / "subset.db"
    build_subset(sample_db, str(out), res)
    conn = sqlite3.connect(out)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute(
            "INSERT INTO readings (id, test_id, kind) VALUES (99999, 99999, 'x')"
        )
        conn.commit()
        raise AssertionError("外键约束未生效")
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()


def test_empty_selection_yields_empty_db(model, sample_db, tmp_path):
    """不勾选任何试验：子库结构完整、无数据行。"""
    res = resolve(model, set(), set())
    assert res.ok
    out = tmp_path / "subset.db"
    build_subset(sample_db, str(out), res)
    conn = sqlite3.connect(out)
    for table in ("groups", "calibrations", "tests", "readings"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    conn.close()
