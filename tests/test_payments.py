import pytest

from helpers import bearer
from payments.app import create

GOOD = {"order_id": "ord_1", "amount_cents": 2500, "currency": "USD", "card_token": "tok_visa"}


@pytest.fixture
def client(fake_redis):
    return create(redis_client=fake_redis).test_client()


def pay(client, body=None, key="key-00000001", user="demo"):
    headers = {**bearer(user)}
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post("/payments", json=body or GOOD, headers=headers)


def test_approved_charge(client):
    response = pay(client)
    assert response.status_code == 201
    assert response.json["status"] == "approved" and response.json["simulated"] is True


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({**GOOD, "card_token": "tok_decline"}, "card declined"),
        ({**GOOD, "amount_cents": 500_001}, "over simulated limit"),
    ],
)
def test_declined_charges_return_402(client, body, reason):
    response = pay(client, body)
    assert response.status_code == 402 and reason in response.json["reason"]


def test_retry_with_same_key_replays_instead_of_charging_twice(client):
    first = pay(client)
    second = pay(client)
    assert second.status_code == first.status_code == 201
    assert second.json["payment_id"] == first.json["payment_id"], "no second charge"
    assert second.headers["Idempotent-Replayed"] == "true"


def test_declines_are_replayed_too(client):
    body = {**GOOD, "card_token": "tok_decline"}
    assert pay(client, body).json["payment_id"] == pay(client, body).json["payment_id"]


def test_same_key_with_different_request_is_rejected(client):
    pay(client)
    response = pay(client, {**GOOD, "amount_cents": 9999})
    assert response.status_code == 409


def test_keys_are_scoped_per_user(client):
    mine, theirs = pay(client, user="demo"), pay(client, user="eve")
    assert mine.json["payment_id"] != theirs.json["payment_id"]


def test_concurrent_duplicate_loses_the_race_and_replays(client, fake_redis, monkeypatch):
    """Simulate two identical requests racing: the other one stores its result
    between our GET (nothing found) and our SET NX (which then fails)."""
    winner = pay(client)  # stores the 'winning' result
    real_get = fake_redis.get
    calls = {"n": 0}

    def first_get_misses(key):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_get(key)

    monkeypatch.setattr(fake_redis, "get", first_get_misses)
    loser = pay(client)
    assert loser.json["payment_id"] == winner.json["payment_id"]
    assert loser.headers["Idempotent-Replayed"] == "true"


@pytest.mark.parametrize("key", [None, "short", "has spaces in it", "x" * 129])
def test_idempotency_key_is_required_and_validated(client, key):
    assert pay(client, key=key).status_code == 400


@pytest.mark.parametrize(
    "body",
    [
        {**GOOD, "amount_cents": 0},
        {**GOOD, "amount_cents": True},
        {**GOOD, "amount_cents": "25"},
        {**GOOD, "currency": "usd"},
        {**GOOD, "order_id": ""},
        {**GOOD, "card_token": ""},
    ],
)
def test_invalid_payment_requests(client, body):
    assert pay(client, body).status_code == 400


def test_payments_require_a_token(client):
    response = client.post("/payments", json=GOOD, headers={"Idempotency-Key": "key-00000001"})
    assert response.status_code == 401
