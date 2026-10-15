# HROne Employee Attendance API

FastAPI + MongoDB Atlas backend for the HROne attendance assignment. All application code is in `app/main.py`.

## Run

Set `MONGODB_URI` to an Atlas `mongodb+srv://` connection string in an untracked `.env` file. Optionally set `MONGO_DB` (defaults to `attendance_db`). Then run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

Open `http://localhost:8000/docs` for Swagger UI. `GET /health` returns 200 only when MongoDB answers a ping.

## Test Safely

Tests delete documents. They require explicit opt-in and only use the separate `test_attendance_db` database on Atlas. The harness reuses `MONGODB_URI` only when `ALLOW_ATLAS_TESTS=1`; optionally set `TEST_MONGO_URI` to a separate Atlas URI. Never set `TEST_MONGO_DB` to the runtime database.

```powershell
$env:ALLOW_ATLAS_TESTS = "1"
$env:TEST_MONGO_DB = "test_attendance_db"
pytest -v
```

The harness rejects missing opt-in, non-Atlas URIs, any test database other than `test_attendance_db`, and attempts to use the runtime database. Destructive fixtures operate only on `test_attendance_db`. There is no local MongoDB fallback. The Atlas database user must have the required permissions, and the cluster must allow connections from this machine.

## Notes

- Runtime requires `MONGODB_URI` or `MONGO_URI` to be an Atlas SRV URI; `MONGO_DB` defaults to `attendance_db`. Real environment variables take priority over `.env`.
- Startup creates indexes idempotently; the client uses a bounded server selection timeout and closes on shutdown.
- The implementation preserves IST attendance dates, whole-second timestamp storage, strict late/overtime thresholds, half-up rounding, join-date-aware analytics, tie-aware leaderboard ranking, compare-and-set concurrency, append-only regularization history, the 24-hour punch duration limit, and the inclusive 92-day trend limit.
- Do not commit `.env`, virtual environments, caches, sample data dumps, supplied kit documents, a Dockerfile, or secrets.
