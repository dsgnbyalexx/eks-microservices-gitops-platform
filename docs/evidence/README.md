# Evidence log

Every result here was produced by running the commands shown, locally on Windows 11 with
Docker Desktop. **Nothing in this folder comes from a cloud environment or Kubernetes yet.**

---

## 2026-10-24: Phase 1, five services on Docker Compose

### Automated tests (measured)

| Suite | Tests | Result | Raw output |
|---|---|---|---|
| Shared library: health/readiness, request ids, metrics, JWT checks, HTTP client | 21 | All passed | [tests.txt](tests.txt) |
| auth, catalog, notifications | 15 | All passed | [tests.txt](tests.txt) |
| payments (idempotency, declines, validation, race) | 17 | All passed | [tests.txt](tests.txt) |
| orders (orchestration and every failure path, retries, deadline) | 14 | All passed | [tests.txt](tests.txt) |
| **Total** | **67** | **All passed, 95% line coverage** | [tests.txt](tests.txt) |
| Lint and formatting (ruff) | — | No issues | [lint.txt](lint.txt) |

The uncovered lines are mainly the real HTTP calls between services, which the unit tests
replace with fakes. The end-to-end runs below exercise them.

### Running stack (measured)

[compose-status.txt](compose-status.txt): Redis, five services, and an nginx gateway. Each
service uses about 50–80 MB of its 128 MB limit and runs as non-root user 10001 on a
read-only root filesystem, with no pip in the image.

### End-to-end smoke test through the gateway (measured)

[smoke-test-compose.txt](smoke-test-compose.txt): **13/13 checks passed.** It covers login,
catalog, a paid order across four services, a declined card, the confirmation
notification, and the security boundaries (`/payments` is not exposed; notifications
cannot be created from outside).

### Distributed tracing (measured)

[request-trace.txt](request-trace.txt): one `X-Request-ID`, sent by the client, appears in
the logs of orders → catalog (×2) → payments → notifications.

### Experiment: does the system degrade gracefully when a dependency is down?

**Method:** [`scripts/resilience_experiment.py`](../../scripts/resilience_experiment.py)
stops real containers one at a time, places an order, and restarts them.

| Scenario | Expected | Measured result ([raw](resilience-experiment.txt)) |
|---|---|---|
| notifications stopped | Order still succeeds; `notification_sent=false` | ✅ 201 CONFIRMED |
| payments stopped | Order recorded as `PAYMENT_ERROR`; client gets a JSON 503 | ✅ 503 PAYMENT_ERROR |
| both restarted | Normal orders again | ✅ 201 CONFIRMED |

### Finding: the retry time exceeded the gateway timeout (found and fixed)

The first run of the payments-down scenario returned a **504 HTML page from the gateway**
instead of a JSON answer. Diagnosis, from logs and a direct measurement:

1. A DNS lookup for a *stopped* container takes **~4 s** to fail, and `requests`
   timeouts do **not** cover DNS resolution.
2. orders retried payments 3 times, **12.6 s** in total.
3. The gateway gave up at **10 s** and returned 504, so the client never learned the
   outcome, although orders had correctly saved `PAYMENT_ERROR`.

**Fix:** payment retries now share an overall **6-second deadline**, so no new attempt
starts once the deadline would be exceeded (covered by a unit test). The same scenario now
returns a JSON 503 in **8.2 s**. That is more than 6 s because an attempt already waiting on
DNS cannot be cut short; the worst case is the budget plus one attempt, which is still
inside the gateway's 10 s.

**Principle:** timeouts must shrink as calls go deeper. Each caller's timeout must be longer
than the total time its callees can take, retries included.

### What this does NOT prove yet

- Behaviour on Kubernetes (probes, rolling updates, multiple replicas): that is Phase 2.
- Performance under load: no load test has been run.
- Anything about EKS, Argo CD, or Prometheus/Grafana: not built yet.
