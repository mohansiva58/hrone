"""
Pytest configuration and isolated test database harness.
Ensures destructive operations are strictly confined to a local disposable test database.
"""
import os
import pytest
from dotenv import load_dotenv
from pymongo import MongoClient

# Configure environment for tests before importing application modules
load_dotenv()
TEST_MONGO_URI = os.getenv("TEST_MONGO_URI", "mongodb://127.0.0.1:27017")
TEST_MONGO_DB = os.getenv("TEST_MONGO_DB", "test_attendance_db")

os.environ["MONGO_URI"] = TEST_MONGO_URI
os.environ["MONGO_DB"] = TEST_MONGO_DB
os.environ["TEST_MODE"] = "1"

import app.main as app_main
from app.main import verify_test_database_safety

# Validate the explicit URI and disposable database name before creating the test client.
verify_test_database_safety(TEST_MONGO_URI, TEST_MONGO_DB)
test_client = MongoClient(TEST_MONGO_URI, tz_aware=True)
test_db = test_client[TEST_MONGO_DB]

app_main.client = test_client
app_main.db = test_db


@pytest.fixture(scope="session", autouse=True)
def init_test_db():
    """Ensure indexes exist on test database."""
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
