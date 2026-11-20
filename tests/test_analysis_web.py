"""工艺差分析 Web 流程测试：上传标注 → 选频点/工艺 → 可估计性结论 → 整份更正。"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import main
from app.analyzesample import create_analysis_sample


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", str(tmp_path / "data"))
    main.SESSIONS.clear()
    return TestClient(main.app)


@pytest.fixture()
def sample(tmp_path):
    """演示样例：v1 标注下工艺差无法估计，v2 补标后可估计。"""
    return create_analysis_sample(str(tmp_path / "sample"))


def _upload_db(client, db_path):
    with open(db_path, "rb") as f:
        resp = client.post(
            "/upload",
            files={"file": ("lab.db", f, "application/x-sqlite3")},
            follow_redirects=False,
        )
    assert resp.status_code == 303


def _upload_csv(client, csv_path, expect=303):
    with open(csv_path, "rb") as f:
        return client.post(
            "/analysis/annotations",
            files={"file": (csv_path.split("/")[-1], f, "text/csv")},
            follow_redirects=False,
        )


def _result(client, pa="甲", pb="乙", freq="500.0"):
    return client.get(
        "/analysis/result", params={"freq": freq, "pa": pa, "pb": pb}
    )


def test_analysis_requires_session(client):
    assert client.get("/analysis", follow_redirects=False).status_code == 303


def test_estimable_after_supplementary_annotation(client, sample):
    """补标前无法估计 → 整份更正（补标交叉组合）后可估计，版本号递增。"""
    _upload_db(client, sample["db"])

    resp = client.get("/analysis")
    assert resp.status_code == 200
    assert "尚未上传标注表" in resp.text

    # 第一版标注：甲只在舱A、乙只在舱B → 混杂
    assert _upload_csv(client, sample["v1"]).status_code == 303
    resp = client.get("/analysis")
    assert "版本 #1" in resp.text

    resp = _result(client)
    assert resp.status_code == 200
    assert "版本 #1" in resp.text  # 结论与参与读数来自同一版标注
    assert "无法估计" in resp.text
    assert "解①" in resp.text and "解②" in resp.text
    # 参与读数含其他工艺（丙）的读数
    for rid in (1001, 1002, 1003, 1004, 1005):
        assert f"#{rid}" in resp.text
    assert "#1006" not in resp.text  # v1 未标注
    assert "#1007" not in resp.text  # 始终未标注

    # 整份更正：补标 1006（工艺乙在舱A）→ 可估计
    assert _upload_csv(client, sample["v2"]).status_code == 303
    resp = client.get("/analysis")
    assert "版本 #2" in resp.text

    resp = _result(client)
    assert resp.status_code == 200
    assert "版本 #2" in resp.text
    assert "最优解给出的工艺差都相同" in resp.text
    assert "3.1" in resp.text  # 工艺差 甲 − 乙 = 3.1 dB
    assert "#1006" in resp.text  # 补标读数参与拟合


def test_rejected_csv_keeps_previous_table(client, sample):
    """含未知读数编号的更正表被拒收，原标注版本与结论保持不变。"""
    _upload_db(client, sample["db"])
    _upload_csv(client, sample["v1"])

    bad = b"reading_id,process,cabin,fixture\n9999,\xe7\x94\xb2,\xe8\x88\xb1A,\xe5\xa4\xb9\xe5\x85\xb7X\n"
    resp = client.post(
        "/analysis/annotations",
        files={"file": ("bad.csv", bad, "text/csv")},
    )
    assert resp.status_code == 400
    assert "9999" in resp.text and "拒收" in resp.text
    assert "版本 #1" in resp.text  # 原表仍在

    resp = _result(client)
    assert resp.status_code == 200
    assert "版本 #1" in resp.text
    assert "无法估计" in resp.text  # 原结论仍可复现


def test_conflicting_annotation_rejected(client, sample):
    _upload_db(client, sample["db"])
    conflict = (
        "reading_id,process,cabin,fixture\n"
        "1001,甲,舱A,夹具X\n"
        "1001,乙,舱A,夹具X\n"
    ).encode("utf-8")
    resp = client.post(
        "/analysis/annotations", files={"file": ("c.csv", conflict, "text/csv")}
    )
    assert resp.status_code == 400
    assert "冲突" in resp.text
    assert "尚未上传标注表" in resp.text  # 没有可回退的版本


def test_identical_duplicate_rows_deduped(client, sample):
    _upload_db(client, sample["db"])
    dup = (
        "reading_id,process,cabin,fixture\n"
        "1001,甲,舱A,夹具X\n"
        "1001,甲,舱A,夹具X\n"
        "1003,乙,舱B,夹具X\n"
    ).encode("utf-8")
    resp = client.post(
        "/analysis/annotations",
        files={"file": ("d.csv", dup, "text/csv")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    resp = client.get("/analysis")
    assert "去重" in resp.text
    assert "版本 #1" in resp.text


def _make_db_missing_column(tmp_path, drop_col):
    path = tmp_path / f"old_{drop_col}.db"
    conn = sqlite3.connect(path)
    cols = [
        "id INTEGER PRIMARY KEY",
        "test_id INTEGER NOT NULL REFERENCES tests(id)",
        "calibration_id INTEGER REFERENCES calibrations(id)",
        "control_reading_id INTEGER REFERENCES readings(id)",
        "kind TEXT NOT NULL DEFAULT 'measurement'",
        "frequency_hz REAL",
        "level_db REAL",
    ]
    cols = [c for c in cols if not c.startswith(drop_col + " ")]
    conn.executescript(
        "CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL);"
        "CREATE TABLE calibrations (id INTEGER PRIMARY KEY, device TEXT NOT NULL);"
        "CREATE TABLE tests (id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL,"
        " calibration_id INTEGER, name TEXT NOT NULL);"
        f"CREATE TABLE readings ({', '.join(cols)});"
    )
    conn.execute("INSERT INTO groups VALUES (1, 'g')")
    conn.execute("INSERT INTO tests VALUES (1, 1, NULL, 't')")
    conn.commit()
    conn.close()
    return str(path)


def test_old_db_missing_level_column_explained(client, tmp_path):
    """旧库缺数值列：分析页说明原因，不报错崩溃。"""
    _upload_db(client, _make_db_missing_column(tmp_path, "level_db"))
    resp = client.get("/analysis")
    assert resp.status_code == 200
    assert "无法进行分析" in resp.text
    assert "level_db" in resp.text


def test_old_db_missing_frequency_column_explained(client, tmp_path):
    _upload_db(client, _make_db_missing_column(tmp_path, "frequency_hz"))
    resp = client.get("/analysis")
    assert resp.status_code == 200
    assert "无法进行分析" in resp.text
    assert "frequency_hz" in resp.text


def test_result_redirects_without_annotations(client, sample):
    _upload_db(client, sample["db"])
    resp = _result(client)
    # 跟随重定向到分析页并给出提示
    assert "请先上传标注表" in resp.text


def test_same_process_rejected(client, sample):
    _upload_db(client, sample["db"])
    _upload_csv(client, sample["v1"])
    resp = _result(client, pa="甲", pb="甲")
    assert "请选择两种不同的工艺" in resp.text


def test_process_not_annotated_at_frequency(client, sample):
    """工艺在该频点无标注（例如标注表已更正）→ 提示并回到分析页。"""
    _upload_db(client, sample["db"])
    _upload_csv(client, sample["v1"])
    resp = _result(client, pa="甲", pb="丁")
    assert "没有已标注读数" in resp.text


def test_analysis_entry_linked_from_select_page(client, sample):
    _upload_db(client, sample["db"])
    resp = client.get("/select")
    assert "/analysis" in resp.text


def test_null_level_reading_listed_as_skipped(client, sample):
    """已标注但 level_db 为空的读数不参与拟合，并在结果页单独列出。"""
    conn = sqlite3.connect(sample["db"])
    conn.execute("UPDATE readings SET level_db = NULL WHERE id = 1002")
    conn.commit()
    conn.close()
    _upload_db(client, sample["db"])
    _upload_csv(client, sample["v1"])
    resp = _result(client)
    assert resp.status_code == 200
    assert "未参与拟合" in resp.text and "#1002" in resp.text
    assert "参与读数编号：#1001、#1003、#1004、#1005" in resp.text.replace("\n", "")
