"""隔声试验数据交付核心逻辑：引用解析与 SQLite 子库生成。

数据库结构约定（表名、关键列名固定，其余列任意，详见 README.md）：

- groups(id, name, ...)
- calibrations(id, device, ...)
- tests(id, group_id -> groups.id,
        calibration_id -> calibrations.id,          -- 可空，试验级校准引用
        name, ...)
- readings(id, test_id -> tests.id,
           calibration_id -> calibrations.id,        -- 可空，读数级校准引用
           control_reading_id -> readings.id,        -- 可空，对照读数引用（允许跨组）
           kind, frequency_hz, level_db, ...)

交付规则：
1. 交付所选试验中未被剔除的全部读数；
2. 沿对照引用、校准引用补齐所需记录；对照读数允许跨组互引；
3. 跨到未勾选的组时，只带入沿引用找到的读数，以及这些读数所属的试验、
   试验所属的组（维持外键完整所必需）和引用到的校准记录；
4. 引用悬空（目标不存在）或指向已剔除读数时，记录完整引用链并阻止下载。
"""
from __future__ import annotations

import sqlite3
from collections import deque
from dataclasses import dataclass, field

GROUPS = "groups"
CALIBRATIONS = "calibrations"
TESTS = "tests"
READINGS = "readings"
# 导出子库时的建表/插数顺序（先被引用表，后被引用表）
EXPORT_TABLES = (GROUPS, CALIBRATIONS, TESTS, READINGS)

REQUIRED_COLUMNS = {
    GROUPS: ("id",),
    CALIBRATIONS: ("id",),
    TESTS: ("id", "group_id", "calibration_id"),
    READINGS: ("id", "test_id", "calibration_id", "control_reading_id"),
}


def validate_schema(conn: sqlite3.Connection) -> list[str]:
    """校验上传库是否满足结构约定，返回错误列表（空列表表示通过）。"""
    errors: list[str] = []
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    for table, cols in REQUIRED_COLUMNS.items():
        if table not in tables:
            errors.append(f"缺少数据表：{table}")
            continue
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if col not in have:
                errors.append(f"表 {table} 缺少列：{col}")
    return errors


@dataclass
class Model:
    """源库中四张表的全部行，按主键索引。"""

    groups: dict[int, dict]
    calibrations: dict[int, dict]
    tests: dict[int, dict]
    readings: dict[int, dict]
    readings_by_test: dict[int, list[int]]


def _rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    cur = conn.execute(f"SELECT * FROM {table}")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def load_model(conn: sqlite3.Connection) -> Model:
    groups = {r["id"]: r for r in _rows(conn, GROUPS)}
    calibrations = {r["id"]: r for r in _rows(conn, CALIBRATIONS)}
    tests = {r["id"]: r for r in _rows(conn, TESTS)}
    readings = {r["id"]: r for r in _rows(conn, READINGS)}
    readings_by_test: dict[int, list[int]] = {}
    for rid, r in readings.items():
        readings_by_test.setdefault(r["test_id"], []).append(rid)
    for ids in readings_by_test.values():
        ids.sort()
    return Model(groups, calibrations, tests, readings, readings_by_test)


@dataclass
class Hop:
    """引用链上的一环。kind ∈ reading / calibration / test。"""

    kind: str
    label: str


@dataclass
class Conflict:
    """一条无法交付的引用：chain 为从所选读数/试验出发的完整引用链。"""

    chain: list[Hop]
    reason: str


@dataclass
class Resolution:
    """一次交付解析的结果：应纳入子库的主键集合 + 拉入链 + 冲突列表。"""

    reading_ids: set[int]
    test_ids: set[int]
    group_ids: set[int]
    calibration_ids: set[int]
    pulled_in: dict[int, list[Hop]] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.conflicts


def _test_label(model: Model, tid: int) -> str:
    t = model.tests.get(tid)
    if t is None:
        return f"试验 #{tid}（不存在）"
    g = model.groups.get(t.get("group_id"), {})
    return f"试验 #{tid}「{t.get('name', '?')}」（组 #{g.get('id', '?')}「{g.get('name', '?')}」）"


def _reading_label(model: Model, rid: int) -> str:
    r = model.readings.get(rid)
    if r is None:
        return f"读数 #{rid}（不存在）"
    kind = r.get("kind") or "读数"
    return f"读数 #{rid}（{kind}｜{_test_label(model, r['test_id'])}）"


def _calibration_label(model: Model, cid: int) -> str:
    c = model.calibrations.get(cid)
    if c is None:
        return f"校准 #{cid}（不存在）"
    return f"校准 #{cid}（{c.get('device', '?')}）"


def resolve(
    model: Model,
    selected_test_ids: set[int] | list[int],
    excluded_reading_ids: set[int] | list[int],
) -> Resolution:
    """从所选试验的未剔除读数出发，沿引用做广度优先解析。

    - excluded_reading_ids 仅对所选试验内的读数有意义（Web 层已过滤）；
    - 引用目标在未勾选试验时：拉入该读数及其所属试验/组，并继续解析其引用；
    - 引用目标不存在或已被剔除：记录冲突（含完整引用链），不中断其余解析。
    """
    selected = {t for t in selected_test_ids if t in model.tests}
    excluded = set(excluded_reading_ids)

    res = Resolution(
        reading_ids=set(),
        test_ids=set(selected),
        group_ids=set(),
        calibration_ids=set(),
    )
    for tid in selected:
        res.group_ids.add(model.tests[tid]["group_id"])
        for rid in model.readings_by_test.get(tid, []):
            if rid not in excluded:
                res.reading_ids.add(rid)

    # 每个已纳入读数对应一条从所选试验出发的引用链
    chains: dict[int, list[Hop]] = {}
    queue: deque[int] = deque()
    for rid in sorted(res.reading_ids):
        chains[rid] = [Hop("reading", _reading_label(model, rid))]
        queue.append(rid)

    reported: set[tuple] = set()  # 冲突去重，避免同一悬空目标重复报告

    def add_conflict(chain: list[Hop], reason: str, key: tuple) -> None:
        if key in reported:
            return
        reported.add(key)
        res.conflicts.append(Conflict(list(chain), reason))

    def add_calibration(cid, chain: list[Hop]) -> None:
        if cid is None:
            return
        if cid in model.calibrations:
            res.calibration_ids.add(cid)
        else:
            add_conflict(
                chain + [Hop("calibration", _calibration_label(model, cid))],
                "悬空：引用的校准记录不存在",
                ("calibration", cid),
            )

    def add_test_calibration(tid: int, chain: list[Hop]) -> None:
        add_calibration(model.tests[tid].get("calibration_id"), chain)

    # 所选试验自身的试验级校准引用
    for tid in sorted(selected):
        add_test_calibration(tid, [Hop("test", _test_label(model, tid))])

    while queue:
        rid = queue.popleft()
        reading = model.readings[rid]
        chain = chains[rid]

        add_calibration(reading.get("calibration_id"), chain)

        cid = reading.get("control_reading_id")
        if cid is None:
            continue
        if cid not in model.readings:
            add_conflict(
                chain + [Hop("reading", _reading_label(model, cid))],
                "悬空：引用的对照读数不存在",
                ("reading", cid),
            )
            continue
        if cid in excluded:
            add_conflict(
                chain + [Hop("reading", _reading_label(model, cid))],
                "冲突：引用的对照读数已被剔除",
                ("excluded", cid),
            )
            continue
        if cid in res.reading_ids:
            continue  # 已纳入（含循环引用的回边），无需重复处理

        # 跨试验/跨组拉入：只带入该读数本身及其所属试验、组
        res.reading_ids.add(cid)
        new_chain = chain + [Hop("reading", _reading_label(model, cid))]
        chains[cid] = new_chain
        res.pulled_in[cid] = new_chain
        owner_test = model.readings[cid]["test_id"]
        if owner_test not in res.test_ids:
            res.test_ids.add(owner_test)
            res.group_ids.add(model.tests[owner_test]["group_id"])
            add_test_calibration(owner_test, new_chain)
        queue.append(cid)

    res.conflicts.sort(key=lambda c: (c.reason, c.chain[-1].label))
    return res


def build_subset(source_path: str, out_path: str, res: Resolution) -> dict[str, int]:
    """按解析结果生成 SQLite 子库。

    - 建表语句直接复用源库 sqlite_master 中的 DDL 原文，
      主键、外键约束随之保留；索引、触发器一并复制；
    - 行按原主键插入；插入后执行 PRAGMA foreign_key_check 校验，
      发现违规即抛异常（理论上 resolve 已保证不会发生）。
    返回各表导出行数。
    """
    src = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    try:
        ddl: dict[str, str] = {}
        extras: list[str] = []  # 索引、触发器
        for typ, _name, tbl, sql in src.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL"
        ):
            if typ == "table" and tbl in EXPORT_TABLES:
                ddl[tbl] = sql
            elif typ in ("index", "trigger") and tbl in EXPORT_TABLES:
                extras.append(sql)

        wanted = {
            GROUPS: res.group_ids,
            CALIBRATIONS: res.calibration_ids,
            TESTS: res.test_ids,
            READINGS: res.reading_ids,
        }

        out = sqlite3.connect(out_path)
        try:
            out.execute("PRAGMA foreign_keys = OFF")  # 插入阶段关闭，结束后统一校验
            for table in EXPORT_TABLES:
                if table in ddl:
                    out.execute(ddl[table])
            counts: dict[str, int] = {}
            for table in EXPORT_TABLES:
                ids = sorted(wanted[table])
                if not ids or table not in ddl:
                    counts[table] = 0
                    continue
                placeholders = ",".join("?" * len(ids))
                cur = src.execute(
                    f"SELECT * FROM {table} WHERE id IN ({placeholders})", ids
                )
                cols = [d[0] for d in cur.description]
                rows = cur.fetchall()
                out.executemany(
                    f"INSERT INTO {table} ({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})",
                    rows,
                )
                counts[table] = len(rows)
            for sql in extras:
                out.execute(sql)
            out.commit()
            violations = out.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"子库外键校验失败：{violations}")
        finally:
            out.close()
    finally:
        src.close()
    return counts
