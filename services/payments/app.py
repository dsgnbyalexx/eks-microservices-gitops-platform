"""Payments service: a SIMULATED card charge (no real payment provider).

Idempotency: every charge must carry an Idempotency-Key header. The first
request's result is stored; a retry with the same key returns that stored
result instead of charging again. This is how real payment APIs make retries
safe after timeouts. Reusing a key with a different request body is rejected.
"""

import hashlib
import json
import re
import uuid

from flask import Response, abort, g, jsonify, request

from common.app_factory import create_app
from common.auth import require_auth
from common.store import redis_from_env

DECLINED_CARD_TOKENS = {"tok_decline"}
LIMIT_CENTS = 500_000
IDEMPOTENCY_TTL_SECONDS = 24 * 3600
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def _validate(body: dict) -> None:
    if not isinstance(body.get("order_id"), str) or not body["order_id"]:
        abort(400, description="order_id is required")
    amount = body.get("amount_cents")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        abort(400, description="amount_cents must be a positive integer")
    if not isinstance(body.get("currency"), str) or not CURRENCY_RE.match(body["currency"]):
        abort(400, description="currency must be a 3-letter code")
    if not isinstance(body.get("card_token"), str) or not body["card_token"]:
        abort(400, description="card_token is required")


def _replay(record: dict) -> Response:
    response = jsonify(record["response"])
    response.status_code = record["status"]
    response.headers["Idempotent-Replayed"] = "true"
    return response


def create(redis_client=None):
    store = redis_client or redis_from_env()
    app = create_app("payments", readiness_check=lambda: store.ping())

    @app.post("/payments")
    @require_auth
    def charge():
        key = request.headers.get("Idempotency-Key", "")
        if not KEY_RE.match(key):
            abort(400, description="Idempotency-Key header (8-128 letters/digits/-/_) is required")
        body = request.get_json(silent=True) or {}
        _validate(body)

        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        redis_key = f"payments:idempotency:{g.user}:{key}"

        existing = store.get(redis_key)
        if existing:
            record = json.loads(existing)
            if record["fingerprint"] != fingerprint:
                abort(409, description="Idempotency-Key was already used with a different request")
            return _replay(record)

        if body["card_token"] in DECLINED_CARD_TOKENS:
            status, outcome, reason = 402, "declined", "card declined (simulated)"
        elif body["amount_cents"] > LIMIT_CENTS:
            status, outcome, reason = 402, "declined", "amount over simulated limit"
        else:
            status, outcome, reason = 201, "approved", None

        result = {
            "payment_id": f"pay_{uuid.uuid4().hex[:16]}",
            "order_id": body["order_id"],
            "amount_cents": body["amount_cents"],
            "currency": body["currency"],
            "status": outcome,
            "reason": reason,
            "simulated": True,
        }
        record = {"fingerprint": fingerprint, "status": status, "response": result}

        # SET ... NX: only the FIRST of two simultaneous identical requests stores
        # a result; the other replays it. No double charge even under a race.
        if not store.set(redis_key, json.dumps(record), nx=True, ex=IDEMPOTENCY_TTL_SECONDS):
            return _replay(json.loads(store.get(redis_key)))

        response = jsonify(result)
        response.status_code = status
        return response

    return app
