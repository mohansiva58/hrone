# HROne Employee Attendance API

FastAPI + MongoDB backend for the HROne attendance assignment. All application code is in `app/main.py`.

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:MONGO_URI = "mongodb://127.0.0.1:27017"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

Open `http://localhost:8000/docs` for Swagger UI. `GET /health` returns 200 only when MongoDB answers a ping.

## Test Safely

Tests delete collections, so use only a disposable local database:

```powershell
$env:TEST_MONGO_URI = "mongodb://127.0.0.1:27017"
$env:TEST_MONGO_DB = "test_attendance_db"
pytest -v
```

The test harness rejects remote MongoDB hosts and non-test database names. A local MongoDB 6.0+ server is required for the full integration and explain-plan suite.

## Notes

- Runtime config comes from `MONGO_URI`/`MONGODB_URI` and `MONGO_DB`; real environment variables take priority over `.env`.
- Startup creates indexes idempotently.
- The implementation preserves IST attendance dates, whole-second timestamp storage, strict late/overtime thresholds, half-up rounding, join-date-aware analytics, tie-aware leaderboard ranking, compare-and-set concurrency, append-only regularization history, the 24-hour punch duration limit, and the inclusive 92-day trend limit.
- Do not commit `.env`, virtual environments, caches, sample data dumps, the supplied kit documents, a Dockerfile, or secrets.
