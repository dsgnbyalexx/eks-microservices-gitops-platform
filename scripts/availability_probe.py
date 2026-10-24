"""Send steady traffic and count failures, e.g. while a rolling update runs.

    python scripts/availability_probe.py --seconds 60 --rate 10

Each tick: GET /catalog/products and GET /orders/<id> (an order created at start,
so the request crosses Ingress -> orders -> Redis). Any non-2xx or network error
counts as a failure.
"""

import argparse
import sys
import time

import requests

from smoke_test import BASE, demo_password


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--rate", type=float, default=10, help="request pairs per second")
    args = parser.parse_args()

    s = requests.Session()

    def setup_call(method: str, path: str, **kwargs):
        # Setup is retried so a single connection blip cannot abort the experiment;
        # only the measured traffic below counts towards the result.
        for attempt in range(5):
            try:
                r = s.request(method, f"{BASE}{path}", timeout=15, **kwargs)
                r.raise_for_status()
                return r.json()
            except requests.RequestException, ValueError:
                if attempt == 4:
                    raise
                time.sleep(1)

    token = setup_call(
        "POST", "/auth/login", json={"username": "demo", "password": demo_password()}
    )["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    order_id = setup_call(
        "POST",
        "/orders",
        headers=auth,
        json={"items": [{"product_id": "mug", "quantity": 1}], "card_token": "tok_visa"},
    )["order"]["id"]

    total = failed = 0
    failures: dict[str, int] = {}
    interval = 1 / args.rate
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        tick = time.monotonic()
        for url in (f"{BASE}/catalog/products", f"{BASE}/orders/{order_id}"):
            total += 1
            try:
                r = s.get(url, headers=auth, timeout=5)
                ok = 200 <= r.status_code < 300
                reason = str(r.status_code)
            except requests.RequestException as err:
                ok, reason = False, type(err).__name__
            if not ok:
                failed += 1
                failures[reason] = failures.get(reason, 0) + 1
        time.sleep(max(0.0, interval - (time.monotonic() - tick)))

    rate = 100 * (total - failed) / total if total else 0
    print(
        f"requests: {total}  failed: {failed}  success: {rate:.2f}%  failures by reason: {failures}"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
