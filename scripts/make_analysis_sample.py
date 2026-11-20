#!/usr/bin/env python3
"""生成工艺差分析演示样例（SQLite 库 + 两版标注 CSV），用法：

    python3 scripts/make_analysis_sample.py [输出目录，默认 analysis_sample]

演练：上传 analysis_sample.db → 分析页上传 annotations_v1.csv →
选 500 Hz、工艺甲/乙 → 工艺差无法估计；再整份替换为 annotations_v2.csv
（补标读数 1006：工艺乙在舱A）→ 同一查询变为可估计（甲 − 乙 = 3.1 dB）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app.analyzesample import create_analysis_sample

if __name__ == "__main__":
    outdir = sys.argv[1] if len(sys.argv) > 1 else "analysis_sample"
    paths = create_analysis_sample(outdir)
    print("已生成工艺差分析演示样例：")
    for key in ("db", "v1", "v2"):
        print(f"  {paths[key]}")
