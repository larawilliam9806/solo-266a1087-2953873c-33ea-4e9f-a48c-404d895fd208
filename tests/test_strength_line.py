"""Tests for the strength-line trial and its summary table."""

import io

from warehouse import core
from warehouse.db import init_db


def upload(client, template_id, text, filename="batch.csv"):
    return client.post(
        "/upload",
        data={
            "template_id": str(template_id),
            "file": (io.BytesIO(text.encode("utf-8")), filename),
        },
        content_type="multipart/form-data",
    )


def summary_row(db, number):
    return db.execute(
        """
        SELECT ps.*, s.number FROM strength_pair_summary ps
        JOIN specimens s ON s.id = ps.specimen_id
        WHERE s.number = ?
        """,
        (number,),
    ).fetchone()


def summary_snapshot(db):
    return [
        tuple(r)
        for r in db.execute(
            "SELECT specimen_id, measured_micro, measured_revision,"
            " converted_micro, converted_revision FROM strength_pair_summary"
            " ORDER BY specimen_id"
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# summary maintenance
# ---------------------------------------------------------------------------


def test_summary_follows_uploads(client, meta, db):
    upload(client, meta["measured_tpl"], "试件编号,修订号,强度,劈裂\nC-1,1,30.0,2.5\n")
    row = summary_row(db, "C-1")
    assert row["measured_micro"] == 30000
    assert row["measured_revision"] == 1
    assert row["converted_micro"] is None

    upload(client, meta["converted_tpl"], "试件编号,修订号,强度\nC-1,1,28.5\n")
    row = summary_row(db, "C-1")
    assert row["measured_micro"] == 30000
    assert row["converted_micro"] == 28500
    assert row["converted_revision"] == 1

    # A patch raises the measured revision; the converted side is untouched.
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-1,2,31.0\n")
    row = summary_row(db, "C-1")
    assert row["measured_micro"] == 31000
    assert row["measured_revision"] == 2
    assert row["converted_micro"] == 28500
    assert row["converted_revision"] == 1


def test_summary_only_rewrites_affected_specimens(client, meta, db):
    upload(
        client,
        meta["measured_tpl"],
        "试件编号,修订号,强度,劈裂\nC-1,1,30.0,2.5\nC-2,1,35.0,2.9\n",
    )
    upload(client, meta["converted_tpl"], "试件编号,修订号,强度\nC-1,1,28.5\nC-2,1,33.5\n")
    before = {
        row["number"]: row["updated_at"]
        for row in db.execute(
            "SELECT s.number, ps.updated_at FROM strength_pair_summary ps"
            " JOIN specimens s ON s.id = ps.specimen_id"
        ).fetchall()
    }

    # A split-tensile patch touches no strength indicator: summary unchanged.
    upload(client, meta["measured_tpl"], "试件编号,修订号,劈裂\nC-1,2,2.6\n")
    after = {
        row["number"]: row["updated_at"]
        for row in db.execute(
            "SELECT s.number, ps.updated_at FROM strength_pair_summary ps"
            " JOIN specimens s ON s.id = ps.specimen_id"
        ).fetchall()
    }
    assert after == before

    # A strength patch for C-2 rewrites only C-2's summary row.
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-2,2,36.0\n")
    row_c1 = summary_row(db, "C-1")
    row_c2 = summary_row(db, "C-2")
    assert row_c1["updated_at"] == before["C-1"]
    assert row_c2["measured_micro"] == 36000
    assert row_c2["measured_revision"] == 2


def test_summary_follows_template_switch(client, meta, db):
    upload(client, meta["converted_tpl"], "试件编号,修订号,强度\nC-3,1,28.7\n")
    row = summary_row(db, "C-3")
    assert row["converted_micro"] == 28700
    assert row["measured_micro"] is None

    assert client.post(
        "/batches/1/switch", data={"template_id": str(meta["measured_tpl"])}
    ).status_code == 302
    row = summary_row(db, "C-3")
    assert row["measured_micro"] == 28700
    assert row["converted_micro"] is None

    assert client.post(
        "/batches/1/switch", data={"template_id": str(meta["converted_tpl"])}
    ).status_code == 302
    row = summary_row(db, "C-3")
    assert row["measured_micro"] is None
    assert row["converted_micro"] == 28700


def test_summary_row_removed_when_strength_withdrawn(client, meta, db):
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-6,1,30.0\n")
    assert summary_row(db, "C-6") is not None

    cur = db.execute(
        "INSERT INTO templates (name, specimen_header, revision_header, created_at)"
        " VALUES ('无匹配模板', '试件编号', '修订号', ?)",
        (core.now_iso(),),
    )
    other = cur.lastrowid
    db.execute(
        "INSERT INTO template_columns (template_id, header, indicator_id)"
        " VALUES (?, '别的列', ?)",
        (other, meta["split"]),
    )
    db.commit()

    assert client.post(
        "/batches/1/switch", data={"template_id": str(other)}
    ).status_code == 302
    assert summary_row(db, "C-6") is None


def test_rejected_upload_leaves_summary_untouched(client, meta, db):
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-1,1,30.0\n", "a.csv")
    upload(client, meta["converted_tpl"], "试件编号,修订号,强度\nC-1,1,28.0\n", "b.csv")
    before = summary_snapshot(db)

    resp = upload(
        client, meta["patch_tpl"], "试件编号,修订号,强度\nC-1,1,31.0\n", "c.csv"
    )
    assert resp.status_code == 409
    assert summary_snapshot(db) == before


def test_rejected_switch_leaves_summary_untouched(client, meta, db):
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-5,1,30.0\n", "a.csv")
    upload(client, meta["converted_tpl"], "试件编号,修订号,强度\nC-5,1,26.0\n", "b.csv")
    before = summary_snapshot(db)

    resp = client.post("/batches/2/switch", data={"template_id": str(meta["patch_tpl"])})
    assert resp.status_code == 409
    assert summary_snapshot(db) == before


def test_legacy_database_backfilled_on_first_enable(client, meta, db):
    """A database that predates the summary table is folded in by init_db."""
    upload(client, meta["measured_tpl"], "试件编号,修订号,强度,劈裂\nC-1,1,30.0,2.5\n")
    upload(client, meta["converted_tpl"], "试件编号,修订号,强度\nC-1,1,28.5\n")
    upload(client, meta["patch_tpl"], "试件编号,修订号,强度\nC-2,3,33.0\n")

    # Simulate the pre-feature database: no summary table at all.
    db.execute("DROP TABLE strength_pair_summary")
    db.commit()

    init_db(db)

    row = summary_row(db, "C-1")
    assert row["measured_micro"] == 30000
    assert row["measured_revision"] == 1
    assert row["converted_micro"] == 28500
    row = summary_row(db, "C-2")
    assert row["measured_micro"] == 33000
    assert row["measured_revision"] == 3
    assert row["converted_micro"] is None


def test_startup_backfills_legacy_database(app, client, meta, db):
    """init_app runs the migration over its own raw connection."""
    from warehouse import create_app
    from warehouse.db import get_db

    upload(client, meta["measured_tpl"], "试件编号,修订号,强度,劈裂\nC-1,1,30.0,2.5\n")
    upload(client, meta["converted_tpl"], "试件编号,修订号,强度\nC-1,1,28.5\n")
    db.execute("DROP TABLE strength_pair_summary")
    db.commit()

    legacy_app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "DATABASE": app.config["DATABASE"],
            "CSRF_ENABLED": False,
        }
    )
    with legacy_app.app_context():
        row = get_db().execute(
            "SELECT measured_micro, measured_revision, converted_micro,"
            " converted_revision FROM strength_pair_summary"
        ).fetchone()
    assert tuple(row) == (30000, 1, 28500, 1)


# ---------------------------------------------------------------------------
# interval computation (pure)
# ---------------------------------------------------------------------------


def test_acceptable_intervals_sweeps_change_points():
    # Spans are half-open from below: (low, high] re-judges the specimen.
    spans = [(2, 5), (4, 8)]
    # counts: L=1:0, 2-3:1, 4-5:2, 6-8:1, 9-10:0
    # Adjacent acceptable segments merge into one maximal interval.
    assert core.acceptable_intervals(spans, 1, 10, 1) == [
        {"lower_micro": 1, "upper_micro": 3, "max_changes": 1},
        {"lower_micro": 6, "upper_micro": 10, "max_changes": 1},
    ]
    assert core.acceptable_intervals(spans, 1, 10, 2) == [
        {"lower_micro": 1, "upper_micro": 10, "max_changes": 2},
    ]
    assert core.acceptable_intervals(spans, 1, 10, 0) == [
        {"lower_micro": 1, "upper_micro": 1, "max_changes": 0},
        {"lower_micro": 9, "upper_micro": 10, "max_changes": 0},
    ]
    assert core.acceptable_intervals(spans, 3, 6, 1) == [
        {"lower_micro": 3, "upper_micro": 3, "max_changes": 1},
        {"lower_micro": 6, "upper_micro": 6, "max_changes": 1},
    ]
    assert core.acceptable_intervals(spans, 4, 5, 1) == []
    # A single-point range still works.
    assert core.acceptable_intervals(spans, 4, 4, 2) == [
        {"lower_micro": 4, "upper_micro": 4, "max_changes": 2},
    ]


# ---------------------------------------------------------------------------
# trial page
# ---------------------------------------------------------------------------


def _seed_trial_data(client, meta):
    # C-1: measured 30.0 / converted 35.0, both rev 1 -> re-judged on (30, 35].
    upload(client, meta["measured_tpl"], "试件编号,修订号,强度,劈裂\nC-1,1,30.0,2.5\n")
    upload(client, meta["converted_tpl"], "试件编号,修订号,强度\nC-1,1,35.0\n")
    # C-2: both 32.0 rev 1 -> equal, never re-judged.
    upload(client, meta["measured_tpl"], "试件编号,修订号,强度,劈裂\nC-2,1,32.0,2.6\n")
    upload(client, meta["converted_tpl"], "试件编号,修订号,强度\nC-2,1,32.0\n")
    # C-3: revisions differ (measured rev 2, converted rev 1) -> listed aside.
    upload(client, meta["measured_tpl"], "试件编号,修订号,强度,劈裂\nC-3,2,40.0,2.7\n")
    upload(client, meta["converted_tpl"], "试件编号,修订号,强度\nC-3,1,38.0\n")
    # C-4: converted only -> listed aside.
    upload(client, meta["converted_tpl"], "试件编号,修订号,强度\nC-4,1,28.0\n")


def test_trial_page_counts_intervals_and_excluded(client, meta, db):
    _seed_trial_data(client, meta)

    resp = client.get("/strength-line?lower=30&upper=36&max_changes=0")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # Two comparable specimens (C-1, C-2); C-3 and C-4 listed aside.
    assert "可比试件数：<strong>2</strong>" in html
    assert "C-3" in html and "修订号不同" in html
    assert "C-4" in html and "缺实测值" in html

    # Re-judgment only on (30, 35]: cap 0 keeps [30, 30] and [35.001, 36].
    assert "30 – 30" in html
    assert "35.001 – 36" in html
    assert "30.001" not in html

    # Cap 1 admits the whole range as one maximal interval.
    html = client.get(
        "/strength-line?lower=30&upper=36&max_changes=1"
    ).get_data(as_text=True)
    assert "30 – 36" in html
    assert "35.001" not in html


def test_trial_page_without_params_shows_overview_only(client, meta, db):
    _seed_trial_data(client, meta)
    resp = client.get("/strength-line")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "可比试件数：<strong>2</strong>" in html
    assert "试算结果" not in html


def test_trial_page_validates_input(client, meta, db):
    _seed_trial_data(client, meta)

    html = client.get(
        "/strength-line?lower=36&upper=30&max_changes=0"
    ).get_data(as_text=True)
    assert "线值下限不能大于上限" in html

    html = client.get("/strength-line?lower=30&upper=36").get_data(as_text=True)
    assert "请填写可接受的改判数上限" in html

    html = client.get(
        "/strength-line?lower=30&upper=36&max_changes=-1"
    ).get_data(as_text=True)
    assert "改判数上限应为不小于 0 的整数" in html

    html = client.get(
        "/strength-line?lower=30.0005&upper=36&max_changes=0"
    ).get_data(as_text=True)
    assert "最多三位小数" in html

    html = client.get(
        "/strength-line?lower=abc&upper=36&max_changes=0"
    ).get_data(as_text=True)
    assert "不是有效的强度值" in html


def test_trial_page_empty_when_indicators_missing(client, db):
    resp = client.get("/strength-line?lower=1&upper=2&max_changes=0")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "STRENGTH_MEASURED" in html
    assert "可比试件数：<strong>0</strong>" in html
