"""工艺差可估计性分析：标注表解析 + 三因素加性模型最小二乘拟合。

模型：y = μ + α_工艺 + β_舱室 + γ_装夹 + ε（无交互），
对某频点全部已标注读数（含其他工艺的读数）做最小二乘拟合。

设计矩阵列不满秩时最优解不唯一，但拟合值唯一；
工艺差 α_A − α_B 可估计 ⟺ 它在所有最优解上取值相同
⟺ 对比向量与设计矩阵零空间正交。
不可估计时，取零空间中使对比非零的方向 v：β̂ 与 β̂+v 同为最优、
逐条拟合值相同，但工艺差不同。

全部矩阵计算用 Fraction 精确运算（设计矩阵为 0/1，XᵀX 的规模只随
因素水平数增长，不随读数条数增长），避免浮点秩判定误差。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型标注，避免循环依赖
    from .core import Model

# ---------------------------------------------------------------- 标注表

_HEADER_ALIASES = {
    "reading_id": {"reading_id", "读数编号", "读数id"},
    "process": {"process", "工艺"},
    "cabin": {"cabin", "舱室"},
    "fixture": {"fixture", "装夹方式", "装夹"},
}
_MAX_ERRORS = 20
_INT_RE = re.compile(r"-?\d+")


@dataclass(frozen=True)
class Annotation:
    """一条读数的试验条件标注。"""

    reading_id: int
    process: str
    cabin: str
    fixture: str


@dataclass
class AnnotationTable:
    """一份完整的标注表（整份替换，不在原表上增量修改）。"""

    rows: dict[int, Annotation]  # reading_id -> Annotation
    duplicates: int = 0  # 上传文件中完全相同、被去重的行数

    @property
    def size(self) -> int:
        return len(self.rows)


def _decode_csv(data: bytes) -> str | None:
    """UTF-8（兼容 BOM）优先，失败回退 GB18030；都失败返回 None。"""
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def parse_annotations(
    data: bytes, valid_reading_ids: set[int]
) -> tuple[AnnotationTable | None, list[str]]:
    """解析并校验标注 CSV。

    返回 (表, []) 或 (None, 错误列表)。任何错误都导致整份拒收：
    - 缺少必需列（reading_id / process / cabin / fixture，支持中文别名）；
    - 读数编号非整数、为空，或在库中不存在；
    - 同一编号出现不一致的标注（完全相同的重复行去重后接受）；
    - 工艺 / 舱室 / 装夹方式为空。
    """
    text = _decode_csv(data)
    if text is None:
        return None, ["无法识别文件编码（请使用 UTF-8 或 GB18030 编码的 CSV）"]
    raw = list(csv.reader(io.StringIO(text)))
    header_lineno = next(
        (i for i, row in enumerate(raw) if any(c.strip() for c in row)), None
    )
    if header_lineno is None:
        return None, ["CSV 为空"]

    header = [h.strip().lower() for h in raw[header_lineno]]
    col_idx: dict[str, int] = {}
    for canon, aliases in _HEADER_ALIASES.items():
        for j, h in enumerate(header):
            if h in {a.lower() for a in aliases}:
                col_idx[canon] = j
                break
    missing = [c for c in _HEADER_ALIASES if c not in col_idx]
    if missing:
        return None, [
            "CSV 缺少必需列：" + "、".join(missing)
            + "（表头需含 reading_id/process/cabin/fixture，或中文 读数编号/工艺/舱室/装夹方式）"
        ]
    need = max(col_idx.values())

    errors: list[str] = []
    n_errors = 0

    def err(msg: str) -> None:
        nonlocal n_errors
        n_errors += 1
        if len(errors) < _MAX_ERRORS:
            errors.append(msg)

    table: dict[int, Annotation] = {}
    first_line: dict[int, int] = {}
    duplicates = 0
    for lineno in range(header_lineno + 1, len(raw)):
        row = raw[lineno]
        if not any(c.strip() for c in row):
            continue
        line = lineno + 1  # 1 起始的行号
        if len(row) <= need:
            err(f"第 {line} 行列数不足（至少需要 {need + 1} 列）")
            continue
        rid_s = row[col_idx["reading_id"]].strip()
        if not rid_s:
            err(f"第 {line} 行读数编号为空")
            continue
        if not _INT_RE.fullmatch(rid_s):
            err(f"第 {line} 行读数编号不是整数：「{rid_s}」")
            continue
        rid = int(rid_s)
        proc = row[col_idx["process"]].strip()
        cabin = row[col_idx["cabin"]].strip()
        fixture = row[col_idx["fixture"]].strip()
        if not proc or not cabin or not fixture:
            err(f"第 {line} 行（读数 #{rid}）的工艺/舱室/装夹方式存在空值")
            continue
        if rid not in valid_reading_ids:
            err(f"第 {line} 行：读数 #{rid} 在库中不存在")
            continue
        ann = Annotation(rid, proc, cabin, fixture)
        if rid in table:
            old = table[rid]
            if (old.process, old.cabin, old.fixture) != (proc, cabin, fixture):
                err(
                    f"读数 #{rid} 标注冲突：第 {first_line[rid]} 行"
                    f"（{old.process}/{old.cabin}/{old.fixture}）与"
                    f"第 {line} 行（{proc}/{cabin}/{fixture}）"
                )
            else:
                duplicates += 1
            continue
        table[rid] = ann
        first_line[rid] = line

    if n_errors > _MAX_ERRORS:
        errors.append(f"……共 {n_errors} 处问题，仅列出前 {_MAX_ERRORS} 处")
    if errors:
        return None, errors
    return AnnotationTable(table, duplicates), []


# ---------------------------------------------------------------- 频点与取数

# 分析必需的 readings 数值列
REQUIRED_ANALYSIS_COLUMNS = ("frequency_hz", "level_db")


def schema_problems(reading_columns: set[str]) -> list[str]:
    """旧库缺少频点列或数值列时，给出分析页要展示的原因。"""
    problems = []
    if "frequency_hz" not in reading_columns:
        problems.append("readings 表缺少 frequency_hz 列，无法按频点取数")
    if "level_db" not in reading_columns:
        problems.append("readings 表缺少 level_db 列，无法取得隔声量（dB）数值")
    return problems


def _as_float(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return None


def available_frequencies(
    model: "Model", table: AnnotationTable
) -> list[tuple[float, int]]:
    """[(频点, 该频点可参与拟合的已标注读数条数)]，按频点升序。"""
    counts: dict[float, int] = {}
    for rid in table.rows:
        r = model.readings.get(rid)
        if r is None:
            continue
        f = _as_float(r.get("frequency_hz"))
        if f is None or _as_float(r.get("level_db")) is None:
            continue
        counts[f] = counts.get(f, 0) + 1
    return sorted(counts.items())


def processes_at(model: "Model", table: AnnotationTable, freq: float) -> list[str]:
    """某频点下有已标注读数（且数值可用）的工艺列表。"""
    procs = set()
    for rid, ann in table.rows.items():
        r = model.readings.get(rid)
        if r is None:
            continue
        if _as_float(r.get("frequency_hz")) != freq:
            continue
        if _as_float(r.get("level_db")) is None:
            continue
        procs.add(ann.process)
    return sorted(procs)


@dataclass
class FitRow:
    reading_id: int
    process: str
    cabin: str
    fixture: str
    y: Fraction  # 实测隔声量（dB，按十进制原文精确化）


def collect_rows(
    model: "Model", table: AnnotationTable, freq: float
) -> tuple[list[FitRow], list[int]]:
    """取某频点全部已标注读数。

    返回 (可参与拟合的行, 因 level_db 为空/非数值被跳过的读数编号)。
    """
    rows: list[FitRow] = []
    skipped: list[int] = []
    for rid in sorted(table.rows):
        r = model.readings.get(rid)
        if r is None:
            continue
        if _as_float(r.get("frequency_hz")) != freq:
            continue
        y = _as_float(r.get("level_db"))
        if y is None:
            skipped.append(rid)
            continue
        ann = table.rows[rid]
        rows.append(
            FitRow(rid, ann.process, ann.cabin, ann.fixture, Fraction(str(y)))
        )
    return rows, skipped


# ---------------------------------------------------------------- 精确最小二乘


def _solve_and_nullspace(
    A: list[list[Fraction]], b: list[Fraction]
) -> tuple[list[Fraction], list[list[Fraction]]]:
    """解相容方程组 A x = b，返回 (一个解, A 的零空间基)。

    自由变量取 0 得到一个特解；零空间基由 RREF 读出。
    对 A = XᵀX 使用时，零空间即设计矩阵 X 的零空间
    （XᵀXv = 0 ⟺ Xv = 0）。
    """
    p = len(A)
    M = [list(A[i]) + [b[i]] for i in range(p)]
    pivots: list[int] = []
    r = 0
    for c in range(p):
        piv = next((i for i in range(r, p) if M[i][c] != 0), None)
        if piv is None:
            continue
        M[r], M[piv] = M[piv], M[r]
        inv = Fraction(1) / M[r][c]
        M[r] = [x * inv for x in M[r]]
        for i in range(p):
            if i != r and M[i][c] != 0:
                f = M[i][c]
                M[i] = [a - f * bb for a, bb in zip(M[i], M[r])]
        pivots.append(c)
        r += 1
    for i in range(r, p):
        if any(x != 0 for x in M[i]):
            raise ArithmeticError("正规方程组不相容（不应发生）")
    sol = [Fraction(0)] * p
    for i, c in enumerate(pivots):
        sol[c] = M[i][p]
    pivot_set = set(pivots)
    basis = []
    for f in range(p):
        if f in pivot_set:
            continue
        v = [Fraction(0)] * p
        v[f] = Fraction(1)
        for i, c in enumerate(pivots):
            v[c] = -M[i][f]
        basis.append(v)
    return sol, basis


@dataclass
class FitResult:
    """一次加性模型拟合的结果。

    estimable=True：所有最优解给出相同工艺差 delta。
    estimable=False：beta 与 alt_beta 同为最优、逐条拟合值相同，
    但工艺差分别为 delta 与 alt_delta；direction 为造成混杂的调整方向。
    """

    process_a: str
    process_b: str
    coef_labels: list[str]
    rows: list[FitRow]
    beta: list[Fraction]
    fitted: list[Fraction]
    residuals: list[Fraction]
    rss: Fraction
    estimable: bool
    delta: Fraction
    alt_beta: list[Fraction] | None = None
    alt_fitted: list[Fraction] | None = None
    alt_delta: Fraction | None = None
    direction: list[tuple[str, Fraction]] = field(default_factory=list)

    @property
    def coef_rows(self) -> list[tuple[str, Fraction]]:
        return list(zip(self.coef_labels, self.beta))

    @property
    def coef_rows2(self) -> list[tuple[str, Fraction, Fraction]]:
        return list(zip(self.coef_labels, self.beta, self.alt_beta or []))

    def view_rows(self) -> list[dict]:
        """逐条拟合（不可估计时附第二组解的拟合值，与第一组完全相同）。"""
        out = []
        for i, row in enumerate(self.rows):
            out.append(
                {
                    "reading_id": row.reading_id,
                    "process": row.process,
                    "cabin": row.cabin,
                    "fixture": row.fixture,
                    "y": row.y,
                    "fitted": self.fitted[i],
                    "alt_fitted": (
                        self.alt_fitted[i] if self.alt_fitted is not None else None
                    ),
                    "resid": self.residuals[i],
                }
            )
        return out


def fit_additive(rows: list[FitRow], process_a: str, process_b: str) -> FitResult:
    """三因素加性模型最小二乘拟合，并判定工艺差 α_A − α_B 是否可估计。"""
    levels_p = sorted({r.process for r in rows})
    levels_c = sorted({r.cabin for r in rows})
    levels_f = sorted({r.fixture for r in rows})
    labels = (
        ["截距 μ"]
        + [f"工艺「{v}」" for v in levels_p]
        + [f"舱室「{v}」" for v in levels_c]
        + [f"装夹「{v}」" for v in levels_f]
    )
    idx_p = {v: 1 + i for i, v in enumerate(levels_p)}
    idx_c = {v: 1 + len(levels_p) + i for i, v in enumerate(levels_c)}
    idx_f = {
        v: 1 + len(levels_p) + len(levels_c) + i for i, v in enumerate(levels_f)
    }
    p = len(labels)

    # 设计矩阵行 + 正规方程组 XᵀX β = Xᵀy（精确分数）
    xs: list[list[Fraction]] = []
    A = [[Fraction(0)] * p for _ in range(p)]
    b = [Fraction(0)] * p
    for row in rows:
        x = [Fraction(0)] * p
        x[0] = Fraction(1)
        x[idx_p[row.process]] = Fraction(1)
        x[idx_c[row.cabin]] = Fraction(1)
        x[idx_f[row.fixture]] = Fraction(1)
        xs.append(x)
        for i in range(p):
            if x[i]:
                b[i] += row.y
                Ai = A[i]
                for j in range(p):
                    if x[j]:
                        Ai[j] += 1
    beta, null_basis = _solve_and_nullspace(A, b)

    # 对比向量：工艺A − 工艺B
    cvec = [Fraction(0)] * p
    cvec[idx_p[process_a]] = Fraction(1)
    cvec[idx_p[process_b]] = Fraction(-1)

    def contrast(coefs: list[Fraction]) -> Fraction:
        return sum(ci * bi for ci, bi in zip(cvec, coefs))

    delta = contrast(beta)
    fitted = [
        sum((xi * bi for xi, bi in zip(x, beta)), Fraction(0)) for x in xs
    ]
    residuals = [row.y - f for row, f in zip(rows, fitted)]
    rss = sum((r * r for r in residuals), Fraction(0))

    # 可估计 ⟺ 对比向量与零空间正交
    offending = next(
        (
            v
            for v in null_basis
            if sum(ci * vi for ci, vi in zip(cvec, v)) != 0
        ),
        None,
    )
    if offending is None:
        return FitResult(
            process_a=process_a,
            process_b=process_b,
            coef_labels=labels,
            rows=rows,
            beta=beta,
            fitted=fitted,
            residuals=residuals,
            rss=rss,
            estimable=True,
            delta=delta,
        )

    # 缩放零空间方向，使两组解的工艺差恰好相差 1 dB，便于对照展示
    scale = Fraction(1) / sum(ci * vi for ci, vi in zip(cvec, offending))
    w = [scale * vi for vi in offending]
    # 消去截距分量：u = (μ+1, 各工艺水平 −1) 恒在零空间内且对比为 0，
    # 用它把调整方向化为纯因素水平间的此消彼长，更便于解读混杂来源
    u = [Fraction(0)] * p
    u[0] = Fraction(1)
    for j in idx_p.values():
        u[j] = Fraction(-1)
    w = [wi - w[0] * ui for wi, ui in zip(w, u)]
    alt_beta = [bi + wi for bi, wi in zip(beta, w)]
    alt_fitted = [
        sum((xi * bi for xi, bi in zip(x, alt_beta)), Fraction(0)) for x in xs
    ]
    direction = [(labels[i], w[i]) for i in range(p) if w[i] != 0]
    return FitResult(
        process_a=process_a,
        process_b=process_b,
        coef_labels=labels,
        rows=rows,
        beta=beta,
        fitted=fitted,
        residuals=residuals,
        rss=rss,
        estimable=False,
        delta=delta,
        alt_beta=alt_beta,
        alt_fitted=alt_fitted,
        alt_delta=contrast(alt_beta),
        direction=direction,
    )


# ---------------------------------------------------------------- 展示格式化


def fmt_db(x: Fraction) -> str:
    """分数格式化为 dB 文本：整数原样，其余保留至多 4 位小数。"""
    if x.denominator == 1:
        return str(x.numerator)
    s = f"{float(x):.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def fmt_freq(f: float) -> str:
    return f"{f:g}"
