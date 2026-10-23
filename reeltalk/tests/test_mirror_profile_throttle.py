"""The mirror profile-refresh throttle is a Postgres table, not a cache (F12c).

``refresh_mirror_profile`` fetches a mirror's home Person document to fill a
missing avatar or bio. That fetch must happen at most once per window per
actor, or a page view of a mirror whose home instance never fills those fields
becomes an unthrottled outbound-fetch loop. The throttle used to live in
LocMemCache — per-process and cleared on every deploy — so N workers each
fetched once per window and a restart reopened every window at once. It is now
one row per actor in Postgres. These tests pin exactly the properties the cache
could not give, so a change that quietly moved it back to a cache would fail.
"""

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone

from reeltalk.activitypub import mirrors
from reeltalk.activitypub.mirrors import (
    PROFILE_REFRESH_THROTTLE,
    RemoteFetchError,
    refresh_mirror_profile,
)
from reeltalk.activitypub.models import MirrorProfileAttempt
from reeltalk.social.models import User

CAROL = "https://peer.example/users/carol/"
MALLORY = "https://mallory.example/users/mallory/"


def _mirror(localname, actor_url):
    """A remote member missing both avatar and summary, so a refresh wants to fetch."""
    user = User(
        localname=localname,
        local=False,
        actor_url=actor_url,
        inbox_url=f"{actor_url}inbox",
        public_key="not-a-real-key",
    )
    user.set_unusable_password()
    user.save()
    return user


@pytest.fixture
def fetches(monkeypatch):
    """The outbound Person fetch replaced by a per-actor call log.

    Counting calls is the whole point: the throttle's job is to keep a second
    page view from dialling out at all, so every assertion here is on the fetch
    count rather than on what a fetched document would have filled in.
    """
    calls = []

    def fake_fetch(actor_url):
        calls.append(actor_url)
        return {}  # fills nothing — the throttle, not the fill, is under test

    monkeypatch.setattr(mirrors, "fetch_person_document", fake_fetch)
    return calls


@pytest.mark.django_db
def test_a_second_view_within_the_window_does_not_fetch(fetches):
    mirror = _mirror("carol@peer.example", CAROL)
    refresh_mirror_profile(mirror)
    refresh_mirror_profile(mirror)
    assert fetches == [CAROL]


@pytest.mark.django_db
def test_the_throttle_is_per_actor(fetches):
    carol = _mirror("carol@peer.example", CAROL)
    mallory = _mirror("mallory@peer.example", MALLORY)
    refresh_mirror_profile(carol)
    refresh_mirror_profile(mallory)
    # Throttling Carol must not silence Mallory's first-ever fetch.
    assert fetches == [CAROL, MALLORY]


@pytest.mark.django_db
def test_the_throttle_survives_a_cache_clear(fetches):
    # The regression F12c is about. The window used to live in LocMemCache, so a
    # deploy — which clears it — reopened every window at once. Clearing the
    # cache between two views must NOT reopen the window now that it is a row.
    mirror = _mirror("carol@peer.example", CAROL)
    refresh_mirror_profile(mirror)
    cache.clear()
    refresh_mirror_profile(mirror)
    assert fetches == [CAROL]


@pytest.mark.django_db
def test_the_window_reopens_once_it_elapses(fetches):
    mirror = _mirror("carol@peer.example", CAROL)
    refresh_mirror_profile(mirror)
    # Push the recorded attempt back past the window, as the clock passing would.
    MirrorProfileAttempt.objects.update(
        attempted_at=timezone.now() - timedelta(seconds=PROFILE_REFRESH_THROTTLE + 1)
    )
    refresh_mirror_profile(mirror)
    assert len(fetches) == 2


@pytest.mark.django_db
def test_a_failing_remote_still_counts_against_the_window(fetches, monkeypatch):
    # Mark-before-fetch ordering, kept from the cache version on purpose: a home
    # instance that errors must not be re-dialled on every page view. The attempt
    # is recorded before the fetch, so the second view is throttled even though
    # nothing was ever filled.
    def boom(actor_url):
        calls = fetches
        calls.append(actor_url)
        raise RemoteFetchError("unreachable")

    monkeypatch.setattr(mirrors, "fetch_person_document", boom)
    mirror = _mirror("carol@peer.example", CAROL)
    refresh_mirror_profile(mirror)
    refresh_mirror_profile(mirror)
    assert fetches == [CAROL]


@pytest.mark.django_db
def test_an_actor_with_no_address_never_reaches_the_throttle(fetches):
    # The early return for a missing actor_url must fire before the throttle, so
    # no row is ever keyed on the empty string. A shared empty key would let one
    # malformed address-less mirror silence every other one's refresh.
    mirror = _mirror("carol@peer.example", "")
    refresh_mirror_profile(mirror)
    assert fetches == []
    assert not MirrorProfileAttempt.objects.exists()
