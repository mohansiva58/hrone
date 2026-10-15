"""
Comprehensive test suite for candidate_kit app/main.py
"""
import pytest
from fastapi.testclient import TestClient
from datetime import datetime, timezone, timedelta, date
from concurrent.futures import ThreadPoolExecutor

from app.main import app, db

client = TestClient(app)

@pytest.fixture(autouse=True)
def setup_db():
    assert db.name == "test_attendance_db", f"Refusing to run destructive operations against unexpected database: '{db.name}'"
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    with TestClient(app) as tc:
        yield tc

def test_health(setup_db):
    res = setup_db.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

def test_create_and_list_employees(setup_db):
    emp_data = {
        "emp_code": "EMP0001",
        "name": "Alice Smith",
        "email": "alice@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    }
    res = setup_db.post("/employees", json=emp_data)
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert isinstance(data["created_at"], int)

    # The employee-creation contract requires distinct shift boundaries.
    equal_shift_emp = dict(emp_data, emp_code="EMP0009", shift_start="09:30", shift_end="09:30")
    res_equal = setup_db.post("/employees", json=equal_shift_emp)
    assert res_equal.status_code == 422

    # Duplicate emp_code -> 409
    res_dup = setup_db.post("/employees", json=emp_data)
    assert res_dup.status_code == 409

    # List employees
    res_list = setup_db.get("/employees")
    assert res_list.status_code == 200
    l_data = res_list.json()
    assert l_data["total"] == 1
    assert len(l_data["items"]) == 1
    assert l_data["items"][0]["emp_code"] == "EMP0001"

def test_punch_in_type_validations(setup_db):
    emp_data = {
        "emp_code": "EMP0001",
        "name": "Alice Smith",
        "email": "alice@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    }
    setup_db.post("/employees", json=emp_data)

    # Float punched_at -> 422
    res1 = setup_db.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500000.5})
    assert res1.status_code == 422

    # String punched_at -> 422
    res2 = setup_db.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": "1783312500000"})
    assert res2.status_code == 422

    # Out of range punched_at -> 422
    res3 = setup_db.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 500})
    assert res3.status_code == 422

def test_punch_in_out_and_rules(setup_db):
    emp_data = {
        "emp_code": "EMP0002",
        "name": "Bob Jones",
        "email": "bob@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    }
    setup_db.post("/employees", json=emp_data)

    # 2026-07-06 09:40:01 IST -> 10 minutes late (R2)
    dt_in = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    ts_in = int(dt_in.timestamp()) * 1000

    res_in = setup_db.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": ts_in})
    assert res_in.status_code == 201, res_in.text
    in_data = res_in.json()
    assert in_data["date"] == "2026-07-06"
    assert in_data["late_minutes"] == 10

    # Duplicate punch-in for same date -> 409
    res_dup_in = setup_db.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": ts_in})
    assert res_dup_in.status_code == 409

    # Punch-out at 19:15:00 IST -> 45 min overtime (>= 30 min minimum per R3)
    dt_out = datetime(2026, 7, 6, 13, 45, 0, tzinfo=timezone.utc)
    ts_out = int(dt_out.timestamp()) * 1000

    res_out = setup_db.post("/attendance/punch-out", json={"emp_code": "EMP0002", "punched_at": ts_out})
    assert res_out.status_code == 200, res_out.text
    out_data = res_out.json()
    assert out_data["overtime_minutes"] == 45
    assert out_data["work_hours"] == 9.58
    assert out_data["half_day"] is False

    # Duplicate punch-out -> 409
    res_dup_out = setup_db.post("/attendance/punch-out", json={"emp_code": "EMP0002", "punched_at": ts_out})
    assert res_dup_out.status_code == 409

def test_overnight_shift(setup_db):
    emp_data = {
        "emp_code": "EMP0003",
        "name": "Charlie Night",
        "email": "charlie@example.com",
        "department": "Support",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-01"
    }
    setup_db.post("/employees", json=emp_data)

    # Punched in at 01:30 IST on July 7 (20:00 UTC July 6). Belongs to shift starting July 6 (R1).
    dt_in = datetime(2026, 7, 6, 20, 0, 0, tzinfo=timezone.utc)
    ts_in = int(dt_in.timestamp()) * 1000

    res_in = setup_db.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": ts_in})
    assert res_in.status_code == 201, res_in.text
    assert res_in.json()["date"] == "2026-07-06"

def test_regularization_rules(setup_db):
    emp_data = {
        "emp_code": "EMP0004",
        "name": "David Miller",
        "email": "david@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    }
    setup_db.post("/employees", json=emp_data)

    dt_in = datetime(2026, 7, 6, 4, 30, 0, tzinfo=timezone.utc)
    ts_in = int(dt_in.timestamp()) * 1000
    setup_db.post("/attendance/punch-in", json={"emp_code": "EMP0004", "punched_at": ts_in})

    # Changing nothing -> 422
    res_nochange = setup_db.patch("/attendance/EMP0004/2026-07-06", json={"reason": "Testing no change", "regularized_by": "hr"})
    assert res_nochange.status_code == 422

    # Setting status LEAVE while specifying punch_in -> 422
    res_leave_pi = setup_db.patch(
        "/attendance/EMP0004/2026-07-06",
        json={"status": "LEAVE", "punch_in": ts_in, "reason": "Setting leave", "regularized_by": "hr"}
    )
    assert res_leave_pi.status_code == 422

    # Regularize punch_in to 09:28 IST
    dt_new = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    ts_new = int(dt_new.timestamp()) * 1000

    res_patch = setup_db.patch(
        "/attendance/EMP0004/2026-07-06",
        json={
            "punch_in": ts_new,
            "reason": "Correcting biometric glitch",
            "regularized_by": "hr.admin"
        }
    )
    assert res_patch.status_code == 200, res_patch.text
    p_data = res_patch.json()
    assert p_data["late_minutes"] == 0
    assert len(p_data["history"]) == 1
    assert p_data["history"][0]["by"] == "hr.admin"

def test_leaderboard_ties_and_limit(setup_db):
    # EMP10 and EMP11 tied on 30 min late
    for emp_code, name in [("EMP0010", "E10"), ("EMP0011", "E11"), ("EMP0012", "E12")]:
        setup_db.post("/employees", json={
            "emp_code": emp_code,
            "name": name,
            "email": f"{emp_code}@example.com",
            "department": "QA",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })

    dt_in = datetime(2026, 7, 6, 4, 30, 0, tzinfo=timezone.utc) # 30 min late
    ts_in = int(dt_in.timestamp()) * 1000
    setup_db.post("/attendance/punch-in", json={"emp_code": "EMP0010", "punched_at": ts_in})
    setup_db.post("/attendance/punch-in", json={"emp_code": "EMP0011", "punched_at": ts_in})

    # With limit=1, since EMP10 and EMP11 tie for rank 1, both should be returned (2 items total)
    res_l = setup_db.get("/analytics/leaderboard/late?month=2026-07&limit=1")
    assert res_l.status_code == 200, res_l.text
    items = res_l.json()["items"]
    assert len(items) == 2
    assert items[0]["rank"] == 1
    assert items[1]["rank"] == 1
    assert items[0]["emp_code"] == "EMP0010"
    assert items[1]["emp_code"] == "EMP0011"

def test_concurrency_punch_in(setup_db):
    setup_db.post("/employees", json={
        "emp_code": "EMP0099",
        "name": "Race Tester",
        "email": "race@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    dt_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    ts_in = int(dt_in.timestamp()) * 1000

    def do_punch_in():
        c = TestClient(app)
        return c.post("/attendance/punch-in", json={"emp_code": "EMP0099", "punched_at": ts_in})

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch_in) for _ in range(5)]
        results = [f.result() for f in futures]

    status_codes = [r.status_code for r in results]
    assert status_codes.count(201) == 1
    assert status_codes.count(409) == 4

def test_explain_endpoint(setup_db):
    res_e = setup_db.get("/admin/explain/attendance_list")
    assert res_e.status_code == 200, res_e.text
    data = res_e.json()
    assert "explain" in data
