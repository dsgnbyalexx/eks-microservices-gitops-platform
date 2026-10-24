from datetime import timedelta

from common.auth import issue_token

TEST_SECRET = "test-secret-key-that-is-long-enough-123"
DEMO_PASSWORD = "correct-horse-battery"


def bearer(user: str = "demo", ttl: timedelta = timedelta(minutes=5)) -> dict:
    return {"Authorization": f"Bearer {issue_token(user, ttl)}"}
