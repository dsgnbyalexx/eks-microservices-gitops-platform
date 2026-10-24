"""Shared behaviour every service inherits: health, readiness, metrics, request ids,
error handling, JWT verification and the HTTP client."""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
import requests
from flask import jsonify

from common import auth, http_client
from common.app_factory import create_app
from helpers import TEST_SECRET, bearer


def _app(readiness=lambda: True):
    app = create_app("demo", readiness_check=readiness)

    @app.get("/items/<item_id>")
    def item(item_id):
        return jsonify(id=item_id)

    @app.get("/boom")
    def boom():
        raise RuntimeError("database password is hunter2")

    @app.get("/private")
    @auth.require_auth
    def private():
        return jsonify(ok=True)

    return app.test_client()


def test_liveness_never_checks_dependencies():
    def broken():
        raise ConnectionError("redis down")

    assert _app(readiness=broken).get("/healthz").status_code == 200


@pytest.mark.parametrize("readiness", [lambda: False, lambda: (_ for _ in ()).throw(OSError())])
def test_readiness_fails_when_dependency_is_down(readiness):
    response = _app(readiness=readiness).get("/readyz")
    assert response.status_code == 503 and response.json["status"] == "unavailable"


def test_request_id_is_generated_or_propagated():
    client = _app()
    assert len(client.get("/items/1").headers["X-Request-ID"]) == 36
    echoed = client.get("/items/1", headers={"X-Request-ID": "trace-abc"})
    assert echoed.headers["X-Request-ID"] == "trace-abc"


def test_metrics_use_route_templates_not_raw_paths():
    client = _app()
    for item_id in ("a", "b", "c"):
        client.get(f"/items/{item_id}")
    text = client.get("/metrics").get_data(as_text=True)
    assert 'endpoint="/items/<item_id>"' in text
    assert "/items/a" not in text, "one series per route, not per id"


def test_unexpected_errors_are_not_leaked():
    response = _app().get("/boom")
    assert response.status_code == 500
    assert response.json == {"error": "internal server error"}


def test_valid_token_is_accepted():
    assert _app().get("/private", headers=bearer()).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer not-a-jwt"},
    ],
)
def test_missing_or_malformed_tokens_are_rejected(headers):
    assert _app().get("/private", headers=headers).status_code == 401


def _forge(**overrides):
    now = datetime.now(UTC)
    claims = {
        "sub": "demo",
        "iss": auth.ISSUER,
        "aud": auth.AUDIENCE,
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides.pop("claims", {}))
    key = overrides.pop("key", TEST_SECRET)
    algorithm = overrides.pop("algorithm", "HS256")
    return jwt.encode(claims, key, algorithm=algorithm)


@pytest.mark.parametrize(
    ("token", "message"),
    [
        (lambda: bearer(ttl=timedelta(seconds=-1))["Authorization"][7:], "token expired"),
        (lambda: _forge(key="a-different-secret-that-is-long-enough"), "invalid token"),
        (lambda: _forge(claims={"aud": "another-app"}), "invalid token"),
        (lambda: _forge(claims={"iss": "someone-else"}), "invalid token"),
        (lambda: _forge(key="", algorithm="none"), "invalid token"),
    ],
    ids=["expired", "wrong-key", "wrong-audience", "wrong-issuer", "alg-none"],
)
def test_bad_tokens_are_rejected(token, message):
    response = _app().get("/private", headers={"Authorization": f"Bearer {token()}"})
    assert response.status_code == 401 and response.json["error"] == message


def test_short_secret_is_refused(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET_KEY", "too-short")
    with pytest.raises(RuntimeError, match="at least 32"):
        auth.secret_key()


def test_http_client_turns_network_errors_and_5xx_into_downstream_error(monkeypatch):
    def unreachable(*_args, **_kwargs):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(http_client.requests, "request", unreachable)
    with pytest.raises(http_client.DownstreamError, match="ConnectionError"):
        http_client.call("GET", "http://catalog:8000/x")

    class Resp:
        status_code = 503

    monkeypatch.setattr(http_client.requests, "request", lambda *a, **k: Resp())
    with pytest.raises(http_client.DownstreamError, match="503"):
        http_client.call("GET", "http://catalog:8000/x")


def test_http_client_always_sets_a_timeout(monkeypatch):
    seen = {}

    class Resp:
        status_code = 200

    def capture(method, url, **kwargs):
        seen.update(kwargs)
        return Resp()

    monkeypatch.setattr(http_client.requests, "request", capture)
    http_client.call("GET", "http://catalog:8000/x")
    assert seen["timeout"] == http_client.DEFAULT_TIMEOUT
