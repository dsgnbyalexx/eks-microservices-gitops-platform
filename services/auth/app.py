"""Auth service: checks a username/password and returns a signed JWT."""

import os

from flask import abort, g, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

from common.app_factory import create_app
from common.auth import issue_token, require_auth, secret_key

MIN_PASSWORD_LENGTH = 12
TOKEN_TTL_SECONDS = 900
# Checked when the username is unknown, so a wrong username and a wrong
# password take the same time (no hint about which usernames exist).
_DUMMY_HASH = generate_password_hash("placeholder-password-for-timing")


def create():
    secret_key()  # fail at startup, not on the first login, if the secret is missing
    username = os.environ.get("DEMO_USERNAME", "demo")
    password = os.environ.get("DEMO_PASSWORD", "")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise RuntimeError(f"DEMO_PASSWORD must be set and at least {MIN_PASSWORD_LENGTH} chars")
    users = {username: generate_password_hash(password)}  # store hashes, never plain text

    app = create_app("auth")

    @app.post("/auth/login")
    def login():
        body = request.get_json(silent=True) or {}
        user, given = body.get("username"), body.get("password")
        if not isinstance(user, str) or not isinstance(given, str):
            abort(400, description="username and password are required")
        stored = users.get(user)
        password_ok = check_password_hash(stored or _DUMMY_HASH, given)
        if stored is None or not password_ok:
            abort(401, description="invalid credentials")
        return jsonify(
            access_token=issue_token(user),
            token_type="Bearer",  # noqa: S106 - the OAuth token type name, not a secret
            expires_in=TOKEN_TTL_SECONDS,
        )

    @app.get("/auth/me")
    @require_auth
    def me():
        return jsonify(username=g.user)

    return app
