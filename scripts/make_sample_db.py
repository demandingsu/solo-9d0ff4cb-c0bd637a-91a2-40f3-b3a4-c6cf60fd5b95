#!/usr/bin/env python3
"""生成演示数据库（含跨组对照引用），用法：

    python3 scripts/make_sample_db.py [输出路径，默认 sample.db]
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app.sampledb import create_sample_db

if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "sample.db"
    create_sample_db(out)
    print(f"已生成演示库：{out}")
