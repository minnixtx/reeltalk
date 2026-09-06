# ReelTalk image — one image serves the web role (uvicorn) and, from M2 on,
# the worker role (Django-Q2 qcluster). See PLAN.md §3.8/§3.9 for the stack shape.

FROM python:3.13-slim AS build

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Lockfile-first copy so dependency layers cache across code-only changes.
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir "uv==0.12.10" \
    && uv sync --frozen

FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

ENV PYTHONUNBUFFERED=1
# The venv was built against the same base image, so its python path resolves.
ENV PATH=/app/.venv/bin:$PATH

COPY --from=build /app/.venv /app/.venv
COPY entrypoint.sh /entrypoint.sh
COPY . /app/

# Ensure volume mount points exist and are owned by the runtime user before
# first use (named volumes inherit permissions from the image on init).
RUN mkdir -p /app/static /app/images \
    && python -m compileall /app/reeltalk /app/manage.py \
    && chown -R appuser:appuser /app

VOLUME ["/app/static", "/app/images"]
USER appuser

# Entrypoint runs migrations + collectstatic for the web role on start.
ENTRYPOINT ["/entrypoint.sh"]
