# Evidence log

Every result here was produced by running the commands shown, locally on Windows 11 with
Docker Desktop. **Nothing in this folder comes from a cloud environment or Kubernetes yet.**

---

## 2026-10-24: Phase 1, five services on Docker Compose

### Automated tests (measured)

| Suite | Tests | Result | Raw output |
|---|---|---|---|
| Shared library: health/readiness, request ids, metrics, JWT checks, HTTP client | 18 | All passed | [tests.txt](tests.txt) |
| auth, catalog, notifications | 14 | All passed | [tests.txt](tests.txt) |
| payments (idempotency, declines, validation, race) | 19 | All passed | [tests.txt](tests.txt) |
| orders (orchestration and every failure path, retries, deadline) | 16 | All passed | [tests.txt](tests.txt) |
| **Total** | **67** | **All passed, 95% line coverage** | [tests.txt](tests.txt) |
| Lint and formatting (ruff) | — | No issues | [lint.txt](lint.txt) |

*This table records Phase 1 (67 tests). [tests.txt](tests.txt) is regenerated on each
phase and now shows 68: Phase 2 added a test that the public notifications path is
read-only.*

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

---

## 2026-10-24: Phase 2, Helm chart on local Kubernetes (kind)

Environment: kind v0.33.0, single node, **Kubernetes v1.37.0**, Traefik v3.7.16 (chart
42.1.3) as the ingress controller, on a Docker VM with ~2.8 GB RAM.

### Validation and deployment (measured)

| Check | Result | Raw output |
|---|---|---|
| `helm lint --strict` (default and kind values) | Pass | [k8s-validation.txt](k8s-validation.txt) |
| kubeconform, strict, against Kubernetes 1.37 schemas | 25/25 objects valid | [k8s-validation.txt](k8s-validation.txt) |
| Install into a namespace enforcing Pod Security **restricted** | All pods admitted and Running | [k8s-cluster-state.txt](k8s-cluster-state.txt) |
| Smoke test through the Traefik Ingress | **14/14 checks** | [smoke-test-k8s.txt](smoke-test-k8s.txt) |
| Unit tests after the `/internal/notifications` change | 68/68 | — |

### Experiments (measured, [raw output](k8s-experiments.txt))

| Experiment | Result |
|---|---|
| **Self-healing:** delete an orders pod | Replaced and serving 2/2 again in ~9 s |
| **NetworkPolicy:** pod-to-pod connections | orders→payments and orders→redis allowed; catalog→payments, auth→payments, catalog→redis **blocked**. kind's network plugin enforces policies |
| **Pod Security Admission:** create a pod that does not meet "restricted" | **Rejected** by the API server |
| **Redis outage:** readiness vs liveness | orders/payments/notifications became **not ready** (out of traffic) with **0 restarts**; catalog kept serving (200); orders returned a clean 503; full recovery with 0 restarts |

### Experiment: does a rolling update drop requests, and does the preStop pause matter?

**Method:** [`scripts/rollout_experiment.sh`](../../scripts/rollout_experiment.sh) sends 75 s of
traffic (10 request-pairs per second, through the Ingress) while `orders` and `catalog` are
restarted. Each run records the ingress controller's restart count and the node's memory
pressure, and marks itself INVALID if the controller restarted.

| `preStop` sleep | Requests | Failed | Success | Valid? |
|---|---|---|---|---|
| **0 s (control)** | 1,490 | **16** (all HTTP 502) | 98.93% | ✅ |
| **5 s (chart default)** | 1,442 | **0** | **100.00%** | ✅ |

Raw: [rollout-experiment.txt](rollout-experiment.txt). Without the pause, a pod begins
shutting down while the ingress still routes to it for a moment. **One run each:** this
shows the effect, not a precise failure rate.

### Finding: overloaded lab machine invalidated the first experiment runs

Earlier attempts gave misleading results, and they are **not** used above:

- The ingress controller entered **CrashLoopBackOff** (failed liveness probe) during
  back-to-back rollouts. The Docker VM had **36 MB free RAM, 407 MB swap in use, and
  memory-pressure stalls of 15%** (5-minute average).
- An early control run showed 130 failures, but the ingress controller may already have
  been failing, so that number was **discarded**.

**Fixes:**

- [`values-kind.yaml`](../../helm/shop/values-kind.yaml) runs 1 replica of the services not
  under test. The production-like defaults keep 2.
- The experiment script now guards against a restarting ingress controller.

### Finding: `helm upgrade` silently reuses earlier `--set` values

After a temporary `--set defaults.preStopSleepSeconds=0`, a plain `helm upgrade` kept
`sleep 0`. With no new values supplied, Helm reuses the previous release's values.
`--reset-values` restores the chart defaults, and the experiment script always uses it.

### What this does NOT prove yet

- Multi-node behaviour (pod spreading, PodDisruptionBudgets during node drains): kind runs
  a single node here.
- Performance under load: no load test beyond 20 requests/second.
- Anything about EKS, Argo CD, or Prometheus/Grafana: not built yet.
