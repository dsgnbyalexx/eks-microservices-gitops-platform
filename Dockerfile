# syntax=docker/dockerfile:1
# One Dockerfile for all five services:
#   docker build --build-arg SERVICE=orders -t shop/orders .

# ---- Stage 1: install dependencies into a virtual environment ----
FROM python:3.14-slim AS build
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
 && /opt/venv/bin/pip uninstall -y pip

# ---- Stage 2: small runtime image ----
FROM python:3.14-slim
ARG SERVICE
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Fail the build early if SERVICE was not given.
RUN test -n "$SERVICE" || (echo "Build with --build-arg SERVICE=<name>" && exit 1)

# pip is not needed at runtime (dependencies were installed in stage 1), so it is
# removed. A fixed numeric user id lets Kubernetes verify runAsNonRoot.
RUN rm -rf /usr/local/lib/python3.14/site-packages/pip \
           /usr/local/lib/python3.14/site-packages/pip-* \
           /usr/local/bin/pip* \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app

COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY services/common ./common
COPY services/${SERVICE} ./service
RUN python -c "import service.app"   # fail the build if the service code does not import

USER 10001
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]

# gunicorn = production web server (Flask's built-in server is for development).
# One worker process with 4 threads: low memory per pod, and Prometheus metrics stay
# accurate (each process would otherwise keep its own separate counters).
# Scaling is done by adding pods, not workers.
CMD ["gunicorn", "--bind=0.0.0.0:8000", "--worker-class=gthread", "--workers=1", "--threads=4", \
     "--timeout=30", "--graceful-timeout=20", "--worker-tmp-dir=/tmp", "service.app:create()"]
