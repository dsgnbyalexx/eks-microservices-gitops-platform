"""Notifications service: SIMULATED email. Messages are stored per user in Redis
(newest first, last 100 kept) instead of being sent.

  GET  /notifications           public: a user reads their own messages
  POST /internal/notifications  internal: only other services (orders) create them.
                                The gateway/Ingress never routes /internal/*, so this
                                separation works with any ingress controller.
"""

import json
import uuid
from datetime import UTC, datetime

from flask import abort, g, jsonify, request

from common.app_factory import create_app
from common.auth import require_auth
from common.store import redis_from_env

KEEP_LAST = 100


def create(redis_client=None):
    store = redis_client or redis_from_env()
    app = create_app("notifications", readiness_check=lambda: store.ping())

    @app.post("/internal/notifications")
    @require_auth
    def send():
        body = request.get_json(silent=True) or {}
        subject, text = body.get("subject"), body.get("body")
        if not isinstance(subject, str) or not subject or len(subject) > 200:
            abort(400, description="subject is required (max 200 chars)")
        if not isinstance(text, str) or len(text) > 2000:
            abort(400, description="body must be text (max 2000 chars)")
        message = {
            "id": f"ntf_{uuid.uuid4().hex[:16]}",
            "to": g.user,
            "subject": subject,
            "body": text,
            "sent_at": datetime.now(UTC).isoformat(),
            "simulated": True,
        }
        key = f"notifications:{g.user}"
        store.lpush(key, json.dumps(message))
        store.ltrim(key, 0, KEEP_LAST - 1)
        return jsonify(notification=message), 202

    @app.get("/notifications")
    @require_auth
    def recent():
        items = store.lrange(f"notifications:{g.user}", 0, 19)
        return jsonify(notifications=[json.loads(i) for i in items])

    return app
