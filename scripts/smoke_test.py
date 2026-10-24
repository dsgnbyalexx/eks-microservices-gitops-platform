"""End-to-end smoke test through the gateway, like a real client would use it.

    python scripts/smoke_test.py                      # Docker Compose (default URL)
    SMOKE_BASE_URL=http://shop.localtest.me:8080 python scripts/smoke_test.py   # Kubernetes

Reads DEMO_PASSWORD from the environment or from .env. Exits non-zero on failure.
"""

import os
import sys
import uuid
from pathlib import Path

import requests

BASE = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:18090").rstrip("/")
TIMEOUT = 10


def demo_password() -> str:
    if os.environ.get("DEMO_PASSWORD"):
        return os.environ["DEMO_PASSWORD"]
    env_file = Path(__file__).resolve().parents[1] / ".env"
    for line in env_file.read_text().splitlines() if env_file.exists() else []:
        if line.startswith("DEMO_PASSWORD="):
            return line.split("=", 1)[1].strip()
    sys.exit("DEMO_PASSWORD not set (environment or .env)")


results: list[tuple[bool, str]] = []


def check(condition: bool, description: str) -> None:
    results.append((condition, description))
    print(f"[{'PASS' if condition else 'FAIL'}] {description}")


def main() -> int:
    s = requests.Session()

    r = s.post(
        f"{BASE}/auth/login", json={"username": "demo", "password": "wrong"}, timeout=TIMEOUT
    )
    check(r.status_code == 401, "wrong password is rejected (401)")

    r = s.post(
        f"{BASE}/auth/login",
        json={"username": "demo", "password": demo_password()},
        timeout=TIMEOUT,
    )
    check(r.status_code == 200, "login succeeds and returns a token")
    if r.status_code != 200:
        return 1
    auth = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r = s.get(f"{BASE}/catalog/products", timeout=TIMEOUT)
    check(r.status_code == 200 and len(r.json()["products"]) >= 4, "catalog lists products")

    r = s.post(
        f"{BASE}/orders",
        json={"items": [{"product_id": "mug", "quantity": 1}], "card_token": "tok_visa"},
        timeout=TIMEOUT,
    )
    check(r.status_code == 401, "ordering without a token is rejected (401)")

    trace_id = f"smoke-{uuid.uuid4().hex[:12]}"
    order_body = {
        "items": [{"product_id": "mug", "quantity": 2}, {"product_id": "hoodie", "quantity": 1}],
        "card_token": "tok_visa",
    }
    r = s.post(
        f"{BASE}/orders",
        json=order_body,
        headers={**auth, "X-Request-ID": trace_id},
        timeout=TIMEOUT,
    )
    order = r.json().get("order", {})
    check(
        r.status_code == 201 and order.get("status") == "CONFIRMED",
        "order is placed and paid across orders -> catalog -> payments",
    )
    check(order.get("total_cents") == 2 * 1499 + 4999, "total is computed from catalog prices")
    check(order.get("notification_sent") is True, "confirmation notification was sent")
    check(r.headers.get("X-Request-ID") == trace_id, f"request id {trace_id} is propagated")

    r = s.get(f"{BASE}/orders/{order.get('id')}", headers=auth, timeout=TIMEOUT)
    check(
        r.status_code == 200 and r.json()["order"]["id"] == order.get("id"),
        "order can be read back (state shared via Redis)",
    )

    r = s.post(
        f"{BASE}/orders",
        json={**order_body, "card_token": "tok_decline"},
        headers=auth,
        timeout=TIMEOUT,
    )
    check(
        r.status_code == 402 and r.json()["order"]["status"] == "PAYMENT_DECLINED",
        "declined card is handled (402, PAYMENT_DECLINED)",
    )

    r = s.get(f"{BASE}/notifications", headers=auth, timeout=TIMEOUT)
    subjects = [n["subject"] for n in r.json().get("notifications", [])]
    check(
        any(order.get("id", "?") in subj for subj in subjects),
        "notification for the order is visible",
    )

    r = s.post(f"{BASE}/payments", json={}, headers=auth, timeout=TIMEOUT)
    check(r.status_code == 404, "payments service is NOT exposed through the gateway")

    r = s.post(
        f"{BASE}/notifications",
        json={"subject": "spoof", "body": ""},
        headers=auth,
        timeout=TIMEOUT,
    )
    check(r.status_code == 403, "notifications cannot be created from outside (403)")

    failed = [d for ok, d in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed against {BASE}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
