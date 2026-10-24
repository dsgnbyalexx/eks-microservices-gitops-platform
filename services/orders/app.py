"""Orders service: orchestrates catalog -> payments -> notifications.

Failure handling, step by step:
  * catalog unreachable      -> 503, no order created
  * payment declined         -> order saved as PAYMENT_DECLINED, 402
  * payments unreachable     -> retried (safe: the order id is the Idempotency-Key);
                                if still failing, order saved as PAYMENT_ERROR, 503
  * notifications unreachable-> order still CONFIRMED (email is best-effort),
                                notification_sent=false is recorded
"""

import json
import os
import re
import time
import uuid
from datetime import UTC, datetime

from flask import abort, g, jsonify, request

from common.app_factory import create_app
from common.auth import require_auth
from common.http_client import DownstreamError, call
from common.logs import log
from common.store import redis_from_env

MAX_ITEMS = 10
MAX_QUANTITY = 10
PRODUCT_ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")
PAYMENT_BUDGET_SECONDS = 6.0  # total for all payment attempts; gateway timeout is 10 s


class Downstream:
    """HTTP calls to the other services. Replaced by a fake in unit tests."""

    def __init__(
        self,
        catalog_url: str,
        payments_url: str,
        notifications_url: str,
        retry_delay: float = 0.2,
        payment_budget_seconds: float = PAYMENT_BUDGET_SECONDS,
    ):
        self.catalog_url = catalog_url
        self.payments_url = payments_url
        self.notifications_url = notifications_url
        self.retry_delay = retry_delay
        self.payment_budget_seconds = payment_budget_seconds

    @classmethod
    def from_env(cls) -> Downstream:
        return cls(
            os.environ.get("CATALOG_URL", "http://catalog:8000"),
            os.environ.get("PAYMENTS_URL", "http://payments:8000"),
            os.environ.get("NOTIFICATIONS_URL", "http://notifications:8000"),
        )

    def get_product(self, product_id: str) -> dict | None:
        response = call("GET", f"{self.catalog_url}/catalog/products/{product_id}")
        if response.status_code == 404:
            return None
        return response.json()["product"]

    def charge(self, order_id: str, amount_cents: int, currency: str, card_token: str):
        payload = {
            "order_id": order_id,
            "amount_cents": amount_cents,
            "currency": currency,
            "card_token": card_token,
        }
        # Overall deadline for ALL attempts. It must stay below the caller's timeout
        # (the gateway/ingress waits 10 s); otherwise the caller gives up first and
        # never learns the outcome. Measured: a DNS lookup for a stopped container
        # takes ~4 s, and request timeouts do not cover DNS.
        deadline = time.monotonic() + self.payment_budget_seconds
        for attempt in range(1, 4):
            try:
                response = call(
                    "POST",
                    f"{self.payments_url}/payments",
                    json=payload,
                    headers={"Idempotency-Key": order_id},
                )
                return response.status_code, response.json()
            except DownstreamError:
                pause = self.retry_delay * attempt
                if attempt == 3 or time.monotonic() + pause >= deadline:
                    raise
                time.sleep(pause)  # short backoff between attempts
        raise AssertionError("unreachable")

    def notify(self, subject: str, body: str) -> None:
        call(
            "POST",
            f"{self.notifications_url}/internal/notifications",
            json={"subject": subject, "body": body},
        )


def _parse_items(body: dict) -> list[dict]:
    items = body.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        abort(400, description=f"items must be a list of 1 to {MAX_ITEMS} entries")
    seen, parsed = set(), []
    for i, item in enumerate(items):
        pid = item.get("product_id") if isinstance(item, dict) else None
        qty = item.get("quantity") if isinstance(item, dict) else None
        if not isinstance(pid, str) or not PRODUCT_ID_RE.match(pid):
            abort(400, description=f"items[{i}].product_id is invalid")
        if not isinstance(qty, int) or isinstance(qty, bool) or not 1 <= qty <= MAX_QUANTITY:
            abort(400, description=f"items[{i}].quantity must be 1 to {MAX_QUANTITY}")
        if pid in seen:
            abort(400, description=f"items[{i}].product_id is duplicated")
        seen.add(pid)
        parsed.append({"product_id": pid, "quantity": qty})
    return parsed


def create(redis_client=None, downstream: Downstream | None = None):
    store = redis_client or redis_from_env()
    services = downstream or Downstream.from_env()
    app = create_app("orders", readiness_check=lambda: store.ping())

    def save(order: dict) -> None:
        store.set(f"orders:{order['id']}", json.dumps(order))

    @app.post("/orders")
    @require_auth
    def place_order():
        body = request.get_json(silent=True) or {}
        items = _parse_items(body)
        card_token = body.get("card_token")
        if not isinstance(card_token, str) or not card_token:
            abort(400, description="card_token is required")

        lines = []
        for item in items:
            try:
                product = services.get_product(item["product_id"])
            except DownstreamError:
                abort(503, description="catalog service unavailable; no order was created")
            if product is None:
                abort(422, description=f"unknown product: {item['product_id']}")
            lines.append(
                {
                    **item,
                    "name": product["name"],
                    "unit_price_cents": product["price_cents"],
                    "currency": product["currency"],
                }
            )

        currencies = {line["currency"] for line in lines}
        if len(currencies) != 1:
            abort(422, description="all items in one order must use the same currency")
        total = sum(line["unit_price_cents"] * line["quantity"] for line in lines)
        order = {
            "id": f"ord_{uuid.uuid4().hex}",
            "user": g.user,
            "items": lines,
            "total_cents": total,
            "currency": currencies.pop(),
            "status": "PENDING",
            "created_at": datetime.now(UTC).isoformat(),
        }
        save(order)

        try:
            status, payment = services.charge(order["id"], total, order["currency"], card_token)
        except DownstreamError as err:
            order["status"] = "PAYMENT_ERROR"
            save(order)
            log("ERROR", "payment unavailable", order_id=order["id"], error=str(err))
            return jsonify(
                error="payment service unavailable; no charge confirmed", order=order
            ), 503

        if status == 402:
            order.update(status="PAYMENT_DECLINED", payment=payment)
            save(order)
            return jsonify(error="payment declined", order=order), 402
        if status != 201:
            abort(502, description="unexpected response from payments")

        order.update(status="CONFIRMED", payment_id=payment["payment_id"])
        try:
            services.notify(
                f"Order {order['id']} confirmed", f"Total: {total / 100:.2f} {order['currency']}"
            )
            order["notification_sent"] = True
        except DownstreamError as err:
            # Best-effort: a failed email must not undo a successful, paid order.
            order["notification_sent"] = False
            log("WARNING", "notification failed", order_id=order["id"], error=str(err))
        save(order)
        return jsonify(order=order), 201

    @app.get("/orders/<order_id>")
    @require_auth
    def get_order(order_id: str):
        raw = store.get(f"orders:{order_id}")
        order = json.loads(raw) if raw else None
        # 404 (not 403) for other users' orders: don't reveal that the id exists.
        if order is None or order["user"] != g.user:
            abort(404, description="order not found")
        return jsonify(order=order)

    return app
