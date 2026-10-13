"""
Employee Attendance & Analytics API - Fully Implemented
"""
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import ipaddress
import json
import math
import os
import re
from typing import Any, Dict, List, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator, model_validator
import pymongo
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or "mongodb://localhost:27017"
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

IST = timezone(timedelta(hours=5, minutes=30))

VALID_ALL_STATUSES = frozenset({"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"})
VALID_PUNCH_STATUSES = frozenset({"PRESENT", "WFH", "ON_DUTY"})
DERIVED_ATTENDANCE_FIELDS = frozenset({"work_hours", "late_minutes", "overtime_minutes", "half_day"})

def is_local_host(host_str: str) -> bool:
    """Return True only for loopback hosts and explicit local Docker hostnames."""
    host = host_str.lower().strip().strip("[]")
    if host in {"localhost", "host.docker.internal"}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def verify_test_database_safety(mongo_uri: str, db_name: str) -> dict:
    """
    Validate a MongoDB URI and database name before opening a destructive client.
    """
    if not isinstance(db_name, str) or not (
        db_name.lower() == "test" or db_name.lower().startswith("test_")
    ):
        raise ValueError(
            f"Safety check failed: Database name '{db_name}' is not an approved disposable test database."
        )

    if not isinstance(mongo_uri, str) or mongo_uri.lower().startswith("mongodb+srv://"):
        raise ValueError("Safety check failed: only explicit local mongodb:// URIs are allowed.")
    try:
        parsed_uri = pymongo.uri_parser.parse_uri(mongo_uri)
    except (ValueError, pymongo.errors.ConfigurationError) as exc:
        raise ValueError("Safety check failed: MongoDB URI is invalid.") from exc

    nodes = parsed_uri.get("nodelist", [])
    if not nodes or any(not is_local_host(host) for host, _ in nodes):
        raise ValueError("Safety check failed: only local MongoDB hosts are allowed.")

    return {"host_category": "LOCAL_DISPOSABLE", "database": db_name}


if os.getenv("TEST_MODE") == "1":
    verify_test_database_safety(MONGO_URI, MONGO_DB)

client = MongoClient(MONGO_URI, connect=False)
db = client[MONGO_DB]


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1), ("emp_code", 1)])
    db.employees.create_index([("joined_on", 1)])

    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1), ("status", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers & Rounding
# --------------------------------------------------------------------------- #
def round_half_up(val: float, decimals: int = 2) -> float:
    d = Decimal(str(val))
    q = Decimal('1') if decimals == 0 else Decimal('1e-' + str(decimals))
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def clean_bson(obj: Any) -> Any:
    return json.loads(json_util.dumps(obj))


def validate_raw_epoch_ms(v: Any) -> Any:
    if v is not None:
        if type(v) is not int:
            raise ValueError("Epoch milliseconds must be an integer, not string or float")
        if not (100000000000 <= v <= 4102444800000):
            raise ValueError("Timestamp out of range")
    return v


def ignore_derived_attendance_fields(data: Any) -> Any:
    if isinstance(data, dict):
        return {key: value for key, value in data.items() if key not in DERIVED_ATTENDANCE_FIELDS}
    return data


def validate_date_str(s: Optional[str], field_name: str = "date") -> None:
    """Raise HTTP 422 if s is not a valid calendar date (e.g. 2026-02-30)."""
    if s is None:
        return
    if not isinstance(s, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        raise HTTPException(422, f"Invalid {field_name}: must be formatted as YYYY-MM-DD")
    try:
        date.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, f"Invalid {field_name}: '{s}' is not a real calendar date")


def ms_to_datetime_utc(ms: int) -> datetime:
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def datetime_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def truncate_to_seconds(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    sec = int(dt.timestamp())
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def parse_time(t_str: str) -> time:
    h, m = map(int, t_str.split(":"))
    return time(h, m)


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    sh = parse_time(shift_start)
    eh = parse_time(shift_end)
    return (eh.hour < sh.hour) or (eh.hour == sh.hour and eh.minute <= sh.minute)


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    punch_in_ist = punch_in_utc.astimezone(IST)
    if is_overnight_shift(shift_start, shift_end):
        eh = parse_time(shift_end)
        if punch_in_ist.time() < eh:
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, date_str: str) -> int:
    start_dt_ist = datetime.fromisoformat(f"{date_str}T{shift_start}:00").replace(tzinfo=IST)
    punch_in_ist = punch_in_utc.astimezone(IST)

    diff_seconds = (punch_in_ist - start_dt_ist).total_seconds()
    if diff_seconds > 600:
        return math.floor(diff_seconds / 60.0)
    return 0


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    eh = parse_time(shift_end)
    start_date = date.fromisoformat(date_str)
    if is_overnight_shift(shift_start, shift_end):
        end_date = start_date + timedelta(days=1)
    else:
        end_date = start_date

    end_dt_ist = datetime.combine(end_date, eh).replace(tzinfo=IST)
    punch_out_ist = punch_out_utc.astimezone(IST)

    diff_seconds = (punch_out_ist - end_dt_ist).total_seconds()
    if diff_seconds >= 1800:
        return math.floor(diff_seconds / 60.0)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    seconds = (punch_out_utc - punch_in_utc).total_seconds()
    hours = seconds / 3600.0
    return round_half_up(hours, 2)


def get_month_range(month_str: str):
    if not isinstance(month_str, str) or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month_str):
        raise HTTPException(422, "Invalid month format. Must be YYYY-MM")
    try:
        y, m = map(int, month_str.split("-"))
        start_date = date(y, m, 1)
        if m == 12:
            end_date = date(y + 1, 1, 1) - timedelta(days=1)
        else:
            end_date = date(y, m + 1, 1) - timedelta(days=1)
        return start_date, end_date
    except Exception:
        raise HTTPException(422, "Invalid month format")


def count_working_days(start_date: date, end_date: date, joined_on_str: str) -> int:
    joined_date = date.fromisoformat(joined_on_str)
    eff_start = max(start_date, joined_date)
    if eff_start > end_date:
        return 0

    count = 0
    curr = eff_start
    while curr <= end_date:
        if curr.weekday() < 5:
            count += 1
        curr += timedelta(days=1)
    return count


def format_employee_doc(doc: dict) -> dict:
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        created_at_ms = datetime_to_ms(created_at)
    else:
        created_at_ms = created_at

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": created_at_ms,
    }


def format_attendance_doc(doc: dict) -> dict:
    punch_in = doc.get("punch_in")
    punch_in_ms = datetime_to_ms(punch_in) if isinstance(punch_in, datetime) else punch_in

    punch_out = doc.get("punch_out")
    punch_out_ms = datetime_to_ms(punch_out) if isinstance(punch_out, datetime) else punch_out

    formatted_history = []
    for h in doc.get("history", []) or []:
        at_val = h.get("at")
        at_ms = datetime_to_ms(at_val) if isinstance(at_val, datetime) else at_val

        formatted_changes = {}
        for k, v in (h.get("changes") or {}).items():
            fr = v.get("from")
            to = v.get("to")
            if k in ("punch_in", "punch_out"):
                fr = datetime_to_ms(fr) if isinstance(fr, datetime) else fr
                to = datetime_to_ms(to) if isinstance(to, datetime) else to
            formatted_changes[k] = {"from": fr, "to": to}

        formatted_history.append({
            "at": at_ms,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": formatted_changes
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": punch_in_ms,
        "punch_out": punch_out_ms,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": formatted_history
    }


# --------------------------------------------------------------------------- #
# Analytics Pipeline Builders
# Shared between real endpoints and the explain endpoint so both always use
# the identical query/aggregation — no simplified substitutes.
# --------------------------------------------------------------------------- #

def _build_employee_monthly_pipeline(emp_code: str, s_str: str, e_str: str) -> list:
    return [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": s_str, "$lte": e_str}
        }},
        {"$addFields": {
            "dt": {"$dateFromString": {"dateString": "$date"}},
            "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
        }},
        {"$addFields": {
            "day_of_week": {"$isoDayOfWeek": "$dt"}
        }},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": ["$is_presence", {"$lte": ["$day_of_week", 5]}]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                        0.0
                    ]
                }
            },
            "leave_days": {
                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
            },
            "total_late_minutes": {
                "$sum": {"$ifNull": ["$late_minutes", 0]}
            },
            "total_overtime_minutes": {
                "$sum": {"$ifNull": ["$overtime_minutes", 0]}
            }
        }}
    ]


def _build_department_summary_pipeline(match_emp: dict, s_str: str, e_str: str) -> list:
    return [
        {"$match": match_emp},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$emp_code", "$$emp"]},
                            {"$gte": ["$date", s_str]},
                            {"$lte": ["$date", e_str]}
                        ]
                    }
                }},
                {"$addFields": {
                    "dt": {"$dateFromString": {"dateString": "$date"}},
                    "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
                }},
                {"$addFields": {
                    "day_of_week": {"$isoDayOfWeek": "$dt"}
                }}
            ],
            "as": "logs"
        }},
        {"$unwind": {
            "path": "$logs",
            "preserveNullAndEmptyArrays": True
        }},
        {"$group": {
            "_id": {
                "dept": "$department",
                "emp": "$emp_code"
            },
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$ne": ["$logs", None]},
                            "$logs.is_presence",
                            {"$lte": ["$logs.day_of_week", 5]}
                        ]},
                        {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                        0.0
                    ]
                }
            },
            "work_hours_records": {
                "$push": {
                    "$cond": [
                        {"$and": [
                            {"$ne": ["$logs", None]},
                            "$logs.is_presence",
                            {"$ne": ["$logs.work_hours", None]}
                        ]},
                        "$logs.work_hours",
                        "$$REMOVE"
                    ]
                }
            },
            "late_count": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$ne": ["$logs", None]},
                            {"$gt": ["$logs.late_minutes", 0]}
                        ]},
                        1,
                        0
                    ]
                }
            },
            "total_late_minutes": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$ne": ["$logs", None]},
                            {"$gt": ["$logs.late_minutes", 0]}
                        ]},
                        "$logs.late_minutes",
                        0
                    ]
                }
            },
            "leave_count": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$ne": ["$logs", None]},
                            {"$eq": ["$logs.status", "LEAVE"]}
                        ]},
                        1,
                        0
                    ]
                }
            },
            "on_duty_count": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$ne": ["$logs", None]},
                            {"$eq": ["$logs.status", "ON_DUTY"]}
                        ]},
                        1,
                        0
                    ]
                }
            }
        }},
        {"$group": {
            "_id": "$_id.dept",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "all_work_hours": {"$push": "$work_hours_records"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"}
        }},
        {"$project": {
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours_raw": {
                "$avg": {
                    "$reduce": {
                        "input": "$all_work_hours",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]}
                    }
                }
            },
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1
        }},
        {"$sort": {"department": 1}}
    ]


def _build_late_leaderboard_pipeline(s_str: str, e_str: str, department: Optional[str], limit: int) -> list:
    pipeline = [
        {"$match": {
            "date": {"$gte": s_str, "$lte": e_str},
            "late_minutes": {"$gt": 0}
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"}
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$project": {
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1
        }},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}}
            }
        }},
        {"$match": {
            "rank": {"$lte": limit}
        }},
        {"$sort": {
            "total_late_minutes": -1,
            "emp_code": 1
        }}
    ])
    return pipeline


def _build_department_trend_pipeline(department: str, d_from: date, d_to: date) -> list:
    num_days = (d_to - d_from).days
    start_utc = datetime.combine(d_from, time.min, tzinfo=timezone.utc)

    return [
        {"$match": {"department": department}},
        {"$group": {
            "_id": "$department",
            "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}}
        }},
        {"$project": {
            "department": "$_id",
            "employees": 1,
            "days": {
                "$map": {
                    "input": {"$range": [0, num_days + 1]},
                    "as": "offset",
                    "in": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": {
                                "$dateAdd": {
                                    "startDate": start_utc,
                                    "unit": "day",
                                    "amount": "$$offset"
                                }
                            },
                            "timezone": "+05:30"
                        }
                    }
                }
            }
        }},
        {"$unwind": "$days"},
        {"$project": {
            "department": 1,
            "date": "$days",
            "active_employees": {
                "$filter": {
                    "input": "$employees",
                    "as": "e",
                    "cond": {"$lte": ["$$e.joined_on", "$days"]}
                }
            }
        }},
        {"$project": {
            "department": 1,
            "date": 1,
            "headcount": {"$size": "$active_employees"},
            "emp_codes": {
                "$map": {
                    "input": "$active_employees",
                    "as": "e",
                    "in": "$$e.emp_code"
                }
            }
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"curr_date": "$date", "dept_emps": "$emp_codes"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$date", "$$curr_date"]},
                            {"$in": ["$emp_code", "$$dept_emps"]}
                        ]
                    }
                }}
            ],
            "as": "logs"
        }},
        {"$project": {
            "date": 1,
            "dt": {"$dateFromString": {"dateString": "$date"}},
            "headcount": 1,
            "present_count": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "l",
                        "in": {
                            "$cond": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                0.0
                            ]
                        }
                    }
                }
            },
            "late_count": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "l",
                        "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]}
                    }
                }
            }
        }},
        {"$addFields": {
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$dt"}, 5]}
        }},
        {"$addFields": {
            "attendance_rate_raw": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$divide": ["$present_count", "$headcount"]},
                    None
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "moving_avg_7d_raw": {
                    "$avg": "$attendance_rate_raw",
                    "window": {
                        "documents": [-6, 0]
                    }
                }
            }
        }},
        {"$sort": {"date": 1}}
    ]


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    model_config = {"extra": "forbid"}

    @field_validator("joined_on")
    def validate_joined_on(cls, v):
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on is not a valid calendar date")
        return v

    @model_validator(mode="after")
    def validate_shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def check_explicit_null(cls, data: Any) -> Any:
        data = ignore_derived_attendance_fields(data)
        if isinstance(data, dict) and "punched_at" in data and data["punched_at"] is None:
            raise ValueError("punched_at cannot be null; omit the field to default to current server time")
        return data

    @field_validator("punched_at", mode="before")
    def validate_punched_at(cls, v):
        return validate_raw_epoch_ms(v)

    @field_validator("status")
    def validate_status(cls, v):
        if v not in VALID_PUNCH_STATUSES:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def check_explicit_null(cls, data: Any) -> Any:
        data = ignore_derived_attendance_fields(data)
        if isinstance(data, dict) and "punched_at" in data and data["punched_at"] is None:
            raise ValueError("punched_at cannot be null; omit the field to default to current server time")
        return data

    @field_validator("punched_at", mode="before")
    def validate_punched_at(cls, v):
        return validate_raw_epoch_ms(v)


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def check_explicit_null(cls, data: Any) -> Any:
        data = ignore_derived_attendance_fields(data)
        if isinstance(data, dict):
            if "status" in data and data["status"] is None:
                raise ValueError("status cannot be null; omit the field if not modifying")
            if "punch_in" in data and data["punch_in"] is None:
                raise ValueError("punch_in cannot be null; set status to ABSENT/LEAVE to clear punches or omit punch_in")
            if "punch_out" in data and data["punch_out"] is None:
                raise ValueError("punch_out cannot be null; omit punch_out if not modifying")
        return data

    @field_validator("punch_in", "punch_out", mode="before")
    def validate_punches(cls, v):
        return validate_raw_epoch_ms(v)

    @field_validator("status")
    def validate_status(cls, v):
        if v is not None and v not in VALID_ALL_STATUSES:
            raise ValueError("Invalid status")
        return v


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    now_utc = truncate_to_seconds(datetime.now(timezone.utc))
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    docs = list(db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size))
    items = [format_employee_doc(d) for d in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        ts_utc = ms_to_datetime_utc(body.punched_at)
    else:
        ts_utc = truncate_to_seconds(datetime.now(timezone.utc))

    d = get_attendance_date(ts_utc, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(ts_utc, emp["shift_start"], d)

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        ts_utc = ms_to_datetime_utc(body.punched_at)
    else:
        ts_utc = truncate_to_seconds(datetime.now(timezone.utc))

    doc = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts_utc}},
        sort=[("punch_in", -1)]
    )

    if not doc:
        raise HTTPException(404, "No punch-in found")

    if doc.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")

    punch_in_dt = doc["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    diff_sec = (ts_utc - punch_in_dt).total_seconds()
    if diff_sec <= 0 or diff_sec > 86400:
        raise HTTPException(422, "Invalid punch-out time")

    wh = compute_work_hours(punch_in_dt, ts_utc)
    ot = compute_overtime(ts_utc, emp["shift_start"], emp["shift_end"], doc["date"])
    hd = (wh < 4.50)

    cas_filter = {
        "_id": doc["_id"],
        "punch_out": None,
        "punch_in": doc.get("punch_in"),
        "status": doc.get("status"),
    }

    res = db.attendance_logs.find_one_and_update(
        cas_filter,
        {"$set": {
            "punch_out": ts_utc,
            "work_hours": wh,
            "overtime_minutes": ot,
            "half_day": hd
        }},
        return_document=pymongo.ReturnDocument.AFTER
    )

    if not res:
        raise HTTPException(409, "Already punched out or record modified concurrently")

    return format_attendance_doc(res)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # Validate status enum
    if status is not None and status not in VALID_ALL_STATUSES:
        raise HTTPException(422, f"Invalid status '{status}'. Must be one of: {sorted(VALID_ALL_STATUSES)}")

    # Validate real calendar dates (regex only checks format, not validity)
    validate_date_str(date_from, "date_from")
    validate_date_str(date_to, "date_to")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be greater than date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_doc(d) for d in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date_param: str = Path(..., alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeIn = ...
):
    # Validate the path date is a real calendar date
    validate_date_str(date_param, "date")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_param})
    if not doc:
        raise HTTPException(404, "Attendance record not found")

    if body.status is None and body.punch_in is None and body.punch_out is None:
        raise HTTPException(422, "No change specified")

    target_status = body.status if body.status is not None else doc["status"]

    if body.punch_in is not None:
        target_punch_in = ms_to_datetime_utc(body.punch_in)
    else:
        target_punch_in = doc.get("punch_in")
        if isinstance(target_punch_in, datetime) and target_punch_in.tzinfo is None:
            target_punch_in = target_punch_in.replace(tzinfo=timezone.utc)

    if body.punch_out is not None:
        target_punch_out = ms_to_datetime_utc(body.punch_out)
    else:
        target_punch_out = doc.get("punch_out")
        if isinstance(target_punch_out, datetime) and target_punch_out.tzinfo is None:
            target_punch_out = target_punch_out.replace(tzinfo=timezone.utc)

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "Cannot specify punch times for ABSENT or LEAVE status")
        target_punch_in = None
        target_punch_out = None
    else:
        if target_punch_in is None:
            raise HTTPException(422, "Presence status requires punch_in")

        computed_date = get_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if computed_date != date_param:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")

        if target_punch_out is not None:
            diff_sec = (target_punch_out - target_punch_in).total_seconds()
            if diff_sec <= 0 or diff_sec > 86400:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if target_status in ("ABSENT", "LEAVE"):
        new_late_minutes = 0
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date_param)
        if target_punch_out is not None:
            new_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            new_overtime_minutes = compute_overtime(target_punch_out, emp["shift_start"], emp["shift_end"], date_param)
            new_half_day = (new_work_hours < 4.50)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    changes = {}
    if target_status != doc.get("status"):
        changes["status"] = {"from": doc.get("status"), "to": target_status}

    old_pi = doc.get("punch_in")
    if isinstance(old_pi, datetime) and old_pi.tzinfo is None:
        old_pi = old_pi.replace(tzinfo=timezone.utc)
    if old_pi != target_punch_in:
        changes["punch_in"] = {"from": old_pi, "to": target_punch_in}

    old_po = doc.get("punch_out")
    if isinstance(old_po, datetime) and old_po.tzinfo is None:
        old_po = old_po.replace(tzinfo=timezone.utc)
    if old_po != target_punch_out:
        changes["punch_out"] = {"from": old_po, "to": target_punch_out}

    if new_work_hours != doc.get("work_hours"):
        changes["work_hours"] = {"from": doc.get("work_hours"), "to": new_work_hours}

    if new_late_minutes != doc.get("late_minutes", 0):
        changes["late_minutes"] = {"from": doc.get("late_minutes", 0), "to": new_late_minutes}

    if new_overtime_minutes != doc.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {"from": doc.get("overtime_minutes", 0), "to": new_overtime_minutes}

    if new_half_day != doc.get("half_day", False):
        changes["half_day"] = {"from": doc.get("half_day", False), "to": new_half_day}

    if not changes:
        raise HTTPException(422, "No fields were changed")

    now_utc = truncate_to_seconds(datetime.now(timezone.utc))
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    # Compare-And-Set optimistic concurrency lock:
    # Protects status, punch_in, punch_out, and history state against concurrent modifications.
    current_status = doc.get("status")
    current_punch_in = doc.get("punch_in")
    current_punch_out = doc.get("punch_out")

    if "history" not in doc:
        history_filter = {"$or": [{"history": {"$exists": False}}, {"history": []}]}
    else:
        history_filter = {"history": doc.get("history", [])}

    cas_filter = {
        "_id": doc["_id"],
        "status": current_status,
        "punch_in": current_punch_in,
        "punch_out": current_punch_out,
        **history_filter
    }

    res = db.attendance_logs.find_one_and_update(
        cas_filter,
        {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry}
        },
        return_document=pymongo.ReturnDocument.AFTER
    )

    if not res:
        raise HTTPException(409, "Concurrent modification conflict")

    return format_attendance_doc(res)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    start_date, end_date = get_month_range(month)
    s_str = start_date.isoformat()
    e_str = end_date.isoformat()

    w_days = count_working_days(start_date, end_date, emp["joined_on"])

    pipeline = _build_employee_monthly_pipeline(emp_code, s_str, e_str)
    res = list(db.attendance_logs.aggregate(pipeline))

    if res:
        row = res[0]
        p_days = float(row.get("present_days", 0.0))
        l_days = int(row.get("leave_days", 0))
        l_cnt = int(row.get("late_count", 0))
        tot_late = int(row.get("total_late_minutes", 0))
        tot_ot = int(row.get("total_overtime_minutes", 0))
    else:
        p_days = 0.0
        l_days = 0
        l_cnt = 0
        tot_late = 0
        tot_ot = 0

    if w_days > 0:
        pct = round_half_up((p_days / float(w_days)) * 100.0, 2)
    else:
        pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": w_days,
        "present_days": p_days,
        "leave_days": l_days,
        "late_count": l_cnt,
        "total_late_minutes": tot_late,
        "total_overtime_minutes": tot_ot,
        "attendance_pct": pct
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None
):
    start_date, end_date = get_month_range(month)
    s_str = start_date.isoformat()
    e_str = end_date.isoformat()

    match_emp = {"joined_on": {"$lte": e_str}}
    if department:
        match_emp["department"] = department

    pipeline = _build_department_summary_pipeline(match_emp, s_str, e_str)
    raw_items = list(db.employees.aggregate(pipeline))
    items = []
    for r in raw_items:
        avg_wh = r.get("avg_work_hours_raw")
        avg_wh_val = round_half_up(avg_wh, 2) if avg_wh is not None else None
        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg_wh_val,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"])
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    start_date, end_date = get_month_range(month)
    s_str = start_date.isoformat()
    e_str = end_date.isoformat()

    pipeline = _build_late_leaderboard_pipeline(s_str, e_str, department, limit)
    raw_items = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for r in raw_items:
        items.append({
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"])
        })

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date_str: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date_str: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")
):
    # Validate real calendar dates before parsing
    validate_date_str(from_date_str, "from")
    validate_date_str(to_date_str, "to")

    emp = db.employees.find_one({"department": department})
    if not emp:
        raise HTTPException(404, "Department not found")

    d_from = date.fromisoformat(from_date_str)
    d_to = date.fromisoformat(to_date_str)

    if d_to < d_from:
        raise HTTPException(422, "to date must be >= from date")

    num_days = (d_to - d_from).days + 1
    if num_days > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    pipeline = _build_department_trend_pipeline(department, d_from, d_to)
    raw_items = list(db.employees.aggregate(pipeline))
    items = []
    for r in raw_items:
        ar_raw = r.get("attendance_rate_raw")
        ar_val = round_half_up(ar_raw, 4) if ar_raw is not None else None

        ma_raw = r.get("moving_avg_7d_raw")
        ma_val = round_half_up(ma_raw, 4) if ma_raw is not None else None

        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": ar_val,
            "moving_avg_7d": ma_val
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if status is not None and status not in VALID_ALL_STATUSES:
            raise HTTPException(422, f"Invalid status '{status}'.")
        validate_date_str(date_from, "date_from")
        validate_date_str(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from cannot be greater than date_to")

        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size
        }
        res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_bson(res)}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly")

        start_date, end_date = get_month_range(month)
        pipeline = _build_employee_monthly_pipeline(emp_code, start_date.isoformat(), end_date.isoformat())
        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_bson(res)}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")

        start_date, end_date = get_month_range(month)
        s_str = start_date.isoformat()
        e_str = end_date.isoformat()
        match_emp = {"joined_on": {"$lte": e_str}}
        if department:
            match_emp["department"] = department

        pipeline = _build_department_summary_pipeline(match_emp, s_str, e_str)
        cmd = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}
        res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": clean_bson(res)}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")

        start_date, end_date = get_month_range(month)
        s_str = start_date.isoformat()
        e_str = end_date.isoformat()

        pipeline = _build_late_leaderboard_pipeline(s_str, e_str, department, limit)
        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_bson(res)}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required for department_trend")

        validate_date_str(from_date, "from")
        validate_date_str(to_date, "to")

        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)

        if d_to < d_from:
            raise HTTPException(422, "to date must be >= from date")

        num_days = (d_to - d_from).days + 1
        if num_days > 92:
            raise HTTPException(422, "Date range cannot exceed 92 days")

        pipeline = _build_department_trend_pipeline(department, d_from, d_to)
        cmd = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}
        res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": clean_bson(res)}

    raise HTTPException(422, "Invalid endpoint")
