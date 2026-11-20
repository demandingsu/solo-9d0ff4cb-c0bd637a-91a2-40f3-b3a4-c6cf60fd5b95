# 隔声试验数据交付复核工具

声学实验室向外部复核方交付隔声试验数据的网页工具：从上传的 SQLite 库中勾选试验、
剔除个别异常读数，沿对照/校准引用自动补齐所需记录，确认预览后下载 SQLite 子库。

## 运行

```bash
docker compose up --build
# 打开 http://localhost:8000
```

本地开发（Python 3.11+）：

```bash
pip install -r requirements-dev.txt
python3 scripts/make_sample_db.py sample.db   # 生成演示库
uvicorn app.main:app --reload
```

运行测试：`pytest`

## 数据库结构约定

上传的 SQLite 库需包含四张表（关键列名固定，其余列任意）：

| 表 | 关键列 | 说明 |
| --- | --- | --- |
| `groups` | `id`, `name` | 试验组 |
| `calibrations` | `id`, `device` | 校准记录 |
| `tests` | `id`, `group_id→groups`, `calibration_id→calibrations`(可空), `name` | 试验 |
| `readings` | `id`, `test_id→tests`, `calibration_id→calibrations`(可空), `control_reading_id→readings`(可空), `kind` | 读数；`control_reading_id` 为对照读数引用，允许跨组 |

## 交付规则

1. 交付所选试验中未被剔除的全部读数；
2. 沿对照引用（`control_reading_id`）与校准引用（`calibration_id`，读数级与试验级）
   补齐所需记录；对照读数允许跨组互引；
3. 跨到未勾选的组时，只带入沿引用找到的读数，以及这些读数所属的试验、
   试验所属的组（维持外键完整所必需）和引用到的校准记录；
4. 引用悬空（目标不存在）或指向已剔除读数时，预览页展示完整引用链并**阻止下载**；
5. 子库建表语句复用源库 DDL 原文，主键、外键约束、索引原样保留，行按原主键写入，
   导出后执行 `PRAGMA foreign_key_check` 校验。

## 使用流程

1. **上传**：选择实验室导出的 SQLite 库（结构不符会给出具体缺失项）；
2. **选择**：按组勾选要交付的试验，可在所选试验下勾选要剔除的异常读数；
3. **预览**：查看交付汇总、跨组带入的读数及其引用链；若存在引用冲突，
   展示每条冲突的完整引用链，下载按钮禁用；
4. **下载**：确认后下载 `*_subset.db`。下载前服务端会重新解析一次，
   若状态已变化产生冲突则返回 409 拒绝。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATA_DIR` | `./data`（容器内 `/data`） | 上传文件与子库的工作目录 |
| `MAX_UPLOAD_MB` | `200` | 上传大小上限 |

## 已知限制

- 会话保存在单进程内存中，重启后需重新上传；上传文件保留在 `DATA_DIR`，
  需运维定期清理（未实现自动清理）；
- 未做多用户隔离与鉴权，仅供实验室内网使用；
- 表名/关键列名固定，如实验室库结构不同需先适配（见 `app/core.py` 顶部约定）。
