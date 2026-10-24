#!/usr/bin/env bash
# Does a rolling update drop requests? Runs steady traffic through the ingress
# while orders and catalog are restarted, for a given preStop pause.
#
#   scripts/rollout_experiment.sh 5    # the chart default
#   scripts/rollout_experiment.sh 0    # control: no pause before shutdown
#
# Guardrail: if the ingress controller restarts during the run (e.g. the machine
# runs out of memory), the run is reported INVALID instead of being counted.
set -euo pipefail

PRESTOP="${1:?usage: rollout_experiment.sh <preStopSleepSeconds>}"
NS=shop
PY="${PYTHON:-.venv/Scripts/python}"
export SMOKE_BASE_URL="${SMOKE_BASE_URL:-http://127.0.0.1:18080}"

traefik_restarts() {
  kubectl -n traefik get pods -l app.kubernetes.io/name=traefik \
    -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}'
}
memory_pressure() {
  # Pressure Stall Information may not exist on every kernel; report n/a then.
  docker exec shop-control-plane sh -c 'grep some /proc/pressure/memory | cut -d" " -f2' 2>/dev/null \
    || echo "n/a"
}

helm upgrade shop helm/shop -n "$NS" --reset-values -f helm/shop/values-kind.yaml \
  --set defaults.preStopSleepSeconds="$PRESTOP" --wait --timeout 5m >/dev/null
for _ in $(seq 1 90); do
  [ "$(kubectl -n "$NS" get pods --no-headers | grep -c Terminating || true)" = 0 ] && break
  sleep 2
done
sleep 45   # let the machine settle after the upgrade

before=$(traefik_restarts); pressure_before=$(memory_pressure)
"$PY" scripts/availability_probe.py --seconds 75 --rate 10 < /dev/null > /tmp/probe.out 2>&1 &
probe=$!
sleep 10
kubectl -n "$NS" rollout restart deploy/orders deploy/catalog >/dev/null
kubectl -n "$NS" rollout status deploy/orders --timeout=150s >/dev/null
kubectl -n "$NS" rollout status deploy/catalog --timeout=150s >/dev/null
wait "$probe" || true
after=$(traefik_restarts); pressure_after=$(memory_pressure)

verdict="VALID"
[ "$before" != "$after" ] && verdict="INVALID (ingress controller restarted during the run)"
echo "preStop=${PRESTOP}s | $(tail -1 /tmp/probe.out) | traefik restarts ${before}->${after} | memory pressure ${pressure_before} -> ${pressure_after} | ${verdict}"
