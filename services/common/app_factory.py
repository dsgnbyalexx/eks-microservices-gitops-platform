"""create_app() gives every service the same operational behaviour:

* /healthz  - liveness: "is the process alive?" Never checks dependencies, so a
              Redis outage does not make Kubernetes restart healthy pods.
* /readyz   - readiness: "should I receive traffic?" Checks this service's OWN
              dependencies (e.g. Redis), never other services, to avoid one
              failing service cascading into all of them being marked unready.
* /metrics  - Prometheus metrics (request counts and latency).
* X-Request-ID on every request and response, included in every log line.
* JSON errors; unexpected errors are logged but never leaked to the client.
"""

import time
import uuid
from collections.abc import Callable

from flask import Flask, Response, g, jsonify, request
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from werkzeug.exceptions import HTTPException

from common.logs import log

ALWAYS_READY: Callable[[], bool] = lambda: True  # noqa: E731


def create_app(service: str, readiness_check: Callable[[], bool] = ALWAYS_READY) -> Flask:
    app = Flask(service)
    app.config["SERVICE_NAME"] = service

    # A registry per app (not the global default) keeps tests independent.
    registry = CollectorRegistry()
    requests_total = Counter(
        "http_requests_total",
        "HTTP requests handled",
        ["service", "method", "endpoint", "status"],
        registry=registry,
    )
    latency = Histogram(
        "http_request_duration_seconds",
        "HTTP request latency",
        ["service", "endpoint"],
        registry=registry,
    )

    @app.before_request
    def _start():
        g.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        g.started = time.perf_counter()

    @app.after_request
    def _finish(response: Response) -> Response:
        # Label by route TEMPLATE (/orders/<order_id>), not the real path,
        # so each order id does not create a new metric series.
        endpoint = request.url_rule.rule if request.url_rule else "unmatched"
        elapsed = time.perf_counter() - g.get("started", time.perf_counter())
        response.headers["X-Request-ID"] = g.get("request_id", "")
        if endpoint not in ("/healthz", "/readyz", "/metrics"):
            requests_total.labels(service, request.method, endpoint, response.status_code).inc()
            latency.labels(service, endpoint).observe(elapsed)
            log(
                "INFO",
                "request",
                service=service,
                request_id=g.get("request_id"),
                method=request.method,
                path=request.path,
                status=response.status_code,
                duration_ms=round(elapsed * 1000, 1),
            )
        return response

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok", service=service)

    @app.get("/readyz")
    def readyz():
        try:
            ready = readiness_check()
        except Exception as err:  # a failing dependency check means "not ready"
            log("WARNING", "readiness check failed", service=service, error=str(err))
            ready = False
        if not ready:
            return jsonify(status="unavailable", service=service), 503
        return jsonify(status="ready", service=service)

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(registry), mimetype=CONTENT_TYPE_LATEST)

    @app.errorhandler(HTTPException)
    def _http_error(err: HTTPException):
        return jsonify(error=err.description), err.code

    @app.errorhandler(Exception)
    def _unexpected(err: Exception):
        log(
            "ERROR",
            "unhandled error",
            service=service,
            request_id=g.get("request_id"),
            error=f"{type(err).__name__}: {err}",
        )
        return jsonify(error="internal server error"), 500

    return app
