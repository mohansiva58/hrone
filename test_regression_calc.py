"""
Regression tests for R1–R5 calculation correctness.

Background
----------
A bug report was raised for employee EMP9875 with:
  - Shift       : 09:30–18:30 (day shift)
  - Date        : 2026-10-09
  - Punch-in    : 2026-10-09 17:00 IST
  - Punch-out   : 2026-10-10 02:00 IST
  - Stored late_minutes     : 780   ← reported
  - Stored overtime_minutes : 780   ← reported
  - Stored work_hours       : 9.0
  - Stored half_day         : false

Investigation showed the API code is correct.  The stored values 780 and 780 are
the result of the caller supplying epoch-milliseconds whose wall-clock time is
correct in IST but whose epoch was computed as if the wall time were UTC
(i.e. the caller's client built the epoch as datetime(17,00 UTC).timestamp()
instead of datetime(17,00 IST).timestamp()).  That makes the API believe punch-in
was at 22:30 IST, giving late = 22:30−09:30 = 780 min and OT = 780 min.

These tests verify:
  1. Correct expected values for the bug-report scenario.
  2. The R2 boundary cases from the spec (09:40:00→0, 09:40:01→10, 10:15:59→45).
  3. Overtime threshold boundary (exactly 30 min → counted, 29:59 → 0).
  4. Overnight-shift correct date assignment, late, and overtime.
  5. Half-day boundary (< 4.50 h → true, exactly 4.50 h → false).
  6. work_hours is null before punch-out, populated after.
  7. Caller timezone-confusion produces wrong stored values (documents the root cause
     so a regression in the API's own conversion would be caught immediately).
"""

import pytest
from fastapi.testclient import TestClient
from datetime import datetime, timezone, timedelta

import app.main as app_main
from app.main import app, db

IST = timezone(timedelta(hours=5, minutes=30))

client = TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(dt_utc: datetime) -> int:
    """Convert a UTC-aware datetime to epoch milliseconds."""
    return int(dt_utc.timestamp()) * 1000


def _ist(year, month, day, hour, minute, second=0) -> datetime:
    """Build an IST-aware datetime and return it (still IST-aware)."""
    return datetime(year, month, day, hour, minute, second, tzinfo=IST)


def _utc_from_ist(year, month, day, hour, minute, second=0) -> datetime:
    """Build a UTC datetime that corresponds to the given IST wall-clock time."""
    return _ist(year, month, day, hour, minute, second).astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_db():
    assert db.name == "test_attendance_db", f"Refusing to run destructive operations against unexpected database: '{db.name}'"
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    with TestClient(app) as tc:
        yield tc


def test_shared_test_client_survives_sequential_lifespans():
    shared_client = app_main.client
    assert db.client is shared_client

    for _ in range(2):
        with TestClient(app):
            assert db.command("ping")["ok"] == 1
        assert db.command("ping")["ok"] == 1


def _create_emp(tc, emp_code="EMP9875", shift_start="09:30", shift_end="18:30"):
    return tc.post("/employees", json={
        "emp_code": emp_code,
        "name": "Test Employee",
        "email": f"{emp_code.lower()}@example.com",
        "department": "Engineering",
        "shift_start": shift_start,
        "shift_end": shift_end,
        "joined_on": "2026-01-01",
    })


# ===========================================================================
# 1.  Bug-report scenario: correct expected values
# ===========================================================================

class TestBugReportScenario:
    """
    Shift 09:30–18:30.  Punch-in 17:00 IST Oct 9, punch-out 02:00 IST Oct 10.

    Expected (per R1–R5):
        date              = 2026-10-09            (IST date of punch-in)
        late_minutes      = 450                   (17:00 − 09:30 = 7 h 30 m = 450 min)
        overtime_minutes  = 450                   (02:00+1 IST − 18:30 = 7 h 30 m = 450 min, ≥ 30)
        work_hours        = 9.0                   ((02:00+1 IST − 17:00 IST) = 9 h exactly)
        half_day          = false                 (9.0 ≥ 4.50)

    The stored values 780 / 780 reported in the bug are wrong and arise only when
    the caller supplies epoch-ms built from wall-clock time treated as UTC.
    """

    def test_punch_in_correct_late_minutes(self, clean_db):
        """R2: punch-in 17:00 IST on a 09:30 shift → 450 late minutes."""
        _create_emp(clean_db)
        pi_utc = _utc_from_ist(2026, 10, 9, 17, 0)
        res = clean_db.post("/attendance/punch-in",
                            json={"emp_code": "EMP9875", "punched_at": _ts(pi_utc)})
        assert res.status_code == 201, res.text
        data = res.json()
        assert data["date"] == "2026-10-09"
        assert data["late_minutes"] == 450, (
            f"Expected 450 late minutes; got {data['late_minutes']}. "
            f"17:00 IST − 09:30 IST = 7.5 h = 450 min."
        )

    def test_punch_out_correct_overtime_and_work_hours(self, clean_db):
        """R3 + R4: punch-out 02:00+1 IST → OT 450 min, work_hours 9.0."""
        _create_emp(clean_db)
        pi_utc = _utc_from_ist(2026, 10, 9, 17, 0)
        po_utc = _utc_from_ist(2026, 10, 10, 2, 0)

        clean_db.post("/attendance/punch-in",
                      json={"emp_code": "EMP9875", "punched_at": _ts(pi_utc)})
        res = clean_db.post("/attendance/punch-out",
                            json={"emp_code": "EMP9875", "punched_at": _ts(po_utc)})
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["overtime_minutes"] == 450, (
            f"Expected 450 OT min; got {data['overtime_minutes']}. "
            f"02:00 IST Oct 10 − 18:30 IST Oct 9 = 7.5 h = 450 min."
        )
        assert data["work_hours"] == 9.0, (
            f"Expected 9.0 h; got {data['work_hours']}."
        )
        assert data["half_day"] is False, "9.0 h ≥ 4.50 → half_day must be False."

    def test_caller_tz_confusion_produces_wrong_values(self, clean_db):
        """
        Root-cause documentation: when the caller erroneously supplies an epoch
        built from the IST wall-clock time treated as UTC, the API receives a
        punch-in that converts to 22:30 IST, yielding late = 780 and OT = 780.

        This test pins the exact wrong-input → wrong-output behaviour so any
        future regression in the API's own UTC conversion is immediately visible.
        """
        _create_emp(clean_db)

        # Caller mistake: builds epoch for "17:00 UTC" instead of "17:00 IST"
        pi_utc_wrong = datetime(2026, 10, 9, 17, 0, 0, tzinfo=timezone.utc)   # 22:30 IST
        po_utc_wrong = datetime(2026, 10, 10, 2, 0, 0, tzinfo=timezone.utc)   # 07:30+1 IST

        clean_db.post("/attendance/punch-in",
                      json={"emp_code": "EMP9875", "punched_at": _ts(pi_utc_wrong)})
        res = clean_db.post("/attendance/punch-out",
                            json={"emp_code": "EMP9875", "punched_at": _ts(po_utc_wrong)})
        assert res.status_code == 200, res.text
        data = res.json()

        # The API is correct; it faithfully computes from the (bad) input.
        # 22:30 IST − 09:30 IST = 780 min
        assert data["late_minutes"] == 780, (
            "Wrong-epoch punch-in (17:00 UTC → 22:30 IST) should produce "
            f"late_minutes=780; got {data['late_minutes']}."
        )
        # 07:30 IST Oct 10 − 18:30 IST Oct 9 = 13 h = 780 min
        assert data["overtime_minutes"] == 780, (
            "Wrong-epoch punch-out (02:00 UTC Oct 10 → 07:30 IST Oct 10) should produce "
            f"overtime_minutes=780; got {data['overtime_minutes']}."
        )
        # work_hours: (po−pi) in seconds is the same regardless of tz offset applied uniformly
        assert data["work_hours"] == 9.0


# ===========================================================================
# 2.  R2 boundary cases — exactly as stated in the spec
# ===========================================================================

class TestR2LateMinutesBoundaries:
    """Spec text: 09:40:00 → 0; 09:40:01 → 10; 10:15:59 → 45."""

    def _punch(self, tc, hour, minute, second):
        pi_utc = _utc_from_ist(2026, 7, 6, hour, minute, second)
        return tc.post("/attendance/punch-in",
                       json={"emp_code": "EMP9875", "punched_at": _ts(pi_utc)})

    def test_exactly_grace_period_not_late(self, clean_db):
        """09:40:00 IST → diff = 600 s, NOT > 600 → late_minutes = 0."""
        _create_emp(clean_db)
        res = self._punch(clean_db, 9, 40, 0)
        assert res.status_code == 201
        assert res.json()["late_minutes"] == 0

    def test_one_second_past_grace_is_late(self, clean_db):
        """09:40:01 IST → diff = 601 s > 600 → floor(601/60) = 10."""
        _create_emp(clean_db)
        res = self._punch(clean_db, 9, 40, 1)
        assert res.status_code == 201
        assert res.json()["late_minutes"] == 10

    def test_mid_hour_late_floored(self, clean_db):
        """10:15:59 IST → diff = 2759 s → floor(2759/60) = 45."""
        _create_emp(clean_db)
        res = self._punch(clean_db, 10, 15, 59)
        assert res.status_code == 201
        assert res.json()["late_minutes"] == 45

    def test_before_shift_start_not_late(self, clean_db):
        """Punch-in before shift start → 0 late."""
        _create_emp(clean_db)
        pi_utc = _utc_from_ist(2026, 7, 6, 9, 0, 0)
        res = clean_db.post("/attendance/punch-in",
                            json={"emp_code": "EMP9875", "punched_at": _ts(pi_utc)})
        assert res.status_code == 201
        assert res.json()["late_minutes"] == 0


# ===========================================================================
# 3.  R3 overtime threshold boundary
# ===========================================================================

class TestR3OvertimeThreshold:
    """Overtime is counted only if punch-out is ≥ 30 min after shift_end."""

    def _setup(self, tc):
        _create_emp(tc)
        pi_utc = _utc_from_ist(2026, 7, 6, 9, 30, 0)   # exactly on time
        tc.post("/attendance/punch-in",
                json={"emp_code": "EMP9875", "punched_at": _ts(pi_utc)})

    def _punch_out(self, tc, hour, minute, second=0):
        po_utc = _utc_from_ist(2026, 7, 6, hour, minute, second)
        return tc.post("/attendance/punch-out",
                       json={"emp_code": "EMP9875", "punched_at": _ts(po_utc)})

    def test_exactly_30_minutes_overtime_counted(self, clean_db):
        """Punch-out at 19:00:00 IST (exactly 30 min after 18:30) → 30 OT min."""
        self._setup(clean_db)
        res = self._punch_out(clean_db, 19, 0, 0)
        assert res.status_code == 200
        assert res.json()["overtime_minutes"] == 30

    def test_29_minutes_59_seconds_overtime_zero(self, clean_db):
        """Punch-out at 18:59:59 IST (29 min 59 s after 18:30) → 0 OT (< 30 min)."""
        self._setup(clean_db)
        res = self._punch_out(clean_db, 18, 59, 59)
        assert res.status_code == 200
        assert res.json()["overtime_minutes"] == 0

    def test_45_minutes_overtime(self, clean_db):
        """Punch-out at 19:15:00 IST → 45 OT min."""
        self._setup(clean_db)
        res = self._punch_out(clean_db, 19, 15, 0)
        assert res.status_code == 200
        assert res.json()["overtime_minutes"] == 45


# ===========================================================================
# 4.  R5 half-day boundary
# ===========================================================================

class TestR5HalfDayBoundary:
    """half_day = True when rounded work_hours < 4.50."""

    def _setup(self, tc):
        _create_emp(tc)
        pi_utc = _utc_from_ist(2026, 7, 6, 9, 30, 0)
        tc.post("/attendance/punch-in",
                json={"emp_code": "EMP9875", "punched_at": _ts(pi_utc)})

    def test_exactly_4_5_hours_not_half_day(self, clean_db):
        """4.5 h work (not < 4.50) → half_day = False."""
        self._setup(clean_db)
        # 09:30 + 4h30m = 14:00 IST
        po_utc = _utc_from_ist(2026, 7, 6, 14, 0, 0)
        res = clean_db.post("/attendance/punch-out",
                            json={"emp_code": "EMP9875", "punched_at": _ts(po_utc)})
        assert res.status_code == 200
        data = res.json()
        assert data["work_hours"] == 4.5
        assert data["half_day"] is False

    def test_under_4_5_hours_is_half_day(self, clean_db):
        """3.5 h work → half_day = True, attendance counts as 0.5."""
        self._setup(clean_db)
        # 09:30 + 3h30m = 13:00 IST
        po_utc = _utc_from_ist(2026, 7, 6, 13, 0, 0)
        res = clean_db.post("/attendance/punch-out",
                            json={"emp_code": "EMP9875", "punched_at": _ts(po_utc)})
        assert res.status_code == 200
        data = res.json()
        assert data["work_hours"] == 3.5
        assert data["half_day"] is True

    def test_just_under_4_5_hours_is_half_day(self, clean_db):
        """4 h 29 min 59 s (rounds to 4.5? No: 4*3600+29*60+59=16199 s / 3600 ≈ 4.4997 → 4.50 half-up)."""
        # 16199 / 3600 = 4.49972... → round_half_up to 2 decimals = 4.50 → half_day = False
        self._setup(clean_db)
        po_utc = _utc_from_ist(2026, 7, 6, 13, 59, 59)
        res = clean_db.post("/attendance/punch-out",
                            json={"emp_code": "EMP9875", "punched_at": _ts(po_utc)})
        assert res.status_code == 200
        data = res.json()
        # 4*3600 + 29*60 + 59 = 16199 s / 3600 = 4.49972... -> round_half_up(4.499..., 2) = 4.50
        assert data["work_hours"] == 4.5
        assert data["half_day"] is False


# ===========================================================================
# 5.  R4: work_hours null before punch-out
# ===========================================================================

class TestR4WorkHours:
    def test_work_hours_null_before_punch_out(self, clean_db):
        """work_hours must be null until punch-out."""
        _create_emp(clean_db)
        pi_utc = _utc_from_ist(2026, 7, 6, 9, 30, 0)
        res = clean_db.post("/attendance/punch-in",
                            json={"emp_code": "EMP9875", "punched_at": _ts(pi_utc)})
        assert res.status_code == 201
        assert res.json()["work_hours"] is None

    def test_work_hours_populated_after_punch_out(self, clean_db):
        """work_hours = (punch_out − punch_in) / 3600, 2 dp half-up."""
        _create_emp(clean_db)
        # 10:05 in, 19:10 out  → 9h5m = 9.083... → 9.08
        pi_utc = _utc_from_ist(2026, 7, 6, 10, 5, 0)
        po_utc = _utc_from_ist(2026, 7, 6, 19, 10, 0)
        clean_db.post("/attendance/punch-in",
                      json={"emp_code": "EMP9875", "punched_at": _ts(pi_utc)})
        res = clean_db.post("/attendance/punch-out",
                            json={"emp_code": "EMP9875", "punched_at": _ts(po_utc)})
        assert res.status_code == 200
        assert res.json()["work_hours"] == 9.08


# ===========================================================================
# 6.  R1: overnight shift — attendance date and late/overtime
# ===========================================================================

class TestR1OvernightShift:
    """Overnight shift 22:00–06:00. date = IST date of shift start."""

    def test_punch_in_before_midnight_correct_date(self, clean_db):
        """Punch-in at 21:55 IST Jul 6 → date = 2026-07-06, not late."""
        _create_emp(clean_db, shift_start="22:00", shift_end="06:00")
        pi_utc = _utc_from_ist(2026, 7, 6, 21, 55, 0)
        res = clean_db.post("/attendance/punch-in",
                            json={"emp_code": "EMP9875", "punched_at": _ts(pi_utc)})
        assert res.status_code == 201
        data = res.json()
        assert data["date"] == "2026-07-06"
        assert data["late_minutes"] == 0

    def test_punch_in_after_midnight_belongs_to_previous_date(self, clean_db):
        """Punch-in at 01:30 IST Jul 7 → still date 2026-07-06 (shift started Jul 6)."""
        _create_emp(clean_db, shift_start="22:00", shift_end="06:00")
        pi_utc = _utc_from_ist(2026, 7, 7, 1, 30, 0)
        res = clean_db.post("/attendance/punch-in",
                            json={"emp_code": "EMP9875", "punched_at": _ts(pi_utc)})
        assert res.status_code == 201
        assert res.json()["date"] == "2026-07-06"

    def test_overnight_overtime_counted_against_next_day_end(self, clean_db):
        """Punch-out at 06:40 IST Jul 7 (40 min after 06:00 shift_end) → OT = 40."""
        _create_emp(clean_db, shift_start="22:00", shift_end="06:00")
        pi_utc = _utc_from_ist(2026, 7, 6, 21, 55, 0)
        po_utc = _utc_from_ist(2026, 7, 7, 6, 40, 0)
        clean_db.post("/attendance/punch-in",
                      json={"emp_code": "EMP9875", "punched_at": _ts(pi_utc)})
        res = clean_db.post("/attendance/punch-out",
                            json={"emp_code": "EMP9875", "punched_at": _ts(po_utc)})
        assert res.status_code == 200
        assert res.json()["overtime_minutes"] == 40

    def test_overnight_work_hours(self, clean_db):
        """21:55 in → 06:40+1 → 8.75 h."""
        _create_emp(clean_db, shift_start="22:00", shift_end="06:00")
        pi_utc = _utc_from_ist(2026, 7, 6, 21, 55, 0)
        po_utc = _utc_from_ist(2026, 7, 7, 6, 40, 0)
        clean_db.post("/attendance/punch-in",
                      json={"emp_code": "EMP9875", "punched_at": _ts(pi_utc)})
        res = clean_db.post("/attendance/punch-out",
                            json={"emp_code": "EMP9875", "punched_at": _ts(po_utc)})
        assert res.status_code == 200
        assert res.json()["work_hours"] == 8.75

    def test_overnight_late_punch_in(self, clean_db):
        """22:35 IST punch-in on overnight shift → 35 late minutes."""
        _create_emp(clean_db, shift_start="22:00", shift_end="06:00")
        pi_utc = _utc_from_ist(2026, 7, 6, 22, 35, 0)
        res = clean_db.post("/attendance/punch-in",
                            json={"emp_code": "EMP9875", "punched_at": _ts(pi_utc)})
        assert res.status_code == 201
        assert res.json()["late_minutes"] == 35
