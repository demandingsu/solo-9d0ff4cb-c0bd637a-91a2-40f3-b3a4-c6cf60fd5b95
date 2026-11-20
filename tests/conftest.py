import sqlite3

import pytest

from app.sampledb import create_sample_db


@pytest.fixture()
def sample_db(tmp_path):
    """干净的演示库：含跨组对照引用、链式引用、两级校准引用。"""
    path = tmp_path / "sample.db"
    create_sample_db(str(path))
    return str(path)


@pytest.fixture()
def model(sample_db):
    from app.core import load_model

    conn = sqlite3.connect(sample_db)
    try:
        return load_model(conn)
    finally:
        conn.close()
