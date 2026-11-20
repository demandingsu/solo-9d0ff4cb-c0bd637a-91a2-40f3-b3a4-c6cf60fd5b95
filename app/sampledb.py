"""生成演示用 SQLite 库：含跨组对照引用、读数级/试验级校准引用。

被 scripts/make_sample_db.py（命令行）与测试共同使用。
"""
from __future__ import annotations

import sqlite3

DDL = """
CREATE TABLE groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT
);
CREATE TABLE calibrations (
    id INTEGER PRIMARY KEY,
    device TEXT NOT NULL,
    calibrated_on TEXT NOT NULL,
    certificate TEXT
);
CREATE TABLE tests (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES groups(id),
    calibration_id INTEGER REFERENCES calibrations(id),
    name TEXT NOT NULL,
    standard TEXT,
    conducted_on TEXT
);
CREATE TABLE readings (
    id INTEGER PRIMARY KEY,
    test_id INTEGER NOT NULL REFERENCES tests(id),
    calibration_id INTEGER REFERENCES calibrations(id),
    control_reading_id INTEGER REFERENCES readings(id),
    kind TEXT NOT NULL DEFAULT 'measurement',
    frequency_hz REAL,
    level_db REAL,
    note TEXT
);
CREATE INDEX idx_readings_test ON readings(test_id);
CREATE INDEX idx_readings_control ON readings(control_reading_id);
CREATE INDEX idx_tests_group ON tests(group_id);
"""

GROUPS = [
    (1, "墙体试件A", "240mm 混凝土砌块墙"),
    (2, "墙体试件B", "双层石膏板轻钢龙骨墙"),
    (3, "公共设施", "背景噪声与对照测量"),
]

CALIBRATIONS = [
    (1, "声级计 NL-52", "2026-01-15", "CAL-2026-0115"),
    (2, "参考麦克风 GRAS 40AE", "2026-02-03", "CAL-2026-0203"),
]

TESTS = [
    (1, 1, 1, "A墙-计权隔声-第1次", "GB/T 19889.3", "2026-03-02"),
    (2, 1, 1, "A墙-计权隔声-第2次", "GB/T 19889.3", "2026-03-03"),
    (3, 2, 1, "B墙-计权隔声-第1次", "GB/T 19889.3", "2026-03-05"),
    (9, 3, 2, "背景噪声对照测量", "GB/T 19889.3", "2026-03-01"),
]

# (id, test_id, calibration_id, control_reading_id, kind, freq, level, note)
READINGS = [
    # T1：跨组引用 G3/T9 的对照读数 R901
    (101, 1, 1, 901, "measurement", 100.0, 52.1, None),
    (102, 1, 1, 901, "measurement", 125.0, 51.0, None),
    (103, 1, None, None, "measurement", 160.0, 49.8, None),
    # T2：无引用
    (111, 2, 1, None, "measurement", 100.0, 52.4, None),
    (112, 2, None, None, "measurement", 125.0, 50.7, "疑似异常，可考虑剔除"),
    # T3：引用 R902，R902 再引用 R901（跨组链式引用）
    (201, 3, 1, 902, "measurement", 100.0, 55.0, None),
    (202, 3, None, None, "measurement", 125.0, 54.2, None),
    # T9：对照读数，属未勾选组时仅沿引用带入
    (901, 9, 2, None, "control", 100.0, 30.2, None),
    (902, 9, 2, 901, "control", 125.0, 29.8, None),
    (903, 9, None, None, "control", 160.0, 29.1, None),
]


def create_sample_db(path: str) -> str:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(DDL)
        conn.executemany("INSERT INTO groups VALUES (?,?,?)", GROUPS)
        conn.executemany("INSERT INTO calibrations VALUES (?,?,?,?)", CALIBRATIONS)
        conn.executemany("INSERT INTO tests VALUES (?,?,?,?,?,?)", TESTS)
        conn.executemany("INSERT INTO readings VALUES (?,?,?,?,?,?,?,?)", READINGS)
        conn.commit()
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"演示库外键校验失败：{violations}")
    finally:
        conn.close()
    return path
