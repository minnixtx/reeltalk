"""Review and reply length caps (REVIEW_LIMITS_PLAN.md, increment RL1).

Every refusal here is asserted through the route, not the widget: the
``maxlength`` attribute is browser courtesy and a direct POST never sees it,
so the server-side check is the wall under test. The over-limit cases also
assert nothing was written - a refusal that saved a truncated row would be
the silent cut the owner ruled against (D2).
"""

import pytest
from django.test import Client

from reeltalk.core.models import (
    REPLY_MAX_LENGTH,
    REVIEW_MAX_LENGTH,
    Film,
    Status,
    add_reply,
    mark_watched,
)
from reeltalk.tests.members import member

REVIEW_MSG = f"{REVIEW_MAX_LENGTH:,}"
REPLY_MSG = f"{REPLY_MAX_LENGTH:,}"


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def user(db):
    return member(localname="alice", password="s3cretpass")


@pytest.fixture
def login(client, user):
    assert client.login(username="alice", password="s3cretpass")
    return client


@pytest.fixture
def film(db):
    return Film.objects.create(
        title="Dune",
        year=2021,
        runtime=155,
        genres=["Sci-Fi"],
        directors=["Denis Villeneuve"],
        description="<p>A desert epic.</p>",
    )


def review(user, film, rating="4.5", content="<p>Great.</p>"):
    return Status.objects.create(
        user=user,
        film=film,
        status_type=Status.Type.REVIEW,
        rating=rating,
        content=content,
    )


# --- model layer: the wall itself ------------------------------------------


@pytest.mark.django_db
def test_mark_watched_refuses_over_limit_and_writes_nothing(user, film):
    text = "x" * (REVIEW_MAX_LENGTH + 1)
    with pytest.raises(ValueError):
        mark_watched(user, film, rating="4", content=text, raw_content=text)
    assert not Status.objects.filter(user=user, film=film).exists()


@pytest.mark.django_db
def test_mark_watched_accepts_exactly_at_limit(user, film):
    text = "x" * REVIEW_MAX_LENGTH
    status = mark_watched(user, film, rating="4", content=text, raw_content=text)
    assert status.raw_content == text


@pytest.mark.django_db
def test_add_reply_refuses_over_limit_and_writes_nothing(user, film):
    parent = review(user, film)
    text = "y" * (REPLY_MAX_LENGTH + 1)
    with pytest.raises(ValueError):
        add_reply(user, parent, content=text, raw_content=text)
    assert not Status.objects.filter(reply_parent=parent).exists()


@pytest.mark.django_db
def test_add_reply_accepts_exactly_at_limit(user, film):
    parent = review(user, film)
    text = "y" * REPLY_MAX_LENGTH
    reply = add_reply(user, parent, content=text, raw_content=text)
    assert reply.raw_content == text


# --- routes: refusal surfaces as a message, on create and on edit ----------


@pytest.mark.django_db
def test_review_at_limit_saves(login, user, film):
    text = "x" * REVIEW_MAX_LENGTH
    resp = login.post(f"/film/{film.id}/watched/", {"rating": "4", "content": text})
    assert resp.status_code == 302
    entry = Status.objects.get(user=user, film=film)
    assert entry.raw_content == text


@pytest.mark.django_db
def test_review_over_limit_is_refused_with_a_message(login, user, film):
    text = "x" * (REVIEW_MAX_LENGTH + 1)
    resp = login.post(
        f"/film/{film.id}/watched/", {"rating": "4", "content": text}, follow=True
    )
    assert resp.redirect_chain[0][0] == f"/film/{film.id}/"
    assert not Status.objects.filter(user=user, film=film).exists()
    # The refusal is what the member reads on the film page. Read off the
    # followed response's context: on this route the queued message surfaces
    # there, not on the redirect response itself - the same read the browser
    # actually gets, and the pattern the verification-gate tests use.
    messages = [str(m) for m in resp.context["messages"]]
    assert any(REVIEW_MSG in m for m in messages)


@pytest.mark.django_db
def test_review_edit_over_limit_keeps_the_original_text(login, user, film):
    login.post(f"/film/{film.id}/watched/", {"rating": "4", "content": "Original text"})
    entry = Status.objects.get(user=user, film=film)
    text = "x" * (REVIEW_MAX_LENGTH + 1)
    resp = login.post(
        f"/film/{film.id}/watched/", {"rating": "4", "content": text}, follow=True
    )
    entry.refresh_from_db()
    assert entry.raw_content == "Original text"
    assert entry.rating == 4
    messages = [str(m) for m in resp.context["messages"]]
    assert any(REVIEW_MSG in m for m in messages)


@pytest.mark.django_db
def test_reply_at_limit_saves(login, user, film):
    parent = review(user, film)
    text = "y" * REPLY_MAX_LENGTH
    resp = login.post(f"/status/{parent.pk}/reply/", {"content": text})
    assert resp.status_code == 200
    assert Status.objects.filter(reply_parent=parent).count() == 1


@pytest.mark.django_db
def test_reply_over_limit_is_refused_with_a_message(login, user, film):
    parent = review(user, film)
    text = "y" * (REPLY_MAX_LENGTH + 1)
    resp = login.post(f"/status/{parent.pk}/reply/", {"content": text})
    assert resp.status_code == 400
    assert REPLY_MSG in resp.json()["error"]
    assert not Status.objects.filter(reply_parent=parent).exists()


# --- widgets: the textareas carry the same bound the server enforces -------


@pytest.mark.django_db
def test_review_composer_carries_maxlength(login, film):
    body = login.get(f"/film/{film.id}/").content.decode()
    assert f'maxlength="{REVIEW_MAX_LENGTH}"' in body


@pytest.mark.django_db
def test_reply_composer_carries_maxlength(login, user, film):
    parent = review(user, film)
    # The composer only renders behind the reply flag — the same one the
    # feed row's reply icon links with (?reply=1).
    body = login.get(f"/status/{parent.pk}/?reply=1").content.decode()
    assert f'maxlength="{REPLY_MAX_LENGTH}"' in body
