"""工艺差分析演示样例：一个 SQLite 库 + 两版标注 CSV。

场景：两种装配工艺（甲、乙）常在不同舱室试验，另有第三种工艺丙的读数。
- annotations_v1.csv：甲只标注在舱A、乙只标注在舱B（装夹相同），
  工艺差与舱室效应混杂 → 无法估计；
- annotations_v2.csv：在 v1 基础上补标读数 1006（工艺乙在舱A的补测），
  标注出现交叉 → 工艺差变为可估计（甲 − 乙 = 3.1 dB）。

被 scripts/make_analysis_sample.py（命令行）与测试共同使用。
"""
from __future__ import annotations

import csv
import os
import sqlite3

from .sampledb import DDL

GROUPS = [
    (1, "分段试件-2026春", "两种装配工艺对比试验"),
]

CALIBRATIONS = [
    (1, "声级计 NL-52", "2026-04-01", "CAL-2026-0401"),
]

TESTS = [
    (11, 1, 1, "工艺甲-舱A-500Hz", "GB/T 19889.3", "2026-04-10"),
    (12, 1, 1, "工艺乙-舱B-500Hz", "GB/T 19889.3", "2026-04-11"),
    (13, 1, 1, "工艺丙-舱A-500Hz", "GB/T 19889.3", "2026-04-12"),
    (14, 1, 1, "工艺乙-舱A-补测", "GB/T 19889.3", "2026-04-18"),
    (15, 1, None, "其他测量", "GB/T 19889.3", "2026-04-19"),
]

# (id, test_id, calibration_id, control_reading_id, kind, freq, level, note)
READINGS = [
    (1001, 11, 1, None, "measurement", 500.0, 40.3, None),
    (1002, 11, 1, None, "measurement", 500.0, 39.7, None),
    (1003, 12, 1, None, "measurement", 500.0, 39.2, None),
    (1004, 12, None, None, "measurement", 500.0, 38.8, None),
    (1005, 13, 1, None, "measurement", 500.0, 41.1, None),
    (1006, 14, 1, None, "measurement", 500.0, 36.9, "补测：工艺乙在舱A"),
    (1007, 15, None, None, "measurement", 500.0, 44.4, "未标注读数"),
    (1008, 15, None, None, "measurement", 250.0, 35.0, "其他频点"),
]

# (reading_id, process, cabin, fixture)
ANNOTATIONS_V1 = [
    (1001, "甲", "舱A", "夹具X"),
    (1002, "甲", "舱A", "夹具X"),
    (1003, "乙", "舱B", "夹具X"),
    (1004, "乙", "舱B", "夹具X"),
    (1005, "丙", "舱A", "夹具X"),
]

# v2 = v1 + 补标 1006（工艺乙在舱A）：标注出现交叉，工艺差变为可估计
ANNOTATIONS_V2 = ANNOTATIONS_V1 + [
    (1006, "乙", "舱A", "夹具X"),
]

CSV_HEADER = ["reading_id", "process", "cabin", "fixture"]


def create_analysis_sample(outdir: str) -> dict[str, str]:
    """在 outdir 下生成演示库与两版标注 CSV，返回各文件路径。"""
    os.makedirs(outdir, exist_ok=True)
    db_path = os.path.join(outdir, "analysis_sample.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(DDL)
        conn.executemany("INSERT INTO groups VALUES (?,?,?)", GROUPS)
        conn.executemany("INSERT INTO calibrations VALUES (?,?,?,?)", CALIBRATIONS)
        conn.executemany("INSERT INTO tests VALUES (?,?,?,?,?,?)", TESTS)
        conn.executemany("INSERT INTO readings VALUES (?,?,?,?,?,?,?,?)", READINGS)
        conn.commit()
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"分析演示库外键校验失败：{violations}")
    finally:
        conn.close()

    paths = {"db": db_path}
    for tag, rows in (("v1", ANNOTATIONS_V1), ("v2", ANNOTATIONS_V2)):
        path = os.path.join(outdir, f"annotations_{tag}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(CSV_HEADER)
            w.writerows(rows)
        paths[tag] = path
    return paths
