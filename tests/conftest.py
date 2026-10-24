import fakeredis
import pytest

from helpers import DEMO_PASSWORD, TEST_SECRET


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET_KEY", TEST_SECRET)
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", DEMO_PASSWORD)


@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=True)
