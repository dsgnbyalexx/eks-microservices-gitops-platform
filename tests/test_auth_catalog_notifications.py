import pytest
import redis

from auth.app import create as create_auth
from catalog.app import create as create_catalog
from common.auth import verify_token
from helpers import DEMO_PASSWORD, bearer
from notifications.app import KEEP_LAST
from notifications.app import create as create_notifications

# ------------------------------------------------------------------ auth ---


@pytest.fixture
def auth_client():
    return create_auth().test_client()


def test_login_returns_a_verifiable_token(auth_client):
    response = auth_client.post("/auth/login", json={"username": "demo", "password": DEMO_PASSWORD})
    assert response.status_code == 200
    assert verify_token(response.json["access_token"])["sub"] == "demo"
    me = auth_client.get(
        "/auth/me", headers={"Authorization": f"Bearer {response.json['access_token']}"}
    )
    assert me.json == {"username": "demo"}


@pytest.mark.parametrize(
    "credentials",
    [{"username": "demo", "password": "wrong-password"}, {"username": "eve", "password": "x"}],
    ids=["wrong-password", "unknown-user"],
)
def test_bad_credentials_get_the_same_401(auth_client, credentials):
    response = auth_client.post("/auth/login", json=credentials)
    assert response.status_code == 401 and response.json["error"] == "invalid credentials"


def test_login_requires_both_fields(auth_client):
    assert auth_client.post("/auth/login", json={"username": "demo"}).status_code == 400


def test_auth_refuses_to_start_without_a_strong_password(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "short")
    with pytest.raises(RuntimeError, match="DEMO_PASSWORD"):
        create_auth()


# --------------------------------------------------------------- catalog ---


def test_catalog_lists_and_gets_products():
    client = create_catalog().test_client()
    products = client.get("/catalog/products").json["products"]
    assert {p["id"] for p in products} >= {"mug", "hoodie"}
    assert client.get("/catalog/products/mug").json["product"]["price_cents"] == 1499
    assert client.get("/catalog/products/nope").status_code == 404


# --------------------------------------------------------- notifications ---


@pytest.fixture
def notifications(fake_redis):
    return create_notifications(redis_client=fake_redis).test_client()


def test_notifications_are_stored_newest_first(notifications):
    for subject in ("first", "second"):
        r = notifications.post(
            "/internal/notifications", json={"subject": subject, "body": "hi"}, headers=bearer()
        )
        assert r.status_code == 202
    listed = notifications.get("/notifications", headers=bearer()).json["notifications"]
    assert [n["subject"] for n in listed] == ["second", "first"]


def test_users_only_see_their_own_notifications(notifications):
    notifications.post(
        "/internal/notifications", json={"subject": "for demo", "body": ""}, headers=bearer("demo")
    )
    assert notifications.get("/notifications", headers=bearer("eve")).json == {"notifications": []}


def test_only_the_last_100_notifications_are_kept(notifications, fake_redis):
    for i in range(KEEP_LAST + 5):
        notifications.post(
            "/internal/notifications", json={"subject": f"n{i}", "body": ""}, headers=bearer()
        )
    assert fake_redis.llen("notifications:demo") == KEEP_LAST


@pytest.mark.parametrize("payload", [{}, {"subject": ""}, {"subject": "x" * 201, "body": ""}])
def test_invalid_notifications_are_rejected(notifications, payload):
    assert (
        notifications.post("/internal/notifications", json=payload, headers=bearer()).status_code
        == 400
    )


def test_public_notifications_path_is_read_only(notifications):
    response = notifications.post("/notifications", json={"subject": "x"}, headers=bearer())
    assert response.status_code == 405


def test_notifications_require_a_token(notifications):
    assert notifications.get("/notifications").status_code == 401


def test_readiness_follows_redis(notifications, fake_redis, monkeypatch):
    assert notifications.get("/readyz").status_code == 200

    def redis_down():
        raise redis.ConnectionError("Connection refused")

    monkeypatch.setattr(fake_redis, "ping", redis_down)
    assert notifications.get("/readyz").status_code == 503
    assert notifications.get("/healthz").status_code == 200, "liveness unaffected"
