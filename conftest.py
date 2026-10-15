"""Pytest configuration with an explicit, isolated Atlas test database."""
import os

import pytest
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import InvalidOperation

load_dotenv()

# A separate URI may be supplied, otherwise reuse the Atlas deployment only after
# explicit opt-in. The database selection is always forced to the test database.
TEST_MONGO_URI = os.getenv("TEST_MONGO_URI") or os.getenv("MONGODB_URI") or os.getenv("MONGO_URI")
TEST_MONGO_DB = os.getenv("TEST_MONGO_DB", "test_attendance_db")
ALLOW_ATLAS_TESTS = os.getenv("ALLOW_ATLAS_TESTS", "")
RUNTIME_DB = os.getenv("MONGO_DB", "attendance_db")
REQUIRED_TEST_DB = "test_attendance_db"

from app.main import verify_test_database_safety  # noqa: E402

verify_test_database_safety(TEST_MONGO_URI, TEST_MONGO_DB, ALLOW_ATLAS_TESTS)
if TEST_MONGO_DB != REQUIRED_TEST_DB:
    raise ValueError("Safety check failed: destructive pytest fixtures may only use 'test_attendance_db'.")
if TEST_MONGO_DB == RUNTIME_DB:
    raise ValueError("Safety check failed: test database cannot equal the runtime database.")

os.environ["MONGODB_URI"] = TEST_MONGO_URI
os.environ["MONGO_DB"] = TEST_MONGO_DB
os.environ["TEST_MODE"] = "1"

import app.main as app_main  # noqa: E402

test_client = MongoClient(
    TEST_MONGO_URI,
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
try:
    test_client.admin.command("ping")
except Exception:
    test_client.close()
    raise RuntimeError("Atlas test database is unavailable; no destructive tests were run.") from None
test_db = test_client[TEST_MONGO_DB]
app_main.client = test_client
app_main.db = test_db


@pytest.fixture(scope="session", autouse=True)
def init_test_db(request):
    """Create required indexes and confine all fixtures to this test database."""
    def close_test_client():
        test_client.close()
        with pytest.raises(InvalidOperation, match="Cannot use MongoClient after close"):
            test_client.admin.command("ping")

    request.addfinalizer(close_test_client)

    test_db.employees.create_index([("emp_code", 1)], unique=True)
    test_db.employees.create_index([("department", 1), ("joined_on", 1), ("emp_code", 1)])
    test_db.employees.create_index([("department", 1), ("emp_code", 1)])
    test_db.employees.create_index([("joined_on", 1)])
    test_db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    test_db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    test_db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])
    test_db.attendance_logs.create_index([("emp_code", 1), ("date", 1), ("status", 1)])
    test_db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    yield
