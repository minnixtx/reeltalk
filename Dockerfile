FROM python:3.11 AS build

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN python -m venv /venv
ENV PATH=/venv/bin:$PATH

COPY pyproject.toml /app/
# Dev group is installed on purpose for now so `docker compose run --rm web pytest`
# works without extra steps; split into a prod image before public deploy (PLAN.md §5 M6).
RUN pip install --upgrade "pip>=25.1.0" && pip install --group main --group dev

FROM python:3.11-slim
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libpq5 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
ENV PATH=/venv/bin:$PATH

COPY --from=build /venv /venv
COPY entrypoint.sh /entrypoint.sh
COPY . /app/

RUN python -m compileall /app/reeltalk /app/celerytalk /app/manage.py

VOLUME ["/app/static", "/app/images"]

# Entrypoint runs migrations + collectstatic when the web container starts.
ENTRYPOINT ["/entrypoint.sh"]
