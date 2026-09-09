"""Django-Q2 task wrappers (M2, R4).

Thin wrappers that put ReelTalk's plain functions on the worker cluster. The
logic stays in the wrapped module (``catalog``) — directly testable without a
queue — and these wrappers exist so callers can enqueue by name with
``async_task("reeltalk.core.tasks.<name>", ...)``. django-q2 1.11 has no task
decorator; a plain importable function is all the cluster needs. The ``worker``
compose service runs ``qcluster`` against the Postgres cluster (Q_CLUSTER).
"""

from django_q.tasks import async_task

import reeltalk.core.catalog as catalog


def backfill_film_ids(film_ids) -> dict:
    """D11 import backfill on the worker cluster.

    See :func:`reeltalk.core.catalog.backfill_films` for the semantics — this
    wrapper adds nothing but queueability.
    """
    return catalog.backfill_films(film_ids)


def enqueue_backfill(film_ids):
    """Queue a D11 backfill batch on the cluster; returns the task id."""
    return async_task(
        "reeltalk.core.tasks.backfill_film_ids", list(film_ids), task_name="backfill"
    )
