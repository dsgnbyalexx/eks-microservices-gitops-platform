"""End-to-end smoke test through the gateway, like a real client would use it.

    python scripts/smoke_test.py                                   # Docker Compose
    SMOKE_BASE_URL=http://127.0.0.1:18080 python scripts/smoke_test.py --wait-seconds 60
                                                                   # Kubernetes (kind)

--wait-seconds polls every public route until it gives its expected answer before
running the checks. Needed on Kubernetes: `helm --wait` only means the PODS are
ready; the ingress controller learns their addresses a moment later (observed in
CI: /orders answered 503 "no available server" 0.6 s after Helm finished).

Reads DEMO_PASSWORD from the environment or from .env. Exits non-zero on failure.
"""

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

import requests

BASE = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:18090").rstrip("/")
TIMEOUT = 10

# Route -> the status it returns when its backend is reachable (no token sent).
ROUTE_PROBES = [
    ("POST", "/auth/login", {400, 401}),
    ("GET", "/catalog/products", {200}),
    ("POST", "/orders", {401}),
    ("GET", "/notifications", {401}),
]


def demo_password() -> str:
    if os.environ.get("DEMO_PASSWORD"):
        return os.environ["DEMO_PASSWORD"]
    env_file = Path(__file__).resolve().parents[1] / ".env"
    for line in env_file.read_text().splitlines() if env_file.exists() else []:
        if line.startswith("DEMO_PASSWORD="):
            return line.split("=", 1)[1].strip()
    sys.exit("DEMO_PASSWORD not set (environment or .env)")


def body(response: requests.Response) -> dict:
    """JSON body, or {} if the answer is not JSON (e.g. a proxy error page)."""
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def wait_for_routes(session: requests.Session, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    started = time.monotonic()
    pending = list(ROUTE_PROBES)
    while pending and time.monotonic() < deadline:
        still = []
        for method, path, expected in pending:
            try:
                status = session.request(method, f"{BASE}{path}", json={}, timeout=5).status_code
            except requests.RequestException:
                status = None
            if status not in expected:
                still.append((method, path, expected))
        pending = still
        if pending:
            time.sleep(1)
    if pending:
        print(f"routes not ready after {seconds:.0f}s: {[p for _, p, _ in pending]}")
        return False
    print(f"all public routes answering after {time.monotonic() - started:.1f}s")
    return True


results: list[tuple[bool, str]] = []


def check(condition: bool, description: str, response: requests.Response | None = None):
    results.append((condition, description))
    detail = ""
    if not condition and response is not None:
        detail = f"  (got HTTP {response.status_code}: {response.text[:120]!r})"
    print(f"[{'PASS' if condition else 'FAIL'}] {description}{detail}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait-seconds", type=float, default=0)
    args = parser.parse_args()

    s = requests.Session()
    if args.wait_seconds and not wait_for_routes(s, args.wait_seconds):
        return 1

    r = s.post(
        f"{BASE}/auth/login", json={"username": "demo", "password": "wrong"}, timeout=TIMEOUT
    )
    check(r.status_code == 401, "wrong password is rejected (401)", r)

    r = s.post(
        f"{BASE}/auth/login",
        json={"username": "demo", "password": demo_password()},
        timeout=TIMEOUT,
    )
    check(r.status_code == 200, "login succeeds and returns a token", r)
    if r.status_code != 200:
        return 1
    auth = {"Authorization": f"Bearer {body(r)['access_token']}"}

    r = s.get(f"{BASE}/catalog/products", timeout=TIMEOUT)
    check(
        r.status_code == 200 and len(body(r).get("products", [])) >= 4, "catalog lists products", r
    )

    r = s.post(
        f"{BASE}/orders",
        json={"items": [{"product_id": "mug", "quantity": 1}], "card_token": "tok_visa"},
        timeout=TIMEOUT,
    )
    check(r.status_code == 401, "ordering without a token is rejected (401)", r)

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
    order = body(r).get("order", {})
    check(
        r.status_code == 201 and order.get("status") == "CONFIRMED",
        "order is placed and paid across orders -> catalog -> payments",
        r,
    )
    check(order.get("total_cents") == 2 * 1499 + 4999, "total is computed from catalog prices")
    check(order.get("notification_sent") is True, "confirmation notification was sent")
    check(r.headers.get("X-Request-ID") == trace_id, f"request id {trace_id} is propagated")

    r = s.get(f"{BASE}/orders/{order.get('id')}", headers=auth, timeout=TIMEOUT)
    check(
        r.status_code == 200 and body(r).get("order", {}).get("id") == order.get("id"),
        "order can be read back (state shared via Redis)",
        r,
    )

    r = s.post(
        f"{BASE}/orders",
        json={**order_body, "card_token": "tok_decline"},
        headers=auth,
        timeout=TIMEOUT,
    )
    check(
        r.status_code == 402 and body(r).get("order", {}).get("status") == "PAYMENT_DECLINED",
        "declined card is handled (402, PAYMENT_DECLINED)",
        r,
    )

    r = s.get(f"{BASE}/notifications", headers=auth, timeout=TIMEOUT)
    subjects = [n["subject"] for n in body(r).get("notifications", [])]
    check(
        any(order.get("id", "?") in subj for subj in subjects),
        "notification for the order is visible",
        r,
    )

    r = s.post(f"{BASE}/payments", json={}, headers=auth, timeout=TIMEOUT)
    check(r.status_code == 404, "payments service is NOT exposed through the gateway", r)

    spoof = {"subject": "spoof", "body": ""}
    r = s.post(f"{BASE}/notifications", json=spoof, headers=auth, timeout=TIMEOUT)
    check(r.status_code == 405, "public /notifications is read-only (405 for POST)", r)
    r = s.post(f"{BASE}/internal/notifications", json=spoof, headers=auth, timeout=TIMEOUT)
    check(r.status_code == 404, "internal endpoints are NOT exposed (/internal/* -> 404)", r)

    failed = [d for ok, d in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed against {BASE}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
