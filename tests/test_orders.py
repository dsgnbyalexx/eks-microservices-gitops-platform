"""Orders orchestration, tested with a fake of the other services so every
failure mode can be triggered on demand."""

import pytest

from common.http_client import DownstreamError
from helpers import bearer
from orders import app as orders_module
from orders.app import Downstream, create

PRODUCTS = {
    "mug": {"id": "mug", "name": "Cloud Mug", "price_cents": 1499, "currency": "USD"},
    "tee": {"id": "tee", "name": "Tee", "price_cents": 2499, "currency": "USD"},
    "euro-mug": {"id": "euro-mug", "name": "Euro Mug", "price_cents": 1299, "currency": "EUR"},
}


class FakeServices:
    def __init__(self):
        self.catalog_down = self.payments_down = self.notifications_down = False
        self.charges, self.notifications = [], []

    def get_product(self, product_id):
        if self.catalog_down:
            raise DownstreamError("catalog down")
        return PRODUCTS.get(product_id)

    def charge(self, order_id, amount_cents, currency, card_token):
        if self.payments_down:
            raise DownstreamError("payments down")
        self.charges.append((order_id, amount_cents, currency))
        if card_token == "tok_decline":
            return 402, {"status": "declined", "reason": "card declined (simulated)"}
        return 201, {"payment_id": "pay_123", "status": "approved"}

    def notify(self, subject, body):
        if self.notifications_down:
            raise DownstreamError("notifications down")
        self.notifications.append(subject)


@pytest.fixture
def services():
    return FakeServices()


@pytest.fixture
def client(fake_redis, services):
    return create(redis_client=fake_redis, downstream=services).test_client()


ORDER = {
    "items": [{"product_id": "mug", "quantity": 2}, {"product_id": "tee", "quantity": 1}],
    "card_token": "tok_visa",
}


def place(client, body=ORDER, user="demo"):
    return client.post("/orders", json=body, headers=bearer(user))


def test_successful_order(client, services):
    response = place(client)
    order = response.json["order"]
    assert response.status_code == 201
    assert order["status"] == "CONFIRMED" and order["total_cents"] == 2 * 1499 + 2499
    assert order["notification_sent"] is True
    assert services.charges == [(order["id"], 5497, "USD")], "charged once, keyed by order id"
    assert client.get(f"/orders/{order['id']}", headers=bearer()).json["order"] == order


def test_declined_payment_is_recorded(client):
    response = place(client, {**ORDER, "card_token": "tok_decline"})
    assert response.status_code == 402
    order_id = response.json["order"]["id"]
    stored = client.get(f"/orders/{order_id}", headers=bearer()).json["order"]
    assert stored["status"] == "PAYMENT_DECLINED"


def test_payments_outage_is_recorded_not_lost(client, services):
    services.payments_down = True
    response = place(client)
    assert response.status_code == 503
    order_id = response.json["order"]["id"]
    assert client.get(f"/orders/{order_id}", headers=bearer()).json["order"]["status"] == (
        "PAYMENT_ERROR"
    )


def test_notification_outage_does_not_fail_a_paid_order(client, services):
    services.notifications_down = True
    response = place(client)
    assert response.status_code == 201
    assert response.json["order"]["status"] == "CONFIRMED"
    assert response.json["order"]["notification_sent"] is False


def test_catalog_outage_creates_no_order(client, services, fake_redis):
    services.catalog_down = True
    assert place(client).status_code == 503
    assert fake_redis.keys("orders:*") == []
    assert services.charges == []


def test_unknown_product_and_mixed_currency_are_422(client):
    unknown = {**ORDER, "items": [{"product_id": "nope", "quantity": 1}]}
    mixed = {
        **ORDER,
        "items": [{"product_id": "mug", "quantity": 1}, {"product_id": "euro-mug", "quantity": 1}],
    }
    assert place(client, unknown).status_code == 422
    assert place(client, mixed).status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"items": [], "card_token": "tok_visa"},
        {"items": [{"product_id": "mug", "quantity": 0}], "card_token": "tok_visa"},
        {"items": [{"product_id": "mug", "quantity": True}], "card_token": "tok_visa"},
        {"items": [{"product_id": "MUG!", "quantity": 1}], "card_token": "tok_visa"},
        {"items": [{"product_id": "mug", "quantity": 1}] * 2, "card_token": "tok_visa"},
        {"items": [{"product_id": "mug", "quantity": 1}]},
    ],
    ids=["empty", "zero-qty", "bool-qty", "bad-id", "duplicate", "no-card"],
)
def test_invalid_orders_are_rejected(client, body):
    assert place(client, body).status_code == 400


def test_other_users_cannot_see_an_order(client):
    order_id = place(client).json["order"]["id"]
    assert client.get(f"/orders/{order_id}", headers=bearer("eve")).status_code == 404


def test_payment_call_is_retried_with_the_same_idempotency_key(monkeypatch):
    attempts = []

    class Resp:
        status_code = 201

        @staticmethod
        def json():
            return {"payment_id": "pay_1"}

    def flaky(method, url, *, json=None, headers=None):
        attempts.append(headers["Idempotency-Key"])
        if len(attempts) < 3:
            raise DownstreamError("timeout")
        return Resp()

    monkeypatch.setattr(orders_module, "call", flaky)
    downstream = Downstream("http://c", "http://p", "http://n", retry_delay=0)
    assert downstream.charge("ord_abc", 100, "USD", "tok_visa") == (201, {"payment_id": "pay_1"})
    assert attempts == ["ord_abc"] * 3, "every retry reuses the same key, so no double charge"


def test_payment_retries_stop_at_the_overall_deadline(monkeypatch):
    attempts = []

    def slow_failure(*_args, **_kwargs):
        attempts.append(1)
        raise DownstreamError("DNS lookup failed after 4 s")

    monkeypatch.setattr(orders_module, "call", slow_failure)
    downstream = Downstream(
        "http://c", "http://p", "http://n", retry_delay=0.05, payment_budget_seconds=0.01
    )
    with pytest.raises(DownstreamError):
        downstream.charge("ord_x", 1, "USD", "tok_visa")
    assert len(attempts) == 1, "no retry is started once the budget would be exceeded"


def test_payment_retries_give_up_after_three_attempts(monkeypatch):
    def down(*_args, **_kwargs):
        raise DownstreamError("down")

    monkeypatch.setattr(orders_module, "call", down)
    with pytest.raises(DownstreamError):
        Downstream("http://c", "http://p", "http://n", retry_delay=0).charge(
            "ord_x", 1, "USD", "tok_visa"
        )
