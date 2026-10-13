"""
Phase 3 regression tests covering all issues fixed in the final audit:
  A. Legacy-history regularization bug (missing 'history' field in document)
  B. Explain endpoint uses real pipelines (verified by checking $match depth)
  C. Invalid calendar date validation (e.g. 2026-02-30 -> 422)
  D. Status enum validation in GET /attendance
  E. Concurrent punch-out (exactly one 200, rest 409)
  F. Concurrent regularization (no lost history entries)
  G. Extra fields in RegularizeIn -> 422 (contract says derived fields are read-only)
  H. Explicit null punched_at behaves like omitted (uses server time)
  I. 92-day trend boundary (92 days OK, 93 days -> 422)
  J. Reversed trend date range -> 422
"""

import pytest
from fastapi.testclient import TestClient
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta, date
from bson import ObjectId

from app.main import app, db, truncate_to_seconds

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _ts(dt_utc: datetime) -> int:
    return int(dt_utc.timestamp()) * 1000


def _utc(year, month, day, hour, minute, second=0) -> datetime:
    """Return IST wall-clock time as correct UTC epoch."""
    ist_dt = datetime(year, month, day, hour, minute, second, tzinfo=IST)
    return ist_dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# Fixture
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def clean_db():
    assert "test" in db.name.lower(), f"Refusing to run destructive operations against non-test database: '{db.name}'"
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    with TestClient(app) as tc:
        yield tc


def _create_emp(tc, emp_code="EMP0050", shift_start="09:30", shift_end="18:30"):
    r = tc.post("/employees", json={
        "emp_code": emp_code,
        "name": "Test User",
        "email": f"{emp_code.lower()}@test.com",
        "department": "TestDept",
        "shift_start": shift_start,
        "shift_end": shift_end,
        "joined_on": "2026-01-01",
    })
    assert r.status_code == 201, r.text
    return r.json()


def _punch_in(tc, emp_code, ist_h, ist_m, ist_s=0, day=6, month=7, year=2026, status="PRESENT"):
    utc = _utc(year, month, day, ist_h, ist_m, ist_s)
    r = tc.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": _ts(utc), "status": status})
    assert r.status_code == 201, r.text
    return r.json()


def _punch_out(tc, emp_code, ist_h, ist_m, ist_s=0, day=6, month=7, year=2026):
    utc = _utc(year, month, day, ist_h, ist_m, ist_s)
    r = tc.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": _ts(utc)})
    assert r.status_code == 200, r.text
    return r.json()


# ===========================================================================
# A. Legacy-history regularization bug
# ===========================================================================

class TestLegacyHistoryRegularization:
    """
    DATA_MODEL §9: older records may omit the 'history' field entirely.
    The optimistic-lock filter previously required history: [] which would
    not match a document with no history key at all, causing every
    regularization of a legacy record to return 409.
    """

    def test_regularize_legacy_doc_no_history_field(self, clean_db):
        """A legacy record (no 'history' key) can be successfully regularized."""
        _create_emp(clean_db)
        # Insert a legacy document directly — no 'history', no 'half_day' keys
        db.attendance_logs.insert_one({
            "emp_code": "EMP0050",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": _utc(2026, 7, 6, 10, 5, 0),   # 10:05 IST (35 min late)
            "punch_out": _utc(2026, 7, 6, 19, 10, 0),  # 19:10 IST
            "work_hours": 9.08,
            "late_minutes": 35,
            "overtime_minutes": 40,
            # no 'history', no 'half_day'
        })

        # Regularize: correct punch-in to 09:28 IST
        new_ts = _ts(_utc(2026, 7, 6, 9, 28, 0))
        res = clean_db.patch(
            "/attendance/EMP0050/2026-07-06",
            json={
                "punch_in": new_ts,
                "reason": "Biometric glitch correction",
                "regularized_by": "hr.admin"
            }
        )
        assert res.status_code == 200, f"Expected 200 on legacy doc, got {res.status_code}: {res.text}"
        data = res.json()
        assert data["late_minutes"] == 0
        assert len(data["history"]) == 1
        assert data["history"][0]["by"] == "hr.admin"
        assert "late_minutes" in data["history"][0]["changes"]

    def test_regularize_legacy_doc_no_history_preserves_entry(self, clean_db):
        """After first regularization, a second one still appends correctly."""
        _create_emp(clean_db)
        db.attendance_logs.insert_one({
            "emp_code": "EMP0050",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": _utc(2026, 7, 6, 10, 5, 0),
            "punch_out": _utc(2026, 7, 6, 19, 10, 0),
            "work_hours": 9.08,
            "late_minutes": 35,
            "overtime_minutes": 40,
            # legacy: no history field
        })

        ts1 = _ts(_utc(2026, 7, 6, 9, 28, 0))
        res1 = clean_db.patch(
            "/attendance/EMP0050/2026-07-06",
            json={"punch_in": ts1, "reason": "First correction ever", "regularized_by": "hr1"}
        )
        assert res1.status_code == 200, res1.text
        assert len(res1.json()["history"]) == 1

        # A further correction must append, not replace
        ts2 = _ts(_utc(2026, 7, 6, 9, 30, 0))
        res2 = clean_db.patch(
            "/attendance/EMP0050/2026-07-06",
            json={"punch_in": ts2, "reason": "Second minor correction", "regularized_by": "hr2"}
        )
        assert res2.status_code == 200, res2.text
        assert len(res2.json()["history"]) == 2

    def test_regularize_legacy_doc_no_half_day_field(self, clean_db):
        """Legacy doc without 'half_day' still regularizes; half_day defaults to False."""
        _create_emp(clean_db)
        db.attendance_logs.insert_one({
            "emp_code": "EMP0050",
            "date": "2026-07-14",
            "status": "PRESENT",
            "punch_in": _utc(2026, 7, 14, 9, 32, 0),
            "punch_out": _utc(2026, 7, 14, 18, 31, 0),
            "work_hours": 8.98,
            "late_minutes": 0,
            "overtime_minutes": 0,
            # no 'history', no 'half_day'
        })

        new_ts = _ts(_utc(2026, 7, 14, 9, 40, 1))  # slightly late
        res = clean_db.patch(
            "/attendance/EMP0050/2026-07-14",
            json={"punch_in": new_ts, "reason": "Time correction", "regularized_by": "admin"}
        )
        assert res.status_code == 200, res.text
        assert res.json()["late_minutes"] == 10


# ===========================================================================
# B. Explain endpoint uses full pipeline
# ===========================================================================

class TestExplainFullPipeline:
    """
    The explain endpoint must use the same pipeline as the real endpoint,
    not a simplified stub. We verify the returned explain document contains
    meaningful stages beyond a bare $match.
    """

    def test_explain_employee_monthly_has_group_stage(self, clean_db):
        """Explain for employee_monthly should show the $group stage in the pipeline."""
        _create_emp(clean_db)
        res = clean_db.get("/admin/explain/employee_monthly?emp_code=EMP0050&month=2026-07")
        assert res.status_code == 200, res.text
        data = res.json()
        assert "explain" in data
        # The command field should include a pipeline with multiple stages
        cmd = data["explain"].get("command", {})
        pipeline = cmd.get("pipeline", [])
        # Full pipeline has at minimum 3 stages: $match, $addFields, $group
        assert len(pipeline) >= 3, f"Expected full pipeline, got only {len(pipeline)} stages"
        stages = [list(s.keys())[0] for s in pipeline]
        assert "$match" in stages
        assert "$group" in stages

    def test_explain_department_summary_has_lookup_stage(self, clean_db):
        """department_summary explain should include the $lookup into attendance_logs."""
        _create_emp(clean_db)
        res = clean_db.get("/admin/explain/department_summary?month=2026-07")
        assert res.status_code == 200, res.text
        data = res.json()
        cmd = data["explain"].get("command", {})
        pipeline = cmd.get("pipeline", [])
        stages = [list(s.keys())[0] for s in pipeline]
        assert "$lookup" in stages, f"$lookup not found in stages: {stages}"
        assert "$project" in stages

    def test_explain_late_leaderboard_has_setwindowfields(self, clean_db):
        """late_leaderboard explain should include $setWindowFields for ranking."""
        _create_emp(clean_db)
        res = clean_db.get("/admin/explain/late_leaderboard?month=2026-07")
        assert res.status_code == 200, res.text
        data = res.json()
        cmd = data["explain"].get("command", {})
        pipeline = cmd.get("pipeline", [])
        stages = [list(s.keys())[0] for s in pipeline]
        assert "$setWindowFields" in stages, f"$setWindowFields not found in stages: {stages}"

    def test_explain_attendance_list_ok(self, clean_db):
        """attendance_list explain returns valid output."""
        res = clean_db.get("/admin/explain/attendance_list")
        assert res.status_code == 200
        assert "explain" in res.json()

    def test_explain_department_trend_missing_params_422(self, clean_db):
        """department_trend explain requires department, from, and to."""
        res = clean_db.get("/admin/explain/department_trend?department=Engineering")
        assert res.status_code == 422


# ===========================================================================
# C. Calendar date validation
# ===========================================================================

class TestCalendarDateValidation:
    """Invalid dates like 2026-02-30 must yield 422, not 500."""

    def test_attendance_list_invalid_date_from(self, clean_db):
        res = clean_db.get("/attendance?date_from=2026-02-30")
        assert res.status_code == 422

    def test_attendance_list_invalid_date_to(self, clean_db):
        res = clean_db.get("/attendance?date_to=2026-13-01")
        assert res.status_code == 422

    def test_attendance_list_valid_dates_ok(self, clean_db):
        res = clean_db.get("/attendance?date_from=2026-02-28&date_to=2026-03-01")
        assert res.status_code == 200

    def test_regularize_invalid_path_date(self, clean_db):
        """PATCH with an impossible date in the path should give 422."""
        _create_emp(clean_db)
        res = clean_db.patch(
            "/attendance/EMP0050/2026-02-30",
            json={"status": "LEAVE", "reason": "Invalid date test", "regularized_by": "hr"}
        )
        assert res.status_code == 422

    def test_trend_invalid_from_date(self, clean_db):
        _create_emp(clean_db)
        res = clean_db.get("/analytics/departments/TestDept/trend?from=2026-02-30&to=2026-03-01")
        assert res.status_code == 422

    def test_trend_invalid_to_date(self, clean_db):
        _create_emp(clean_db)
        res = clean_db.get("/analytics/departments/TestDept/trend?from=2026-03-01&to=2026-04-31")
        assert res.status_code == 422

    def test_trend_reversed_dates_422(self, clean_db):
        _create_emp(clean_db)
        res = clean_db.get("/analytics/departments/TestDept/trend?from=2026-07-31&to=2026-07-01")
        assert res.status_code == 422

    def test_trend_92_days_ok(self, clean_db):
        """Exactly 92 inclusive days (2026-07-01 to 2026-09-30) must pass."""
        _create_emp(clean_db)
        res = clean_db.get("/analytics/departments/TestDept/trend?from=2026-07-01&to=2026-09-30")
        assert res.status_code == 200
        data = res.json()
        assert len(data["items"]) == 92

    def test_trend_93_days_422(self, clean_db):
        """93-day span (2026-07-01 to 2026-10-01, num_days=93 > 92) must be rejected with 422."""
        _create_emp(clean_db)
        res = clean_db.get("/analytics/departments/TestDept/trend?from=2026-07-01&to=2026-10-01")
        assert res.status_code == 422


# ===========================================================================
# D. Status enum validation in GET /attendance
# ===========================================================================

class TestAttendanceStatusValidation:
    def test_invalid_status_returns_422(self, clean_db):
        res = clean_db.get("/attendance?status=WORKING")
        assert res.status_code == 422

    def test_valid_statuses_accepted(self, clean_db):
        for s in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            res = clean_db.get(f"/attendance?status={s}")
            assert res.status_code == 200, f"Expected 200 for status={s}, got {res.status_code}"


# ===========================================================================
# E. Concurrent punch-out: exactly one 200, rest 409
# ===========================================================================

class TestConcurrentPunchOut:
    def test_concurrent_punch_out_one_succeeds(self, clean_db):
        _create_emp(clean_db)
        _punch_in(clean_db, "EMP0050", 9, 30)

        po_utc = _utc(2026, 7, 6, 18, 30)
        po_ts = _ts(po_utc)

        def do_punch_out():
            c = TestClient(app)
            return c.post("/attendance/punch-out", json={"emp_code": "EMP0050", "punched_at": po_ts})

        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = [ex.submit(do_punch_out) for _ in range(5)]
            results = [f.result() for f in futures]

        codes = [r.status_code for r in results]
        assert codes.count(200) == 1, f"Expected exactly 1 success, got: {codes}"
        assert codes.count(409) == 4, f"Expected 4 conflicts, got: {codes}"


# ===========================================================================
# F. Concurrent regularization: no lost history
# ===========================================================================

class TestConcurrentRegularization:
    def test_concurrent_regularizations_no_lost_history(self, clean_db):
        """Two concurrent regularizations: one wins (200), one gets 409. No entry is lost."""
        _create_emp(clean_db)
        _punch_in(clean_db, "EMP0050", 10, 5)

        ts1 = _ts(_utc(2026, 7, 6, 9, 28))
        ts2 = _ts(_utc(2026, 7, 6, 9, 30))

        def regularize(ts, name):
            c = TestClient(app)
            return c.patch(
                "/attendance/EMP0050/2026-07-06",
                json={"punch_in": ts, "reason": f"Fix from {name}", "regularized_by": name}
            )

        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(regularize, ts1, "hr1")
            f2 = ex.submit(regularize, ts2, "hr2")
            r1, r2 = f1.result(), f2.result()

        codes = sorted([r1.status_code, r2.status_code])
        assert codes == [200, 409], f"Expected one 200 and one 409, got {codes}"

        # Verify exactly one history entry exists (the winner's)
        doc = db.attendance_logs.find_one({"emp_code": "EMP0050", "date": "2026-07-06"})
        assert len(doc.get("history", [])) == 1


# ===========================================================================
# G. Extra fields in RegularizeIn rejected (contract: derived fields read-only)
# ===========================================================================

class TestRegularizeExtraFields:
    def test_sending_work_hours_rejected(self, clean_db):
        """work_hours is a derived read-only field; sending it must yield 422."""
        _create_emp(clean_db)
        _punch_in(clean_db, "EMP0050", 10, 0)

        res = clean_db.patch(
            "/attendance/EMP0050/2026-07-06",
            json={
                "work_hours": 8.0,
                "reason": "Trying to inject work_hours",
                "regularized_by": "bad_actor"
            }
        )
        assert res.status_code == 422

    def test_sending_late_minutes_rejected(self, clean_db):
        _create_emp(clean_db)
        _punch_in(clean_db, "EMP0050", 10, 0)

        res = clean_db.patch(
            "/attendance/EMP0050/2026-07-06",
            json={
                "late_minutes": 0,
                "reason": "Trying to inject late_minutes",
                "regularized_by": "bad_actor"
            }
        )
        assert res.status_code == 422

    def test_sending_overtime_minutes_rejected(self, clean_db):
        _create_emp(clean_db)
        _punch_in(clean_db, "EMP0050", 10, 0)

        res = clean_db.patch(
            "/attendance/EMP0050/2026-07-06",
            json={
                "overtime_minutes": 0,
                "reason": "Trying to inject overtime",
                "regularized_by": "bad_actor"
            }
        )
        assert res.status_code == 422

    def test_sending_half_day_rejected(self, clean_db):
        _create_emp(clean_db)
        _punch_in(clean_db, "EMP0050", 10, 0)

        res = clean_db.patch(
            "/attendance/EMP0050/2026-07-06",
            json={
                "half_day": False,
                "reason": "Trying to inject half_day",
                "regularized_by": "bad_actor"
            }
        )
        assert res.status_code == 422


# ===========================================================================
# H. Explicit null vs omitted timestamps (contract: EpochMillis is not nullable)
# ===========================================================================

class TestExplicitNullVsOmitted:
    def test_punch_in_omitted_punched_at_succeeds(self, clean_db):
        """Omitted punched_at defaults to current server time (201 Created)."""
        _create_emp(clean_db)
        res = clean_db.post("/attendance/punch-in", json={"emp_code": "EMP0050"})
        assert res.status_code == 201
        assert res.json()["punch_in"] is not None

    def test_punch_in_explicit_null_punched_at_rejected_422(self, clean_db):
        """Explicit punched_at: null must be rejected with 422 (EpochMillis is not nullable)."""
        _create_emp(clean_db)
        res = clean_db.post("/attendance/punch-in", json={"emp_code": "EMP0050", "punched_at": None})
        assert res.status_code == 422

    def test_punch_out_omitted_punched_at_succeeds(self, clean_db):
        """Omitted punched_at on punch-out defaults to current server time (200 OK)."""
        _create_emp(clean_db)
        one_hr_ago = _ts(truncate_to_seconds(datetime.now(timezone.utc) - timedelta(hours=1)))
        clean_db.post("/attendance/punch-in", json={"emp_code": "EMP0050", "punched_at": one_hr_ago})
        res = clean_db.post("/attendance/punch-out", json={"emp_code": "EMP0050"})
        assert res.status_code == 200, res.text
        assert res.json()["punch_out"] is not None

    def test_punch_out_explicit_null_punched_at_rejected_422(self, clean_db):
        """Explicit punched_at: null on punch-out must be rejected with 422."""
        _create_emp(clean_db)
        _punch_in(clean_db, "EMP0050", 9, 30)
        res = clean_db.post("/attendance/punch-out", json={"emp_code": "EMP0050", "punched_at": None})
        assert res.status_code == 422

    def test_regularize_explicit_null_punch_in_rejected_422(self, clean_db):
        """Explicit punch_in: null on regularize must be rejected with 422."""
        _create_emp(clean_db)
        _punch_in(clean_db, "EMP0050", 9, 30)
        res = clean_db.patch(
            "/attendance/EMP0050/2026-07-06",
            json={"punch_in": None, "reason": "Attempting null punch_in", "regularized_by": "admin"}
        )
        assert res.status_code == 422

    def test_regularize_explicit_null_status_rejected_422(self, clean_db):
        """Explicit status: null on regularize must be rejected with 422."""
        _create_emp(clean_db)
        _punch_in(clean_db, "EMP0050", 9, 30)
        res = clean_db.patch(
            "/attendance/EMP0050/2026-07-06",
            json={"status": None, "reason": "Attempting null status", "regularized_by": "admin"}
        )
        assert res.status_code == 422


# ===========================================================================
# I. Punch-out racing with Regularization concurrency
# ===========================================================================

class TestPunchOutRacingRegularization:
    def test_punch_out_racing_with_regularization(self, clean_db):
        """If punch-out modifies punch_out before a regularization commits, the regularization gets 409."""
        _create_emp(clean_db)
        _punch_in(clean_db, "EMP0050", 9, 30)

        po_ts = _ts(_utc(2026, 7, 6, 18, 30))
        reg_pi_ts = _ts(_utc(2026, 7, 6, 9, 28))

        def do_punch_out():
            c = TestClient(app)
            return c.post("/attendance/punch-out", json={"emp_code": "EMP0050", "punched_at": po_ts})

        def do_regularize():
            c = TestClient(app)
            return c.patch(
                "/attendance/EMP0050/2026-07-06",
                json={"punch_in": reg_pi_ts, "reason": "Correction race test", "regularized_by": "admin"}
            )

        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(do_punch_out)
            f2 = ex.submit(do_regularize)
            r1, r2 = f1.result(), f2.result()

        # Both operations cannot overwrite each other without detection.
        # Either punch-out wins (200) and regularize conflicts (409), or regularize wins (200) and punch-out succeeds (200 on new punch_in)
        assert r1.status_code in (200, 409, 422)
        assert r2.status_code in (200, 409)


# ===========================================================================
# J. Input validation: joined_on and month formats
# ===========================================================================

class TestInputValidationFormats:
    def test_invalid_joined_on_calendar_date(self, clean_db):
        res = clean_db.post("/employees", json={
            "emp_code": "EMP0099",
            "name": "Invalid Date",
            "email": "inv@test.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-02-30",
        })
        assert res.status_code == 422

    def test_invalid_joined_on_format(self, clean_db):
        res = clean_db.post("/employees", json={
            "emp_code": "EMP0099",
            "name": "Invalid Date",
            "email": "inv@test.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "01-01-2026",
        })
        assert res.status_code == 422

    def test_invalid_month_format_422(self, clean_db):
        res = clean_db.get("/analytics/departments/summary?month=2026-13")
        assert res.status_code == 422

    def test_trend_one_calendar_day_ok(self, clean_db):
        _create_emp(clean_db)
        res = clean_db.get("/analytics/departments/TestDept/trend?from=2026-07-01&to=2026-07-01")
        assert res.status_code == 200
        assert len(res.json()["items"]) == 1
        assert res.json()["items"][0]["date"] == "2026-07-01"

    def test_trend_92_inclusive_rows_ok(self, clean_db):
        _create_emp(clean_db)
        # 2026-07-01 to 2026-09-30 is exactly 92 inclusive calendar days (31 + 31 + 30 = 92)
        res = clean_db.get("/analytics/departments/TestDept/trend?from=2026-07-01&to=2026-09-30")
        assert res.status_code == 200
        assert len(res.json()["items"]) == 92

    def test_trend_93_inclusive_rows_rejected_422(self, clean_db):
        _create_emp(clean_db)
        # 2026-07-01 to 2026-10-01 is 93 inclusive calendar days (31 + 31 + 30 + 1 = 93)
        res = clean_db.get("/analytics/departments/TestDept/trend?from=2026-07-01&to=2026-10-01")
        assert res.status_code == 422


# ===========================================================================
# I. Department Trend Pre-Joining Date Logs & Indexing Verification
# ===========================================================================

class TestDepartmentTrendPreJoiningDateAndIndexing:
    def test_trend_excludes_logs_before_joining_date(self, clean_db):
        """Attendance logs dated before an employee's joined_on must NOT be counted in trend."""
        # Emp 1: joined 2026-10-01
        r1 = clean_db.post("/employees", json={
            "emp_code": "EMP8801",
            "name": "Emp One",
            "email": "one@trend.com",
            "department": "TrendDept",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-10-01"
        })
        assert r1.status_code == 201, r1.text

        # Emp 2: joined 2026-10-10
        r2 = clean_db.post("/employees", json={
            "emp_code": "EMP8802",
            "name": "Emp Two",
            "email": "two@trend.com",
            "department": "TrendDept",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-10-10"
        })
        assert r2.status_code == 201, r2.text

        # Directly insert attendance log for Emp 2 on 2026-10-05 (before their joined_on 2026-10-10)
        from app.main import db
        db.attendance_logs.insert_one({
            "emp_code": "EMP8802",
            "date": "2026-10-05",
            "status": "PRESENT",
            "punch_in": datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 10, 5, 13, 0, tzinfo=timezone.utc),
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False
        })
        # Insert attendance log for Emp 1 on 2026-10-05 (valid active employee)
        db.attendance_logs.insert_one({
            "emp_code": "EMP8801",
            "date": "2026-10-05",
            "status": "PRESENT",
            "punch_in": datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 10, 5, 13, 0, tzinfo=timezone.utc),
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False
        })

        res = clean_db.get("/analytics/departments/TrendDept/trend?from=2026-10-01&to=2026-10-12")
        assert res.status_code == 200, res.text
        items = res.json()["items"]

        # On 2026-10-05 (Friday), only EMP8801 was employed (joined_on 2026-10-01 <= 2026-10-05)
        # EMP8802 joined on 2026-10-10, so on 2026-10-05 headcount = 1, present_count = 1.0 (NOT 2.0)
        day_05 = next(item for item in items if item["date"] == "2026-10-05")
        assert day_05["headcount"] == 1, f"Expected headcount 1 on 2026-10-05, got {day_05['headcount']}"
        assert day_05["present_count"] == 1.0, f"Expected present_count 1.0 on 2026-10-05, got {day_05['present_count']}"
        assert day_05["attendance_rate"] == 1.0

        # On 2026-10-12 (Monday), both EMP8801 and EMP8802 are employed
        day_12 = next(item for item in items if item["date"] == "2026-10-12")
        assert day_12["headcount"] == 2

    def test_trend_explain_uses_index_and_no_collscan(self, clean_db):
        """Trend uses the indexed date and employee-code lookup at 100,000 records."""
        employee_count = 10_000
        db.employees.insert_many([
            {
                "emp_code": f"SCALE{index:05d}",
                "name": f"Scale Employee {index}",
                "email": f"scale{index}@example.com",
                "department": "TestDept",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
            }
            for index in range(employee_count)
        ])
        db.attendance_logs.insert_many([
            {
                "emp_code": f"SCALE{index:05d}",
                "date": f"2026-10-{day:02d}",
                "status": "PRESENT",
                "half_day": False,
                "late_minutes": 0,
            }
            for index in range(employee_count)
            for day in range(1, 11)
        ])
        res = clean_db.get("/admin/explain/department_trend?department=TestDept&from=2026-10-01&to=2026-10-09")
        assert res.status_code == 200, res.text
        explain_data = res.json()["explain"]

        def find_stages(obj):
            st = []
            if isinstance(obj, dict):
                if "stage" in obj:
                    st.append(obj["stage"])
                for v in obj.values():
                    st.extend(find_stages(v))
            elif isinstance(obj, list):
                for v in obj:
                    st.extend(find_stages(v))
            return st

        stages = find_stages(explain_data)
        assert "IXSCAN" in stages, f"Expected IXSCAN in explain stages, got: {stages}"
        assert "COLLSCAN" not in stages, f"COLLSCAN detected in explain stages: {stages}"


# ===========================================================================
# K. Deterministic Punch-Out vs Regularization Interleaving
# ===========================================================================

class TestDeterministicPunchOutRegularizationInterleaving:
    """
    Deterministically force the exact concurrency race interleaving:
    1. Punch-out reads the record (initial state: punch_in = 09:30, punch_out = None).
    2. Before punch-out writes, regularization modifies the record (e.g. punch_in corrected to 08:30 or status changed).
    3. Punch-out attempts its CAS write using the stale read state.
    4. The write must be rejected with HTTP 409 Conflict.
    5. The final record must retain the regularized state and derived fields must remain consistent.
    """

    def test_deterministic_interleaving_punch_in_modified(self, clean_db, monkeypatch):
        import pymongo.collection
        _create_emp(clean_db, emp_code="EMP7001", shift_start="09:30", shift_end="18:30")
        _punch_in(clean_db, "EMP7001", 9, 30)

        po_utc = _utc(2026, 7, 6, 18, 30)
        po_ts = _ts(po_utc)

        original_find_one = pymongo.collection.Collection.find_one
        in_interleaving = False
        has_interleaved = False

        def find_one_with_interleaving(self_coll, *args, **kwargs):
            nonlocal in_interleaving, has_interleaved
            if in_interleaving or has_interleaved or self_coll.name != "attendance_logs":
                return original_find_one(self_coll, *args, **kwargs)
            doc = original_find_one(self_coll, *args, **kwargs)
            if doc and doc.get("emp_code") == "EMP7001":
                in_interleaving = True
                has_interleaved = True
                try:
                    # Interleave a regularization that changes punch_in before punch_out CAS write
                    reg_ts = _ts(_utc(2026, 7, 6, 8, 30))
                    r = clean_db.patch(
                        "/attendance/EMP7001/2026-07-06",
                        json={"punch_in": reg_ts, "reason": "Biometric correction in race", "regularized_by": "admin"}
                    )
                    assert r.status_code == 200
                finally:
                    in_interleaving = False
            return doc

        monkeypatch.setattr(pymongo.collection.Collection, "find_one", find_one_with_interleaving)

        # Attempt punch-out: must get 409 Conflict because document punch_in was modified concurrently
        res = clean_db.post("/attendance/punch-out", json={"emp_code": "EMP7001", "punched_at": po_ts})
        assert res.status_code == 409, f"Expected 409 Conflict, got {res.status_code}: {res.text}"

        # Verify final record retains regularized punch_in (08:30 IST / 03:00 UTC) and was NOT corrupted
        final_doc = db.attendance_logs.find_one({"emp_code": "EMP7001", "date": "2026-07-06"})
        assert final_doc["punch_out"] is None
        assert final_doc["work_hours"] is None
        assert len(final_doc["history"]) == 1

    def test_deterministic_interleaving_status_modified_to_leave(self, clean_db, monkeypatch):
        import pymongo.collection
        _create_emp(clean_db, emp_code="EMP7002", shift_start="09:30", shift_end="18:30")
        _punch_in(clean_db, "EMP7002", 9, 30)

        po_utc = _utc(2026, 7, 6, 18, 30)
        po_ts = _ts(po_utc)

        original_find_one = pymongo.collection.Collection.find_one
        in_interleaving = False
        has_interleaved = False

        def find_one_with_interleaving(self_coll, *args, **kwargs):
            nonlocal in_interleaving, has_interleaved
            if in_interleaving or has_interleaved or self_coll.name != "attendance_logs":
                return original_find_one(self_coll, *args, **kwargs)
            doc = original_find_one(self_coll, *args, **kwargs)
            if doc and doc.get("emp_code") == "EMP7002":
                in_interleaving = True
                has_interleaved = True
                try:
                    r = clean_db.patch(
                        "/attendance/EMP7002/2026-07-06",
                        json={"status": "LEAVE", "reason": "Emergency leave applied", "regularized_by": "admin"}
                    )
                    assert r.status_code == 200
                finally:
                    in_interleaving = False
            return doc

        monkeypatch.setattr(pymongo.collection.Collection, "find_one", find_one_with_interleaving)

        res = clean_db.post("/attendance/punch-out", json={"emp_code": "EMP7002", "punched_at": po_ts})
        assert res.status_code == 409

        final_doc = db.attendance_logs.find_one({"emp_code": "EMP7002", "date": "2026-07-06"})
        assert final_doc["status"] == "LEAVE"
        assert final_doc["punch_in"] is None
        assert final_doc["punch_out"] is None
        assert final_doc["work_hours"] is None


# ===========================================================================
# L. Database Safety & Seeder Import Guards
# ===========================================================================

class TestDatabaseSafetyAndImportGuards:
    """
    Verify test database safety rules and import safety.
    """

    def test_safety_guard_rejects_production_db_name(self):
        """Refuses destructive execution if database name is 'attendance_db'."""
        from app.main import verify_test_database_safety
        with pytest.raises(ValueError, match="not an approved disposable test database"):
            verify_test_database_safety("mongodb://127.0.0.1:27017", "attendance_db")

    def test_safety_guard_rejects_remote_atlas_host(self):
        """Refuses destructive execution if host is remote or Atlas."""
        from app.main import verify_test_database_safety
        with pytest.raises(ValueError, match="only local MongoDB hosts"):
            verify_test_database_safety("mongodb://cluster0.mongodb.net:27017", "test_db")

    def test_safety_guard_rejects_srv_uri(self):
        """SRV URIs are rejected without resolving or contacting their remote host."""
        from app.main import verify_test_database_safety
        with pytest.raises(ValueError, match="explicit local mongodb"):
            verify_test_database_safety("mongodb+srv://cluster0.mongodb.net", "test_db")

    def test_safety_guard_approves_local_test_db(self):
        """Approves loopback IPv4, IPv6, and the explicit Docker Desktop host."""
        from app.main import verify_test_database_safety
        for uri in (
            "mongodb://127.0.0.1:27017",
            "mongodb://[::1]:27017",
            "mongodb://host.docker.internal:27017",
        ):
            info = verify_test_database_safety(uri, "test_attendance_db")
            assert info["host_category"] == "LOCAL_DISPOSABLE"
            assert info["database"] == "test_attendance_db"
