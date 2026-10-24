import secrets
from datetime import timedelta

from common.auth import issue_token

# Generated per test run: no secret-looking strings live in the repository.
TEST_SECRET = secrets.token_urlsafe(32)
DEMO_PASSWORD = secrets.token_urlsafe(16)


def bearer(user: str = "demo", ttl: timedelta = timedelta(minutes=5)) -> dict:
    return {"Authorization": f"Bearer {issue_token(user, ttl)}"}
