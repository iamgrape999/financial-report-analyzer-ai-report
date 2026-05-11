"""
pytest configuration — set environment variables before any test module is imported.

pytest loads conftest.py files before collecting/importing test modules, so this
ensures credit_report.config reads the correct test values when imported by any suite.
"""
import os

os.environ.setdefault("GEMINI_API_KEY", "mock-key-for-testing")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("AUTO_CREATE_TABLES", "true")
