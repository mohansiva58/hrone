# HROne Employee Attendance & Analytics API

A FastAPI + MongoDB REST API for tracking daily employee attendance, computing attendance metrics, and serving analytics dashboards for HR teams.

## Tech stack

- Python 3.11+
- FastAPI
- PyMongo (synchronous driver)
- MongoDB 6.0+
- Pydantic v2

## Prerequisites

- Python 3.11 or newer
- MongoDB 6.0 or newer
- Configuration via environment variables or local `.env` (never commit secrets)

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `MONGO_URI` | No | `mongodb://localhost:27017` | Full MongoDB connection string; takes precedence |
| `MONGODB_URI` | No | — | Supported alias when `MONGO_URI` is unset |
| `MONGO_DB` | No | `attendance_db` | Database name |

Example local configuration in `.env` (or set directly in the environment):

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

For a cloud deployment, set `MONGODB_URI` (or `MONGO_URI`) to the provider's
connection string and set `MONGO_DB` to the intended database. Keep credentials
in the ignored `.env` or deployment secret store; never commit them. `MONGO_URI`
takes precedence if both URI variables are set.

## Setup

```powershell
# 1. Create and activate virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1      # Windows PowerShell
# or: source .venv/bin/activate   # macOS / Linux

# 2. Install application and test dependencies
pip install -r requirements.txt

# 3. Configure the API database (optional; defaults shown)
$env:MONGO_URI = "mongodb://127.0.0.1:27017"
$env:MONGO_DB = "attendance_db"
```

## Running the API

```powershell
uvicorn app.main:app --port 8000
```

Interactive OpenAPI documentation is available at: `http://localhost:8000/docs`

## Running tests

Tests delete and populate collections in their configured database. They refuse to
start unless `TEST_MONGO_URI` names a local MongoDB host and `TEST_MONGO_DB` is
`test` or starts with `test_`. Do not point these settings at a database containing
data you need. They may be set in the shell or in the ignored local `.env` file.

```powershell
# These are the safe local defaults; set explicitly when running tests
$env:TEST_MONGO_URI = "mongodb://127.0.0.1:27017"
$env:TEST_MONGO_DB = "test_attendance_db"

# Run the complete test suite against the disposable local test database
pytest -v
```

The test suite includes an explain-plan check using 100,000 attendance documents;
explain-plan choices can vary by MongoDB version and data distribution.

## Project structure

```
.
├── app/
│   └── main.py              # All application code (single-file requirement)
├── conftest.py               # Test configuration and test DB isolation harness
├── pytest.ini                # Pytest settings and test discovery paths
├── test_main.py              # Core endpoint tests
├── test_regression_calc.py   # Calculation boundary regression tests
├── test_audit_fixes.py       # Concurrency, explain, safety, and audit tests
├── requirements.txt          # Application and test dependencies
├── REVIEW.md                 # Starter code defect review and fixes
├── DECISIONS.md              # Design choices and architectural decisions
├── README.md                 # Project instructions and rules summary
└── .gitignore                # Secrets, local environments, and supplied kit files
```

Supplied sample data, seeding utilities, and assessment-kit input documents remain
local-only and are intentionally excluded from the public repository.

## Business Rules (R1–R10)

| Rule | PRD Definition |
|---|---|
| **R1** | **Time zone and attendance date.** Shift times and attendance dates are IST (UTC+05:30). A record's date is the IST calendar date of its punch_in. Overnight shifts (shift_end <= shift_start, e.g. 22:00 - 06:00): a punch-in earlier than shift_end belongs to the shift that started the previous day, and the shift ends on the next calendar day. Instants are truncated to whole seconds before any calculation, and the truncated value is stored and returned. |
| **R2** | **Late.** Grace period 10 minutes. Late only if the punch-in is strictly more than 10:00 after shift_start; then late_minutes = whole minutes (floor) since shift_start, not since the grace period ended. Shift 09:30: 09:40:00 gives 0; 09:40:01 gives 10; 10:15:59 gives 45. |
| **R3** | **Overtime.** Whole minutes (floor) from shift_end to punch_out, counted only if that is at least 30; otherwise 0. |
| **R4** | **Work hours.** (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up. null until punch-out. |
| **R5** | **Half day.** After punch-out, half_day is true when the rounded work_hours < 4.50. A half day counts as 0.5 of a present day. |
| **R6** | **Presence.** PRESENT, WFH, ON_DUTY count as present. ABSENT, LEAVE do not. |
| **R7** | **Working days.** Monday - Friday, no holiday calendar; counted from joined_on for mid-month joiners. present_days counts only Mon - Fri records; weekend records still add to late and overtime totals. |
| **R8** | **Reported numbers.** 2 decimals half-up (rates: 4 decimals half-up). |
| **R9** | **Headcount.** Employees with joined_on on or before the end of the period, whether or not they have any logs. |
| **R10** | **Pagination.** page starts at 1; page_size default 20, max 100; total always reflects the filters. |

Employee creation requires distinct shift boundaries. Existing records with equal
times are still classified as overnight by the attendance-date helper.
Attendance request bodies ignore supplied `work_hours`, `late_minutes`,
`overtime_minutes`, and `half_day`; the API always computes these read-only fields.
Unrelated unknown request fields and explicit nulls for non-nullable inputs remain
invalid.

## Submission checklist

- [x] `app/main.py` — all application code in this one file
- [x] `requirements.txt` — includes required dependencies
- [x] `README.md` — setup, run, and business rules documentation
- [x] `REVIEW.md` — defect review and fixes documentation
- [x] `DECISIONS.md` — design decisions (150–200 words)
- [x] All database indexes created at startup
- [x] No `Dockerfile` committed
- [x] Concurrency and compare-and-set optimistic locking implemented and verified
