"""引用解析测试：跨组互引、链式拉入、剔除冲突、悬空引用、循环引用。"""
import sqlite3

from app.core import resolve


def test_cross_group_pull_in(model):
    """选 T1：R101/R102 跨组引用 G3/T9 的对照读数 R901。"""
    res = resolve(model, {1}, set())
    assert res.ok
    # 拉入 R901 及其所属试验 T9、组 G3、校准 C2
    assert 901 in res.reading_ids
    assert 9 in res.test_ids
    assert 3 in res.group_ids
    assert 2 in res.calibration_ids
    # 未沿引用到达的读数（R902/R903）不带入
    assert 902 not in res.reading_ids
    assert 903 not in res.reading_ids
    # 引用链完整：从所选读数到被拉入读数
    chain = res.pulled_in[901]
    assert any("101" in h.label or "102" in h.label for h in chain)
    assert "901" in chain[-1].label


def test_chained_cross_group_reference(model):
    """选 T3：R201 → R902 → R901，两级跨组链式拉入。"""
    res = resolve(model, {3}, set())
    assert res.ok
    assert {201, 202, 902, 901} <= res.reading_ids
    assert 903 not in res.reading_ids
    # R901 的链应经过 R902
    labels = [h.label for h in res.pulled_in[901]]
    assert any("902" in x for x in labels)


def test_exclusion_conflict_blocks(model):
    """剔除被引用的对照读数 → 冲突，链完整。"""
    res = resolve(model, {1, 9}, {901})
    assert not res.ok
    assert 901 not in res.reading_ids
    conflicts = [c for c in res.conflicts if "剔除" in c.reason]
    assert conflicts, "应报告指向已剔除读数的冲突"
    c = conflicts[0]
    assert "901" in c.chain[-1].label  # 链末端是被剔除的读数
    assert any("101" in h.label or "102" in h.label for h in c.chain)


def test_excluding_unreferenced_reading_is_fine(model):
    """剔除无人引用的读数不产生冲突。"""
    res = resolve(model, {1, 2}, {112})
    assert res.ok
    assert 112 not in res.reading_ids


def test_dangling_control_reference(model, sample_db):
    """对照引用悬空（目标不存在）→ 冲突并给出引用链。"""
    conn = sqlite3.connect(sample_db)
    conn.execute(
        "INSERT INTO readings VALUES (199, 2, NULL, 999, 'measurement', 200.0, 48.0, NULL)"
    )
    conn.commit()
    conn.close()
    from app.core import load_model

    conn = sqlite3.connect(sample_db)
    m = load_model(conn)
    conn.close()
    res = resolve(m, {2}, set())
    assert not res.ok
    c = res.conflicts[0]
    assert "悬空" in c.reason and "对照读数" in c.reason
    assert "199" in c.chain[0].label
    assert "999" in c.chain[-1].label


def test_dangling_calibration_reference(model, sample_db):
    """试验级校准引用悬空 → 冲突。"""
    conn = sqlite3.connect(sample_db)
    conn.execute("UPDATE tests SET calibration_id = 999 WHERE id = 2")
    conn.commit()
    conn.close()
    from app.core import load_model

    conn = sqlite3.connect(sample_db)
    m = load_model(conn)
    conn.close()
    res = resolve(m, {2}, set())
    assert not res.ok
    assert any("校准" in c.reason for c in res.conflicts)


def test_reference_cycle_terminates(model, sample_db):
    """R901 ↔ R902 循环引用：两者都拉入，解析正常终止。"""
    conn = sqlite3.connect(sample_db)
    conn.execute("UPDATE readings SET control_reading_id = 902 WHERE id = 901")
    conn.commit()
    conn.close()
    from app.core import load_model

    conn = sqlite3.connect(sample_db)
    m = load_model(conn)
    conn.close()
    res = resolve(m, {3}, set())
    assert res.ok
    assert {901, 902} <= res.reading_ids


def test_exclusion_of_pulled_in_target_via_selected_test(model):
    """R901 所在试验被勾选且 R901 被剔除时，跨组引用方同样报冲突。"""
    res = resolve(model, {1, 3, 9}, {901})
    assert not res.ok
    # R101、R201（经 R902）两条链都应报冲突
    assert len([c for c in res.conflicts if "剔除" in c.reason]) >= 1
