"""标注表解析与三因素加性模型拟合测试。"""
import sqlite3
from fractions import Fraction

import pytest

from app.analyzesample import create_analysis_sample
from app.analyze import (
    FitRow,
    available_frequencies,
    collect_rows,
    fit_additive,
    parse_annotations,
    processes_at,
    schema_problems,
)
from app.core import load_model

VALID_IDS = {1001, 1002, 1003, 1004, 1005, 1006, 1007, 1008}


def _csv(text: str, encoding: str = "utf-8") -> bytes:
    return text.encode(encoding)


# ---------------------------------------------------------------- 标注表解析


def test_parse_ok_english_header():
    data = _csv("reading_id,process,cabin,fixture\n1001,甲,舱A,夹具X\n1002,乙,舱B,夹具Y\n")
    table, errors = parse_annotations(data, VALID_IDS)
    assert errors == []
    assert table.size == 2
    assert table.rows[1001].process == "甲"
    assert table.rows[1002].fixture == "夹具Y"


def test_parse_ok_chinese_header_and_gb18030():
    data = _csv("读数编号,工艺,舱室,装夹方式\n1003,甲,舱A,夹具X\n", "gb18030")
    table, errors = parse_annotations(data, VALID_IDS)
    assert errors == []
    assert table.rows[1003].cabin == "舱A"


def test_parse_ok_with_bom_and_extra_columns():
    text = "﻿reading_id,process,cabin,fixture,备注\n1001,甲,舱A,夹具X,忽略我\n"
    data = text.encode("utf-8")
    assert data.startswith(b"\xef\xbb\xbf")  # 带 BOM
    table, errors = parse_annotations(data, VALID_IDS)
    assert errors == []
    assert table.size == 1


def test_parse_rejects_unknown_reading_id():
    data = _csv("reading_id,process,cabin,fixture\n9999,甲,舱A,夹具X\n")
    table, errors = parse_annotations(data, VALID_IDS)
    assert table is None
    assert any("9999" in e and "不存在" in e for e in errors)


def test_parse_rejects_conflicting_duplicate():
    data = _csv(
        "reading_id,process,cabin,fixture\n"
        "1001,甲,舱A,夹具X\n"
        "1001,乙,舱A,夹具X\n"
    )
    table, errors = parse_annotations(data, VALID_IDS)
    assert table is None
    assert any("冲突" in e and "1001" in e for e in errors)


def test_parse_dedupes_identical_rows():
    data = _csv(
        "reading_id,process,cabin,fixture\n"
        "1001,甲,舱A,夹具X\n"
        "1001,甲,舱A,夹具X\n"
    )
    table, errors = parse_annotations(data, VALID_IDS)
    assert errors == []
    assert table.size == 1
    assert table.duplicates == 1


def test_parse_rejects_missing_column():
    data = _csv("reading_id,process,cabin\n1001,甲,舱A\n")
    table, errors = parse_annotations(data, VALID_IDS)
    assert table is None
    assert any("fixture" in e for e in errors)


def test_parse_rejects_non_integer_and_empty_fields():
    data = _csv(
        "reading_id,process,cabin,fixture\n"
        "abc,甲,舱A,夹具X\n"
        "1002,,舱A,夹具X\n"
    )
    table, errors = parse_annotations(data, VALID_IDS)
    assert table is None
    assert any("整数" in e for e in errors)
    assert any("空值" in e for e in errors)


def test_parse_rejects_bad_encoding():
    table, errors = parse_annotations(b"\xff\xfe\x00\x01\x02", VALID_IDS)
    assert table is None
    assert any("编码" in e for e in errors)


# ---------------------------------------------------------------- 拟合与可估计性


def _row(rid, proc, cabin, fixture, y):
    return FitRow(rid, proc, cabin, fixture, Fraction(str(y)))


def test_fit_estimable_simple_exact():
    """单一舱室/装夹下两种工艺：工艺差 = 两组均值差，可精确手算。"""
    rows = [
        _row(1, "甲", "A", "X", "10"),
        _row(2, "甲", "A", "X", "12"),
        _row(3, "乙", "A", "X", "7"),
        _row(4, "乙", "A", "X", "8"),
    ]
    res = fit_additive(rows, "甲", "乙")
    assert res.estimable
    assert res.delta == Fraction(7, 2)  # 11 − 7.5
    assert res.rss == Fraction(5, 2)  # 1+1+0.25+0.25
    assert res.alt_beta is None


def test_fit_confounded_gives_two_optimal_solutions():
    """甲只在舱A、乙只在舱B：工艺差与舱室效应混杂，不可估计。"""
    rows = [
        _row(1, "甲", "舱A", "X", "40.3"),
        _row(2, "甲", "舱A", "X", "39.7"),
        _row(3, "乙", "舱B", "X", "39.2"),
        _row(4, "乙", "舱B", "X", "38.8"),
    ]
    res = fit_additive(rows, "甲", "乙")
    assert not res.estimable
    # 两组解同为最优：逐条拟合值完全相同
    assert res.alt_fitted == res.fitted
    # 工艺差不同：按约定恰好相差 1 dB
    assert res.alt_delta == res.delta + 1
    # 调整方向非空且只含系数标签与数值
    assert res.direction
    assert all(isinstance(w, Fraction) for _, w in res.direction)


# ---------------------------------------------------------------- 演示样例（端到端数值）


@pytest.fixture()
def analysis_model(tmp_path):
    paths = create_analysis_sample(str(tmp_path / "sample"))
    conn = sqlite3.connect(paths["db"])
    try:
        yield load_model(conn), paths
    finally:
        conn.close()


def _rows_from_csv(model, csv_path):
    with open(csv_path, "rb") as f:
        table, errors = parse_annotations(f.read(), set(model.readings))
    assert errors == []
    rows, skipped = collect_rows(model, table, 500.0)
    return rows, skipped


def test_sample_v1_not_estimable_v2_estimable(analysis_model):
    """补标前无法估计，补标一条交叉标注后可估计（甲 − 乙 = 3.1 dB）。"""
    model, paths = analysis_model

    rows_v1, skipped_v1 = _rows_from_csv(model, paths["v1"])
    assert skipped_v1 == []
    assert {r.reading_id for r in rows_v1} == {1001, 1002, 1003, 1004, 1005}
    res_v1 = fit_additive(rows_v1, "甲", "乙")
    assert not res_v1.estimable
    assert res_v1.alt_fitted == res_v1.fitted

    rows_v2, _ = _rows_from_csv(model, paths["v2"])
    assert {r.reading_id for r in rows_v2} == {1001, 1002, 1003, 1004, 1005, 1006}
    res_v2 = fit_additive(rows_v2, "甲", "乙")
    assert res_v2.estimable
    assert res_v2.delta == Fraction(31, 10)  # 3.1 dB（手算核对）
    assert res_v2.rss == Fraction(13, 50)  # 0.26 dB²（手算核对）


def test_sample_frequencies_and_processes(analysis_model):
    model, paths = analysis_model
    with open(paths["v2"], "rb") as f:
        table, _ = parse_annotations(f.read(), set(model.readings))
    freqs = available_frequencies(model, table)
    assert freqs == [(500.0, 6)]  # 1007 未标注、1008 在 250 Hz 且未标注
    assert processes_at(model, table, 500.0) == ["丙", "乙", "甲"]  # 按码点排序


def test_collect_rows_skips_null_level(analysis_model):
    """已标注但 level_db 为空的读数被跳过并列入 skipped。"""
    model, paths = analysis_model
    conn = sqlite3.connect(paths["db"])
    conn.execute("UPDATE readings SET level_db = NULL WHERE id = 1002")
    conn.commit()
    conn.close()
    conn = sqlite3.connect(paths["db"])
    model2 = load_model(conn)
    conn.close()
    with open(paths["v1"], "rb") as f:
        table, _ = parse_annotations(f.read(), set(model2.readings))
    rows, skipped = collect_rows(model2, table, 500.0)
    assert skipped == [1002]
    assert 1002 not in {r.reading_id for r in rows}


def test_schema_problems_reports_missing_columns():
    assert schema_problems({"id", "frequency_hz", "level_db"}) == []
    problems = schema_problems({"id"})
    assert any("frequency_hz" in p for p in problems)
    assert any("level_db" in p for p in problems)
