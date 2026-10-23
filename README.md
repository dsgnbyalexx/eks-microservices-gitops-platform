# Kubernetes Microservices Platform (EKS-ready, GitOps)

> Five lightweight Flask services (auth, catalog, orders, simulated payments,
> notifications) packaged with Docker and Helm, running on Kubernetes, and designed
> to move to Amazon EKS with Argo CD.

> **Status: work in progress.** This README separates what is built and tested from
> what is designed but not yet deployed. Nothing below is claimed until it has evidence
> in [`docs/evidence/`](docs/evidence/).

## Project status

| Capability | Status | Evidence |
|---|---|---|
| Five Flask services + Docker Compose | ⏳ Planned | — |
| Automated tests | ⏳ Planned | — |
| Helm chart on local Kubernetes (kind) with probes & resource limits | ⏳ Planned | — |
| GitHub Actions CI (lint, test, image build) | ⏳ Planned | — |
| Amazon EKS deployment | 🗺️ Future | — |
| Argo CD GitOps, Prometheus/Grafana, IRSA / Pod Identity | 🗺️ Future | — |

**Legend:** ✅ implemented & verified · 🧪 simulated locally · ⏳ planned for this phase · 🗺️ future enhancement

## License

[MIT](LICENSE)
