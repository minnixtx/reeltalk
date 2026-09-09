"""Django-Q2 wrapper tests (M2 increment 3).

The worker itself is verified live against the compose stack; here we cover
the wiring — delegation to the plain catalog function, queueing a signed
package on the Postgres cluster, and end-to-end name resolution via a sync
run. django-q2 1.11 stores queued work in ``OrmQ`` (signed package) and only
creates a ``Task`` row when it is executed.
"""

import pytest
from django.test import override_settings
from django_q.models import OrmQ, SignedPackage, Task
from django_q.tasks import async_task

from reeltalk.core import tasks


def test_wrapper_delegates_to_catalog(monkeypatch):
    seen = {}

    def fake_backfill(film_ids):
        seen["ids"] = list(film_ids)
        return {"backfilled": len(seen["ids"])}

    monkeypatch.setattr("reeltalk.core.catalog.backfill_films", fake_backfill)
    result = tasks.backfill_film_ids([1, 2, 3])
    assert seen["ids"] == [1, 2, 3]
    assert result == {"backfilled": 3}


@pytest.mark.django_db
def test_enqueue_backfill_queues_a_signed_package():
    task_id = tasks.enqueue_backfill([7, 8])
    assert OrmQ.objects.count() == 1
    package = SignedPackage.loads(OrmQ.objects.get().payload)
    assert package["id"] == task_id
    assert package["name"] == "backfill"
    assert package["func"] == "reeltalk.core.tasks.backfill_film_ids"
    # args is the *args tuple — the id list is its single element.
    assert tuple(package["args"]) == ([7, 8],)
    # Not executed yet — a Task row only appears when it runs.
    assert Task.objects.count() == 0


@pytest.mark.django_db
@override_settings(TMDB_API_KEY="")
def test_async_task_resolves_wrapper_by_name():
    # The key is forced empty (compose run injects .env, so the real one may
    # be present): an empty batch is then a no-op returning the unconfigured
    # summary — but the run still proves name resolution, import, and result
    # recording on the Postgres cluster.
    task_id = async_task("reeltalk.core.tasks.backfill_film_ids", [], sync=True)
    orm_task = Task.objects.get(pk=task_id)
    assert orm_task.success is True
    assert orm_task.result == {"skipped_unconfigured": 0}
