"""端到端 Web 流程测试：上传 → 勾选/剔除 → 预览 → 下载 / 冲突阻止。"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", str(tmp_path / "data"))
    main.SESSIONS.clear()
    return TestClient(main.app)


def _upload(client, sample_db):
    with open(sample_db, "rb") as f:
        resp = client.post(
            "/upload",
            files={"file": ("lab.db", f, "application/x-sqlite3")},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/select"


def test_full_flow_download(client, sample_db, tmp_path):
    _upload(client, sample_db)

    resp = client.get("/select")
    assert resp.status_code == 200
    assert "墙体试件A" in resp.text and "背景噪声对照测量" in resp.text

    resp = client.post("/select", data={"test": ["1"]}, follow_redirects=False)
    assert resp.status_code == 303

    resp = client.get("/preview")
    assert resp.status_code == 200
    assert "确认并下载" in resp.text
    assert "901" in resp.text  # 跨组带入的对照读数出现在预览中

    resp = client.post("/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-sqlite3")
    out = tmp_path / "downloaded.db"
    out.write_bytes(resp.content)
    conn = sqlite3.connect(out)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    reading_ids = {r[0] for r in conn.execute("SELECT id FROM readings")}
    assert reading_ids == {101, 102, 103, 901}
    conn.close()


def test_conflict_blocks_download(client, sample_db):
    _upload(client, sample_db)
    # 勾选 T1 与 T9，并剔除被 T1 读数引用的对照读数 R901
    resp = client.post(
        "/select",
        data={"test": ["1", "9"], "excl": ["901"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = client.get("/preview")
    assert resp.status_code == 200
    assert "已阻止下载" in resp.text
    assert "剔除" in resp.text  # 引用链与冲突原因展示在预览页

    resp = client.post("/download")
    assert resp.status_code == 409
    assert "阻止" in resp.text


def test_exclusion_of_unselected_test_reading_ignored(client, sample_db):
    """剔除未勾选试验的读数被静默忽略，不影响交付。"""
    _upload(client, sample_db)
    client.post("/select", data={"test": ["1"], "excl": ["901"]})
    resp = client.get("/preview")
    assert "确认并下载" in resp.text
    resp = client.post("/download")
    assert resp.status_code == 200


def test_upload_rejects_bad_file(client, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not sqlite")
    with open(bad, "rb") as f:
        resp = client.post("/upload", files={"file": ("bad.db", f)})
    assert resp.status_code == 400
    assert "SQLite" in resp.text


def test_upload_rejects_wrong_schema(client, tmp_path):
    wrong = tmp_path / "wrong.db"
    conn = sqlite3.connect(wrong)
    conn.execute("CREATE TABLE whatever (id INTEGER)")
    conn.commit()
    conn.close()
    with open(wrong, "rb") as f:
        resp = client.post("/upload", files={"file": ("wrong.db", f)})
    assert resp.status_code == 400
    assert "缺少数据表" in resp.text


def test_pages_require_session(client):
    assert client.get("/select", follow_redirects=False).status_code == 303
    assert client.get("/preview", follow_redirects=False).status_code == 303
