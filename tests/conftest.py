import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("WORKER_TOKEN", "test-owner-token")
os.environ.setdefault("LOCAL_WORKER_ENABLED", "0")  # tests drive the queue themselves


@pytest.fixture
def appmod(tmp_path, monkeypatch):
    import app as A
    monkeypatch.setattr(A, "USAGE_FILE", tmp_path / "usage.json")
    monkeypatch.setattr(A, "API_KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(A, "JOBS_DIR", tmp_path / "jobs")
    (tmp_path / "jobs").mkdir()
    (tmp_path / "usage.json").write_text("{}")
    (tmp_path / "api_keys.json").write_text("{}")
    with A._jobs_lock:
        A._jobs.clear()
    A._ip_request_log.clear()
    return A


@pytest.fixture
def client(appmod):
    from fastapi.testclient import TestClient
    c = TestClient(appmod.app)
    c.get("/usage")
    return c


VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64  # bytes are never decoded in these tests


def pytest_configure(config):
    config.addinivalue_line("markers", "locked: run without the own-hardware entitlement")
