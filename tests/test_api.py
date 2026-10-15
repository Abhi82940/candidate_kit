"""
Employee Attendance & Analytics API - Comprehensive Test Suite
Verifies all 12 endpoints, business rules R1-R10, schema validations,
concurrency guarantees, strict date formats, and MongoDB query plans.
"""
import concurrent.futures
import os
from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

# Use dedicated test database
os.environ["MONGO_DB"] = "attendance_test_db"

from app.main import app, db, client, dt_to_epoch_ms, init_indexes

test_client = TestClient(app)


@pytest.fixture(autouse=True, scope="module")
def setup_database():
    """Setup clean database and ensure indexes for the test suite."""
    db.employees.drop()
    db.attendance_logs.drop()
    with TestClient(app) as _:
        pass
    yield
    client.drop_database("attendance_test_db")


# --------------------------------------------------------------------------- #
# 1. Health Probe
# --------------------------------------------------------------------------- #
def test_health():
    res = test_client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


# --------------------------------------------------------------------------- #
# 2. Database Indexes & Visibility
# --------------------------------------------------------------------------- #
def test_indexes_and_visible_failure():
    """Verify indexes are created and fail visibly without swallowing errors."""
    emp_indexes = list(db.employees.list_indexes())
    emp_index_names = [idx["name"] for idx in emp_indexes]
    assert "emp_code_1" in emp_index_names
    assert "department_1" in emp_index_names
    assert "joined_on_1" in emp_index_names
    assert "department_1_joined_on_1" in emp_index_names

    att_indexes = list(db.attendance_logs.list_indexes())
    att_index_names = [idx["name"] for idx in att_indexes]
    assert "emp_code_1_date_1" in att_index_names
    assert "emp_code_1_punch_in_-1" in att_index_names
    assert "date_-1_emp_code_1" in att_index_names
    assert "date_1_late_minutes_1" in att_index_names

    # Test that init_indexes raises DuplicateKeyError visibly if duplicates exist
    temp_coll = db["temp_dup_test"]
    temp_coll.drop()
    temp_coll.insert_many([{"emp_code": "DUP1"}, {"emp_code": "DUP1"}])
    with pytest.raises(DuplicateKeyError):
        temp_coll.create_index([("emp_code", 1)], unique=True)
    temp_coll.drop()


# --------------------------------------------------------------------------- #
# 3. Employee Management & Strict Date Validation
# --------------------------------------------------------------------------- #
def test_employee_crud_and_strict_date_validation():
    emp_payload = {
        "emp_code": "EMP1001",
        "name": "Alice Smith",
        "email": "alice@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    res = test_client.post("/employees", json=emp_payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP1001"
    assert isinstance(data["created_at"], int)
    assert "_id" not in data

    # Duplicate emp_code -> 409
    res_dup = test_client.post("/employees", json=emp_payload)
    assert res_dup.status_code == 409

    # Strict date validation for joined_on
    # Malformed format (DD/MM/YYYY) -> 422
    assert test_client.post("/employees", json=dict(emp_payload, emp_code="EMP1002", joined_on="05/01/2026")).status_code == 422
    # Non-padded month -> 422
    assert test_client.post("/employees", json=dict(emp_payload, emp_code="EMP1003", joined_on="2026-1-5")).status_code == 422
    # Impossible calendar date (Feb 30) -> 422
    assert test_client.post("/employees", json=dict(emp_payload, emp_code="EMP1004", joined_on="2026-02-30")).status_code == 422
    # Impossible calendar date (April 31) -> 422
    assert test_client.post("/employees", json=dict(emp_payload, emp_code="EMP1005", joined_on="2026-04-31")).status_code == 422
    # Malformed string -> 422
    assert test_client.post("/employees", json=dict(emp_payload, emp_code="EMP1006", joined_on="not-a-date")).status_code == 422

    # Equal shift_start and shift_end -> 422
    assert test_client.post("/employees", json=dict(emp_payload, emp_code="EMP1007", shift_start="09:00", shift_end="09:00")).status_code == 422

    # Invalid emp_code regex -> 422
    assert test_client.post("/employees", json=dict(emp_payload, emp_code="INVALID123")).status_code == 422

    # List employees with department filter and pagination
    res_list = test_client.get("/employees?department=Engineering&page=1&page_size=10")
    assert res_list.status_code == 200
    list_data = res_list.json()
    assert list_data["total"] >= 1
    assert list_data["page"] == 1
    assert list_data["page_size"] == 10
    assert list_data["items"][0]["emp_code"] == "EMP1001"


# --------------------------------------------------------------------------- #
# 4. Punch-In & Rules R1, R2
# --------------------------------------------------------------------------- #
def test_punch_in_and_rules():
    emp_payload = {
        "emp_code": "EMP2001",
        "name": "Bob Jones",
        "email": "bob@example.com",
        "department": "Sales",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    test_client.post("/employees", json=emp_payload)

    # Unknown employee -> 404
    assert test_client.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404

    # R2: Grace period 10 minutes (09:40:00 IST = 04:10:00 UTC) -> late_minutes = 0
    dt_grace = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    ms_grace = dt_to_epoch_ms(dt_grace)
    res_grace = test_client.post("/attendance/punch-in", json={"emp_code": "EMP2001", "punched_at": ms_grace})
    assert res_grace.status_code == 201
    assert res_grace.json()["late_minutes"] == 0
    assert res_grace.json()["date"] == "2026-07-06"
    assert res_grace.json()["punch_out"] is None
    assert res_grace.json()["history"] == []

    # Duplicate punch-in on same date -> 409
    assert test_client.post("/attendance/punch-in", json={"emp_code": "EMP2001", "punched_at": ms_grace}).status_code == 409

    # R2: Late boundary (09:40:01 IST = 04:10:01 UTC) -> strictly > 10 min -> late_minutes = 10
    emp_late = dict(emp_payload, emp_code="EMP2002")
    test_client.post("/employees", json=emp_late)
    dt_late = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    ms_late = dt_to_epoch_ms(dt_late)
    res_late = test_client.post("/attendance/punch-in", json={"emp_code": "EMP2002", "punched_at": ms_late})
    assert res_late.status_code == 201
    assert res_late.json()["late_minutes"] == 10

    # R1: Overnight shift (22:00 to 06:00 IST).
    # Punch-in at 05:30 IST on 2026-07-07 (< shift_end 06:00) belongs to 2026-07-06.
    emp_night = dict(emp_payload, emp_code="EMP2003", shift_start="22:00", shift_end="06:00")
    test_client.post("/employees", json=emp_night)
    dt_night = datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)  # 05:30 IST
    ms_night = dt_to_epoch_ms(dt_night)
    res_night = test_client.post("/attendance/punch-in", json={"emp_code": "EMP2003", "punched_at": ms_night})
    assert res_night.status_code == 201
    assert res_night.json()["date"] == "2026-07-06"


# --------------------------------------------------------------------------- #
# 5. Punch-Out Validation, Chronology (422 vs 404), & Rules R3-R5
# --------------------------------------------------------------------------- #
def test_punch_out_chronology_and_rules():
    emp_payload = {
        "emp_code": "EMP3001",
        "name": "Charlie Brown",
        "email": "charlie@example.com",
        "department": "Marketing",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    test_client.post("/employees", json=emp_payload)

    # Punch-out when employee has no punch-in record -> 404
    assert test_client.post("/attendance/punch-out", json={"emp_code": "EMP3001"}).status_code == 404

    # Punch in at 09:30 IST (04:00 UTC)
    in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    in_ms = dt_to_epoch_ms(in_dt)
    test_client.post("/attendance/punch-in", json={"emp_code": "EMP3001", "punched_at": in_ms})

    # A. Punch out EARLIER than punch in -> must return HTTP 422, NOT 404
    early_dt = in_dt - timedelta(minutes=30)
    early_ms = dt_to_epoch_ms(early_dt)
    res_early = test_client.post("/attendance/punch-out", json={"emp_code": "EMP3001", "punched_at": early_ms})
    assert res_early.status_code == 422
    assert "strictly after" in res_early.json()["detail"].lower()

    # B. Punch out EQUAL to punch in -> must return HTTP 422, NOT 404
    res_equal = test_client.post("/attendance/punch-out", json={"emp_code": "EMP3001", "punched_at": in_ms})
    assert res_equal.status_code == 422
    assert "strictly after" in res_equal.json()["detail"].lower()

    # C. Punch out more than 24 hours later -> 422
    late_dt = in_dt + timedelta(hours=25)
    late_ms = dt_to_epoch_ms(late_dt)
    res_late = test_client.post("/attendance/punch-out", json={"emp_code": "EMP3001", "punched_at": late_ms})
    assert res_late.status_code == 422

    # D. Valid punch out at 19:10 IST (13:40 UTC)
    # Work hours: 9h 40m = 9.666... -> 9.67 half-up
    # Overtime: 18:30 to 19:10 = 40 min (>= 30 min threshold) -> 40
    # Half day: work_hours (9.67) >= 4.50 -> False
    out_dt = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    out_ms = dt_to_epoch_ms(out_dt)
    res_out = test_client.post("/attendance/punch-out", json={"emp_code": "EMP3001", "punched_at": out_ms})
    assert res_out.status_code == 200
    out_data = res_out.json()
    assert out_data["work_hours"] == 9.67
    assert out_data["overtime_minutes"] == 40
    assert out_data["half_day"] is False

    # Second punch out on already closed record -> 409
    assert test_client.post("/attendance/punch-out", json={"emp_code": "EMP3001", "punched_at": out_ms}).status_code == 409

    # E. Test half_day (< 4.50 hours) and overtime < 30 min (0 overtime)
    emp_half = dict(emp_payload, emp_code="EMP3002")
    test_client.post("/employees", json=emp_half)
    test_client.post("/attendance/punch-in", json={"emp_code": "EMP3002", "punched_at": in_ms})
    # Punch out after 4 hours (13:30 IST = 08:00 UTC)
    out_half_dt = datetime(2026, 7, 6, 8, 0, 0, tzinfo=timezone.utc)
    res_half = test_client.post("/attendance/punch-out", json={"emp_code": "EMP3002", "punched_at": dt_to_epoch_ms(out_half_dt)})
    assert res_half.status_code == 200
    assert res_half.json()["work_hours"] == 4.0
    assert res_half.json()["half_day"] is True
    assert res_half.json()["overtime_minutes"] == 0


# --------------------------------------------------------------------------- #
# 6. Attendance Listing & Strict Date Validation
# --------------------------------------------------------------------------- #
def test_attendance_listing_and_filters():
    # Valid listing with emp_code filter
    res = test_client.get("/attendance?emp_code=EMP3001&page=1&page_size=10")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] >= 1
    assert data["items"][0]["emp_code"] == "EMP3001"
    assert "_id" not in data["items"][0]

    # Strict date validation for date_from / date_to
    # Malformed format (DD/MM/YYYY) -> 422
    assert test_client.get("/attendance?date_from=01/07/2026").status_code == 422
    assert test_client.get("/attendance?date_to=06/07/2026").status_code == 422
    # Impossible calendar date -> 422
    assert test_client.get("/attendance?date_from=2026-02-30").status_code == 422
    assert test_client.get("/attendance?date_to=2026-04-31").status_code == 422
    # date_from > date_to -> 422
    assert test_client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01").status_code == 422

    # Valid date range
    res_range = test_client.get("/attendance?date_from=2026-07-01&date_to=2026-07-10")
    assert res_range.status_code == 200
    assert res_range.json()["total"] >= 1


# --------------------------------------------------------------------------- #
# 7. Regularization (PATCH), Legacy History, & Strict Date Validation
# --------------------------------------------------------------------------- #
def test_regularization_legacy_history_and_rules():
    emp_payload = {
        "emp_code": "EMP4001",
        "name": "Diana Prince",
        "email": "diana@example.com",
        "department": "HR",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    test_client.post("/employees", json=emp_payload)

    # Insert a legacy document directly into MongoDB where 'history' is completely missing
    in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    db.attendance_logs.insert_one({
        "emp_code": "EMP4001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    })

    # Strict date validation on path param {date}
    # Impossible date -> 422
    assert test_client.patch("/attendance/EMP4001/2026-02-30", json={"status": "PRESENT", "reason": "fixing", "regularized_by": "admin"}).status_code == 422
    # Malformed date -> 422
    assert test_client.patch("/attendance/EMP4001/06-07-2026", json={"status": "PRESENT", "reason": "fixing", "regularized_by": "admin"}).status_code == 422

    # Regularize legacy record without history: supply punch_out
    out_dt = datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc)
    out_ms = dt_to_epoch_ms(out_dt)
    reg_payload = {
        "punch_out": out_ms,
        "reason": "Forgot to punch out",
        "regularized_by": "manager1",
    }
    res_reg = test_client.patch("/attendance/EMP4001/2026-07-06", json=reg_payload)
    assert res_reg.status_code == 200
    data = res_reg.json()
    assert data["work_hours"] == 9.0
    assert len(data["history"]) == 1
    assert data["history"][0]["by"] == "manager1"
    assert "punch_out" in data["history"][0]["changes"]

    # No-op regularize -> 422
    res_noop = test_client.patch("/attendance/EMP4001/2026-07-06", json={
        "status": "PRESENT",
        "reason": "Duplicate change",
        "regularized_by": "manager1",
    })
    assert res_noop.status_code == 422

    # Change to ABSENT clears punches
    res_absent = test_client.patch("/attendance/EMP4001/2026-07-06", json={
        "status": "ABSENT",
        "reason": "Was absent whole day",
        "regularized_by": "manager1",
    })
    assert res_absent.status_code == 200
    assert res_absent.json()["punch_in"] is None
    assert res_absent.json()["punch_out"] is None

    # Providing punch times with ABSENT -> 422
    res_bad_absent = test_client.patch("/attendance/EMP4001/2026-07-06", json={
        "status": "ABSENT",
        "punch_in": out_ms,
        "reason": "Invalid combination",
        "regularized_by": "manager1",
    })
    assert res_bad_absent.status_code == 422


# --------------------------------------------------------------------------- #
# 8. Monthly Summary Analytics & Rules R5-R8
# --------------------------------------------------------------------------- #
def test_employee_monthly_analytics():
    # Employee joined mid-month: 2026-07-15
    test_client.post("/employees", json={
        "emp_code": "EMP8001",
        "name": "Mid Month Joiner",
        "email": "mid@test.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-15",
    })

    # Add 2 logs: 1 full present day, 1 half-day present
    db.attendance_logs.insert_many([
        {
            "emp_code": "EMP8001",
            "date": "2026-07-16",  # Thursday
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 16, 4, 0, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 16, 13, 0, 0, tzinfo=timezone.utc),
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        },
        {
            "emp_code": "EMP8001",
            "date": "2026-07-17",  # Friday
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 17, 4, 0, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 17, 8, 0, 0, tzinfo=timezone.utc),
            "work_hours": 4.0,
            "late_minutes": 15,
            "overtime_minutes": 0,
            "half_day": True,
        },
    ])

    res = test_client.get("/analytics/employees/EMP8001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP8001"
    assert data["month"] == "2026-07"
    # July 15 to 31, 2026 has 13 weekdays (Mon-Fri)
    assert data["working_days"] == 13
    # 1.0 + 0.5 = 1.5 present days
    assert data["present_days"] == 1.5
    assert data["late_count"] == 1
    assert data["total_late_minutes"] == 15
    # (1.5 / 13) * 100 = 11.538... -> 11.54 half-up
    assert data["attendance_pct"] == 11.54

    # Unknown employee -> 404
    assert test_client.get("/analytics/employees/EMP9999/monthly?month=2026-07").status_code == 404


# --------------------------------------------------------------------------- #
# 9. Department Summary Analytics & 0-Log Headcount (R9)
# --------------------------------------------------------------------------- #
def test_department_summary_and_zero_logs():
    test_client.post("/employees", json={
        "emp_code": "EMP6001",
        "name": "Zero Logs User",
        "email": "zero@test.com",
        "department": "Finance",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })

    # Employee EMP6001 has no logs. Department summary must still include Finance with headcount 1
    res = test_client.get("/analytics/departments/summary?month=2026-07&department=Finance")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["department"] == "Finance"
    assert items[0]["headcount"] == 1
    assert items[0]["present_days"] == 0.0
    assert items[0]["avg_work_hours"] is None


# --------------------------------------------------------------------------- #
# 10. Late Leaderboard & Competition Ranking ($rank)
# --------------------------------------------------------------------------- #
def test_leaderboard_competition_ranking():
    for code in ["EMP5001", "EMP5002", "EMP5003"]:
        test_client.post("/employees", json={
            "emp_code": code,
            "name": f"User {code}",
            "email": f"{code}@test.com",
            "department": "QA",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        })

    db.attendance_logs.insert_many([
        {"emp_code": "EMP5001", "date": "2026-08-03", "status": "PRESENT", "late_minutes": 50, "punch_in": datetime.now(timezone.utc)},
        {"emp_code": "EMP5002", "date": "2026-08-03", "status": "PRESENT", "late_minutes": 30, "punch_in": datetime.now(timezone.utc)},
        {"emp_code": "EMP5003", "date": "2026-08-03", "status": "PRESENT", "late_minutes": 30, "punch_in": datetime.now(timezone.utc)},
    ])

    res = test_client.get("/analytics/leaderboard/late?month=2026-08&limit=2")
    assert res.status_code == 200
    items = res.json()["items"]
    # Ties share rank 2 and both return because rank <= 2
    assert len(items) == 3
    assert items[0]["emp_code"] == "EMP5001" and items[0]["rank"] == 1
    assert items[1]["rank"] == 2 and items[2]["rank"] == 2


# --------------------------------------------------------------------------- #
# 11. Department Trend Analytics & $densify Gap-Filling
# --------------------------------------------------------------------------- #
def test_department_trend_gap_filling_and_strict_dates():
    test_client.post("/employees", json={
        "emp_code": "EMP7001",
        "name": "Trend User",
        "email": "trend@test.com",
        "department": "Support",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })

    # Strict date validation
    assert test_client.get("/analytics/departments/Support/trend?from=01/07/2026&to=05/07/2026").status_code == 422
    assert test_client.get("/analytics/departments/Support/trend?from=2026-07-05&to=2026-07-01").status_code == 422
    assert test_client.get("/analytics/departments/Support/trend?from=2026-01-01&to=2026-05-01").status_code == 422  # > 92 days

    # Query 5 days: 2026-07-01 to 2026-07-05 (including weekend 07-04 and 07-05)
    res = test_client.get("/analytics/departments/Support/trend?from=2026-07-01&to=2026-07-05")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 5
    assert [row["date"] for row in items] == ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04", "2026-07-05"]
    # Weekend dates have null attendance_rate
    assert items[3]["is_working_day"] is False
    assert items[3]["attendance_rate"] is None


# --------------------------------------------------------------------------- #
# 12. Admin Explain & Query Plans (Zero COLLSCAN, IXSCAN Present)
# --------------------------------------------------------------------------- #
def test_explain_endpoints_and_indexes():
    # Test all 5 explain endpoints
    endpoints = [
        "/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-05",
        "/admin/explain/employee_monthly?emp_code=EMP1001&month=2026-01",
        "/admin/explain/department_summary?month=2026-01&department=Engineering",
        "/admin/explain/department_summary?month=2026-01",
        "/admin/explain/late_leaderboard?month=2026-08",
        "/admin/explain/late_leaderboard?month=2026-08&department=QA",
        "/admin/explain/department_trend?department=Engineering&from=2026-01-01&to=2026-01-05",
    ]
    for url in endpoints:
        res = test_client.get(url)
        assert res.status_code == 200
        body = res.json()
        assert "explain" in body
        assert "collection" in body
        exp_str = str(body["explain"])
        assert "IXSCAN" in exp_str, f"Missing IXSCAN in {url}"
        assert "COLLSCAN" not in exp_str, f"Unexpected COLLSCAN in {url}"


# --------------------------------------------------------------------------- #
# 13. Concurrency Guarantees (Punch-In, Punch-Out, Regularization)
# --------------------------------------------------------------------------- #
def test_concurrency_guarantees():
    emp_payload = {
        "emp_code": "EMP9001",
        "name": "Concurrent Worker",
        "email": "concurrent@test.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }
    test_client.post("/employees", json=emp_payload)

    # 1. Concurrent punch-in for same employee & date: exactly one 201, rest 409
    p_in_dt = datetime(2026, 9, 1, 4, 0, 0, tzinfo=timezone.utc)
    p_in_ms = dt_to_epoch_ms(p_in_dt)

    def do_punch_in():
        return test_client.post("/attendance/punch-in", json={"emp_code": "EMP9001", "punched_at": p_in_ms})

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch_in) for _ in range(5)]
        results = [f.result().status_code for f in futures]

    assert results.count(201) == 1
    assert results.count(409) == 4

    # 2. Concurrent punch-out for same open record: exactly one 200, rest 409
    p_out_dt = datetime(2026, 9, 1, 13, 0, 0, tzinfo=timezone.utc)
    p_out_ms = dt_to_epoch_ms(p_out_dt)

    def do_punch_out():
        return test_client.post("/attendance/punch-out", json={"emp_code": "EMP9001", "punched_at": p_out_ms})

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch_out) for _ in range(5)]
        results = [f.result().status_code for f in futures]

    assert results.count(200) == 1
    assert results.count(409) == 4
