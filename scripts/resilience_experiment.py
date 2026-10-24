"""Resilience experiment on the Docker Compose stack: stop one dependency at a
time and check that orders degrade the way the design says they should.

    python scripts/resilience_experiment.py

Expected:
  * notifications down -> order still CONFIRMED (201), notification_sent = false
  * payments down      -> order recorded as PAYMENT_ERROR (503), after retries
  * both back          -> normal CONFIRMED orders again
"""

import subprocess
import sys
import time

import requests

from smoke_test import BASE, demo_password

TIMEOUT = 20


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], check=True, capture_output=True)  # noqa: S603, S607


def wait_healthy(service: str, seconds: int = 60) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        state = subprocess.run(  # noqa: S603
            ["docker", "compose", "ps", service, "--format", "{{.Health}}"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if state == "healthy":
            return
        time.sleep(2)
    raise TimeoutError(f"{service} did not become healthy")


def place_order(token: str) -> tuple[int, dict, float]:
    started = time.perf_counter()
    r = requests.post(
        f"{BASE}/orders",
        json={"items": [{"product_id": "tee", "quantity": 1}], "card_token": "tok_visa"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    )
    try:
        order = r.json().get("order", {})
    except ValueError:  # e.g. an HTML 504 page from the gateway
        order = {"status": f"(non-JSON {r.headers.get('Content-Type', '?')} response)"}
    return r.status_code, order, (time.perf_counter() - started) * 1000


def report(label: str, status: int, order: dict, ms: float) -> None:
    print(
        f"  {label:<24} HTTP {status}  status={order.get('status')}  "
        f"notification_sent={order.get('notification_sent')}  ({ms:.0f} ms)"
    )


def main() -> int:
    token = requests.post(
        f"{BASE}/auth/login",
        json={"username": "demo", "password": demo_password()},
        timeout=TIMEOUT,
    ).json()["access_token"]
    outcomes = []

    print("A. notifications service stopped")
    compose("stop", "-t", "5", "notifications")
    try:
        status, order, ms = place_order(token)
        report("order:", status, order, ms)
        outcomes.append(
            status == 201
            and order.get("status") == "CONFIRMED"
            and order.get("notification_sent") is False
        )
    finally:
        compose("start", "notifications")

    print("B. payments service stopped")
    compose("stop", "-t", "5", "payments")
    try:
        status, order, ms = place_order(token)
        report("order:", status, order, ms)
        outcomes.append(status == 503 and order.get("status") == "PAYMENT_ERROR")
    finally:
        compose("start", "payments")

    print("C. both services restarted")
    wait_healthy("notifications")
    wait_healthy("payments")
    status, order, ms = place_order(token)
    report("order:", status, order, ms)
    outcomes.append(
        status == 201 and order.get("status") == "CONFIRMED" and order.get("notification_sent")
    )

    print(f"\n{sum(outcomes)}/{len(outcomes)} scenarios behaved as designed")
    return 0 if all(outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
