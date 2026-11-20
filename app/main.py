"""隔声试验数据交付复核 Web 应用（FastAPI）。

流程：上传 SQLite 库 → 勾选试验 / 剔除异常读数 → 预览引用解析结果
（含跨组拉入链与冲突）→ 确认后下载 SQLite 子库。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, field

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .analyze import (
    AnnotationTable,
    available_frequencies,
    collect_rows,
    fit_additive,
    fmt_db,
    fmt_freq,
    parse_annotations,
    processes_at,
    schema_problems,
)
from .core import READINGS, build_subset, load_model, resolve, table_columns, validate_schema

DATA_DIR = os.environ.get("DATA_DIR", "./data")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))

app = FastAPI(title="隔声试验数据交付复核")
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
)


@dataclass(frozen=True)
class AnnotationRevision:
    """一版标注表：整份替换时整体换引用，读取方拿到的必为同一版。"""

    version: int
    table: AnnotationTable
    source: str


@dataclass
class SessionState:
    workdir: str
    source_name: str
    selected_tests: set[int] = field(default_factory=set)
    excluded_readings: set[int] = field(default_factory=set)
    # 工艺差分析：标注表整份替换（单引用原子切换），版本号单调递增；
    # 每次分析请求只读取一次 annotations 引用，保证结论与参与读数来自同一版
    annotations: AnnotationRevision | None = None
    annotation_version: int = 0
    analysis_note: str | None = None  # 一次性提示（重定向后展示并清除）

    @property
    def db_path(self) -> str:
        return os.path.join(self.workdir, "source.db")


# 单进程内存会话；重启即失效（上传文件保留在 DATA_DIR 下，由运维清理）
SESSIONS: dict[str, SessionState] = {}


def _get_session(request: Request) -> SessionState | None:
    sid = request.cookies.get("sid")
    return SESSIONS.get(sid) if sid else None


def _load(st: SessionState):
    conn = sqlite3.connect(f"file:{st.db_path}?mode=ro", uri=True)
    try:
        return load_model(conn)
    finally:
        conn.close()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request, "upload.html", {"error": None}
    )


@app.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, file: UploadFile):
    error: str | None = None
    if not file.filename:
        error = "未选择文件"
    data = await file.read()
    if error is None and len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        error = f"文件超过大小限制（{MAX_UPLOAD_MB} MB）"

    if error is None:
        # 旧会话的工作目录一并清理
        old = _get_session(request)
        if old:
            SESSIONS.pop(request.cookies.get("sid"), None)
            shutil.rmtree(old.workdir, ignore_errors=True)

        sid = uuid.uuid4().hex
        workdir = os.path.join(DATA_DIR, sid)
        os.makedirs(workdir, exist_ok=True)
        path = os.path.join(workdir, "source.db")
        with open(path, "wb") as f:
            f.write(data)
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            errors = validate_schema(conn)
            conn.close()
        except sqlite3.DatabaseError:
            errors = ["文件不是有效的 SQLite 数据库"]
        if errors:
            shutil.rmtree(workdir, ignore_errors=True)
            return templates.TemplateResponse(
                request,
                "upload.html",
                {"error": "；".join(errors)},
                status_code=400,
            )
        SESSIONS[sid] = SessionState(workdir, file.filename or "source.db")
        resp = RedirectResponse("/select", status_code=303)
        resp.set_cookie("sid", sid, httponly=True)
        return resp

    return templates.TemplateResponse(
        request, "upload.html", {"error": error}, status_code=400
    )


# 读数表展示时的优先列序，其余列按出现顺序排后
_PREFERRED_READING_COLS = [
    "id", "kind", "frequency_hz", "level_db",
    "control_reading_id", "calibration_id", "note",
]


@app.get("/select", response_class=HTMLResponse)
def select_page(request: Request):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    model = _load(st)

    headers: list[str] = []
    for r in model.readings.values():
        for col in r:
            if col not in headers:
                headers.append(col)
    headers = [c for c in _PREFERRED_READING_COLS if c in headers] + [
        c for c in headers if c not in _PREFERRED_READING_COLS
    ]

    groups = []
    for gid in sorted(model.groups):
        g = model.groups[gid]
        tests = []
        for t in sorted(model.tests.values(), key=lambda x: x["id"]):
            if t["group_id"] != gid:
                continue
            readings = [
                {
                    "id": r["id"],
                    "cells": [r.get(c) for c in headers],
                    "excluded": r["id"] in st.excluded_readings,
                }
                for r in (
                    model.readings[rid]
                    for rid in model.readings_by_test.get(t["id"], [])
                )
            ]
            tests.append(
                {"row": t, "readings": readings, "selected": t["id"] in st.selected_tests}
            )
        groups.append({"row": g, "tests": tests})

    return templates.TemplateResponse(
        request,
        "select.html",
        {
            "groups": groups,
            "headers": headers,
            "source": st.source_name,
        },
    )


@app.post("/select")
async def select_save(request: Request):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    model = _load(st)

    selected = {int(v) for v in form.getlist("test")} & set(model.tests)
    # 剔除仅对所选试验内的读数生效，其余静默忽略
    excluded = {
        int(v)
        for v in form.getlist("excl")
        if (r := model.readings.get(int(v))) and r["test_id"] in selected
    }
    st.selected_tests = selected
    st.excluded_readings = excluded
    return RedirectResponse("/preview", status_code=303)


@app.get("/preview", response_class=HTMLResponse)
def preview(request: Request):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    model = _load(st)
    res = resolve(model, st.selected_tests, st.excluded_readings)
    return templates.TemplateResponse(
        request,
        "preview.html",
        {
            "res": res,
            "model": model,
            "selected": sorted(st.selected_tests),
            "excluded": sorted(st.excluded_readings),
            "pulled_in": sorted(res.pulled_in.items()),
            "source": st.source_name,
        },
    )


@app.post("/download")
def download(request: Request):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    model = _load(st)
    # 下载前重新解析，防止预览之后状态被修改导致不一致
    res = resolve(model, st.selected_tests, st.excluded_readings)
    if not res.ok:
        return HTMLResponse(
            "<h2>已阻止下载</h2><p>存在未解决的引用冲突（悬空或指向已剔除读数）。"
            "请返回 <a href='/preview'>预览</a> 查看引用链，"
            "或回 <a href='/select'>选择页</a> 修改。</p>",
            status_code=409,
        )
    out_path = os.path.join(st.workdir, f"subset_{int(time.time() * 1000)}.db")
    build_subset(st.db_path, out_path, res)
    name = os.path.splitext(st.source_name)[0] + "_subset.db"
    return FileResponse(
        out_path, filename=name, media_type="application/x-sqlite3"
    )


# ---------------------------------------------------------------- 工艺差分析


def _reading_columns(st: SessionState) -> set[str]:
    conn = sqlite3.connect(f"file:{st.db_path}?mode=ro", uri=True)
    try:
        return table_columns(conn, READINGS)
    finally:
        conn.close()


def _analysis_context(
    st: SessionState,
    *,
    note: str | None = None,
    errors: list[str] | None = None,
    freq_val: float | None = None,
) -> dict:
    """分析页上下文：库结构前提、标注表状态、可选频点与工艺。"""
    model = _load(st)
    problems = schema_problems(_reading_columns(st))
    rev = st.annotations  # 单引用读取：版本号与表内容必属同一版
    table = rev.table if rev else None
    freqs: list[tuple[float, int]] = []
    procs: list[str] = []
    freq_ok = False
    if table is not None and not problems:
        freqs = available_frequencies(model, table)
        if freq_val is not None and any(f == freq_val for f, _ in freqs):
            freq_ok = True
            procs = processes_at(model, table, freq_val)
    return {
        "source": st.source_name,
        "note": note,
        "errors": errors,
        "problems": problems,
        "table": table,
        "version": rev.version if rev else 0,
        "annotation_source": rev.source if rev else "",
        "freqs": freqs,
        "freq_val": freq_val if freq_ok else None,
        "procs": procs,
        "fmt_freq": fmt_freq,
    }


@app.get("/analysis", response_class=HTMLResponse)
def analysis_page(request: Request, freq: str | None = None):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    note = st.analysis_note
    st.analysis_note = None
    freq_val: float | None = None
    if freq is not None:
        try:
            freq_val = float(freq)
        except ValueError:
            note = note or f"频点参数无效：「{freq}」"
    ctx = _analysis_context(st, note=note, freq_val=freq_val)
    return templates.TemplateResponse(request, "analysis.html", ctx)


@app.post("/analysis/annotations", response_class=HTMLResponse)
async def analysis_annotations_upload(request: Request, file: UploadFile):
    """上传标注 CSV：校验通过则整份替换（版本号 +1），否则拒收并保留原表。"""
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    data = await file.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        ctx = _analysis_context(
            st, errors=[f"文件超过大小限制（{MAX_UPLOAD_MB} MB）"]
        )
        return templates.TemplateResponse(
            request, "analysis.html", ctx, status_code=400
        )
    model = _load(st)
    table, errors = parse_annotations(data, set(model.readings))
    if errors:
        ctx = _analysis_context(st, errors=errors)
        return templates.TemplateResponse(
            request, "analysis.html", ctx, status_code=400
        )
    st.annotation_version += 1
    st.annotations = AnnotationRevision(
        st.annotation_version, table, file.filename or "annotations.csv"
    )
    dup = f"，其中完全相同的重复行 {table.duplicates} 条已去重" if table.duplicates else ""
    st.analysis_note = (
        f"标注表已整份替换为版本 #{st.annotation_version}"
        f"（{table.size} 条，来源 {st.annotations.source}{dup}）"
    )
    return RedirectResponse("/analysis", status_code=303)


@app.get("/analysis/result", response_class=HTMLResponse)
def analysis_result(request: Request, freq: str = "", pa: str = "", pb: str = ""):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)

    def back(note: str, freq_val: float | None = None):
        st.analysis_note = note
        suffix = f"?freq={freq_val!r}" if freq_val is not None else ""
        return RedirectResponse(f"/analysis{suffix}", status_code=303)

    # 同一请求内只取一次标注表引用：结论与参与读数必然来自同一版标注
    rev = st.annotations
    if rev is None:
        return back("请先上传标注表")
    table = rev.table
    problems = schema_problems(_reading_columns(st))
    if problems:
        return back("库结构不满足分析条件：" + "；".join(problems))
    try:
        freq_val = float(freq)
    except ValueError:
        return back(f"频点参数无效：「{freq}」")
    model = _load(st)
    rows, skipped = collect_rows(model, table, freq_val)
    if not rows:
        return back(
            f"频点 {fmt_freq(freq_val)} Hz 下没有可参与拟合的已标注读数"
        )
    if pa == pb:
        return back("请选择两种不同的工艺", freq_val)
    present = {r.process for r in rows}
    missing = [p for p in (pa, pb) if p not in present]
    if missing:
        return back(
            f"工艺「{'」「'.join(missing)}」在频点 {fmt_freq(freq_val)} Hz "
            "没有已标注读数（标注表可能已更正，请重新选择）",
            freq_val,
        )
    res = fit_additive(rows, pa, pb)
    return templates.TemplateResponse(
        request,
        "analysis_result.html",
        {
            "res": res,
            "version": rev.version,
            "freq_val": freq_val,
            "skipped": skipped,
            "source": st.source_name,
            "fmt_db": fmt_db,
            "fmt_freq": fmt_freq,
        },
    )
