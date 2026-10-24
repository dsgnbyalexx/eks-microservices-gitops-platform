"""JSON Web Tokens (JWT). The auth service SIGNS tokens; every other service
VERIFIES them locally with the shared secret, so no service has to call auth on
every request (stateless authentication).

HS256 with a shared secret keeps the demo simple. A production system would
use an asymmetric algorithm (RS256/ES256): only auth holds the private key, and
other services verify with the public key, so they could not forge tokens.
"""

import os
from datetime import UTC, datetime, timedelta
from functools import wraps

import jwt
from flask import abort, g, request

ISSUER = "auth-service"
AUDIENCE = "shop"
ALGORITHM = "HS256"
MIN_SECRET_LENGTH = 32


def secret_key() -> str:
    key = os.environ.get("AUTH_SECRET_KEY", "")
    if len(key) < MIN_SECRET_LENGTH:
        raise RuntimeError(f"AUTH_SECRET_KEY must be set and at least {MIN_SECRET_LENGTH} chars")
    return key


def issue_token(username: str, ttl: timedelta = timedelta(minutes=15)) -> str:
    now = datetime.now(UTC)
    claims = {"sub": username, "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + ttl}
    return jwt.encode(claims, secret_key(), algorithm=ALGORITHM)


def verify_token(token: str) -> dict:
    # algorithms= is an allowlist: it blocks "alg: none" and algorithm-confusion attacks.
    return jwt.decode(
        token,
        secret_key(),
        algorithms=[ALGORITHM],
        audience=AUDIENCE,
        issuer=ISSUER,
        options={"require": ["exp", "iat", "sub"]},
    )


def require_auth(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            abort(401, description="missing bearer token")
        try:
            claims = verify_token(token)
        except jwt.ExpiredSignatureError:
            abort(401, description="token expired")
        except jwt.InvalidTokenError:
            abort(401, description="invalid token")
        g.user = claims["sub"]
        g.token = token
        return view(*args, **kwargs)

    return wrapper
