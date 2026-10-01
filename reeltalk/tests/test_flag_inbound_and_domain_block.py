"""Inbound ``Flag``, the domain block, and the per-account refusal (increment 6).

Ordered by how each failure would bite:

* **The inbound report is keyed on the verified sender.** A ``Flag`` is a
  peer writing an accusation into our queue. The declared ``actor`` is a
  claim; the signature is the authority. A handler that recorded the
  declared actor would let any peer forge a report as anyone.
* **Only local targets are filed.** A report about somebody else's user is
  addressed to the wrong server, and filing it here is both useless work
  and a way to dump noise onto the wrong queue.
* **The domain block's two halves.** The door refuses the host, and the
  sweep hides the accounts we already hold. Each is tested independently,
  because the design deliberately does not let one stand in for the other.
* **The lift is scoped to what the block caused.** An unblock must restore
  the accounts this block suspended and nothing else — not an individually
  suspended account, not one held down by a different block.
* **Refusing a remote account is local.** Nothing is broadcast: we cannot
  sign another instance's actor document, and we have no business claiming
  their user is suspended.

Session-proof rules throughout: ``create_user()`` for every probe, a
``sessionid`` check on each authenticated position.
"""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory
from django.urls import reverse

from reeltalk.activitypub.flags import handle_flag
from reeltalk.activitypub.inbox import HANDLERS, process_inbound_activity
from reeltalk.activitypub.mirrors import (
    RemoteFetchError,
    mirror_user_from_person,
    resolve_known_actor,
    resolve_sender,
)
from reeltalk.core.models import Film, Status
from reeltalk.moderation.decorators import can_act_on
from reeltalk.moderation.models import (
    INBOUND_COMMENT_MAX,
    DomainBlock,
    Report,
    account_host_candidates,
    block_domain,
    blocked_domain_for,
    file_report,
    normalize_domain,
    unblock_domain,
)
from reeltalk.notifications.models import Notification
from reeltalk.social.models import SuspensionOrigin
from reeltalk.tests.members import member as create_member

User = get_user_model()
PASSWORD = "s3cretpass"

REMOTE_HOST = "far.example"
REMOTE_INBOX = "https://far.example/users/dana/inbox"
BLOCKED_HOST = "bad.example"


def logged_in(user):
    client = Client()
    client.force_login(user)
    return client


def request():
    return RequestFactory().post("/")


def mirror(localname="dana", host=REMOTE_HOST):
    return User.objects.create(
        localname=f"{localname}@{host}",
        local=False,
        actor_url=f"https://{host}/users/{localname}",
        inbox_url=f"https://{host}/users/{localname}/inbox",
    )


def local_review(author, title="Heat"):
    film = Film.objects.create(title=title, year=1995)
    return Status.objects.create(
        user=author,
        film=film,
        status_type="review",
        content="<p>good film</p>",
        raw_content="good film",
    )


def peer_sender():
    """The verified sender of an inbound Flag — a remote instance's actor."""
    return User.objects.create(
        localname="reporter@peer.example",
        local=False,
        actor_url="https://peer.example/users/reporter",
        inbox_url="https://peer.example/users/reporter/inbox",
    )


def flag_activity(target_uris, comment="this looks like spam", actor=None):
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": "https://peer.example/flags/1",
        "type": "Flag",
        "actor": actor or "https://peer.example/users/reporter",
        "content": comment,
        "object": target_uris,
    }
    return activity


@pytest.fixture
def member(db):
    return create_member(localname="member", password=PASSWORD)


@pytest.fixture
def mod(db):
    return create_member(localname="queue_mod", password=PASSWORD, is_moderator=True)


# --- registration ----------------------------------------------------------


def test_flag_is_registered_in_the_inbox_handlers():
    """Before this line a Flag was ignored gracefully, i.e. it vanished."""
    assert HANDLERS.get("Flag") is handle_flag


# --- inbound: the verified sender is the reporter ---------------------------


@pytest.mark.django_db
def test_a_peer_flag_about_a_member_lands_in_the_queue(member):
    sender = peer_sender()
    handle_flag(
        flag_activity(["https://peer.example/nope", "https://testserver/user/member/"]),
        sender,
        request(),
    )
    reports = Report.objects.filter(target_user=member)
    assert reports.count() == 1
    assert reports.first().reporter_id == sender.pk
    assert reports.first().resolved_at is None


@pytest.mark.django_db
def test_the_declared_actor_is_ignored(member):
    """A Flag naming a different actor still records the verified sender.

    The activity's ``actor`` field says one thing; the caller passed a
    different verified sender. The row must follow the signature.
    """
    sender = peer_sender()
    forged = "https://elsewhere.example/users/nobody"
    handle_flag(
        flag_activity(
            ["https://testserver/user/member/"],
            actor=forged,
        ),
        sender,
        request(),
    )
    report = Report.objects.get(target_user=member)
    assert report.reporter_id == sender.pk
    assert report.reporter.localname != "nobody"


@pytest.mark.django_db
def test_the_category_is_other_because_the_wire_carries_none(member):
    """Their Flag carries no category; inventing one is putting words in their mouth."""
    sender = peer_sender()
    handle_flag(flag_activity(["https://testserver/user/member/"]), sender, request())
    assert Report.objects.get(target_user=member).category == Report.Category.OTHER


@pytest.mark.django_db
def test_a_flag_about_a_local_post_files_against_the_post(member):
    status = local_review(member)
    sender = peer_sender()
    handle_flag(
        flag_activity([f"https://testserver/status/{status.pk}/"]), sender, request()
    )
    report = Report.objects.get(target_user=member)
    assert report.target_status_id == status.pk


@pytest.mark.django_db
def test_a_status_wins_over_its_author_in_the_same_flag(member):
    """One decision, not two work items, when a Flag names both."""
    status = local_review(member)
    sender = peer_sender()
    handle_flag(
        flag_activity(
            [
                "https://testserver/user/member/",
                f"https://testserver/status/{status.pk}/",
            ]
        ),
        sender,
        request(),
    )
    assert Report.objects.filter(target_user=member).count() == 1
    assert Report.objects.get(target_user=member).target_status_id == status.pk


@pytest.mark.django_db
def test_a_flag_about_a_remote_account_is_dropped():
    """Not ours to moderate, and filing it here is noise on the wrong queue."""
    remote = mirror()
    sender = peer_sender()
    detail = handle_flag(flag_activity([remote.actor_url]), sender, request())
    assert Report.objects.count() == 0
    assert "not ours" in detail


@pytest.mark.django_db
def test_a_flag_about_a_banned_member_is_skipped(member):
    member.ban(reason="already gone")
    sender = peer_sender()
    detail = handle_flag(
        flag_activity(["https://testserver/user/member/"]), sender, request()
    )
    assert Report.objects.count() == 0
    assert "banned" in detail


@pytest.mark.django_db
def test_the_inbound_comment_is_capped(member):
    """Their cap is 5,000 and a peer may relay text it did not write."""
    sender = peer_sender()
    handle_flag(
        flag_activity(
            ["https://testserver/user/member/"],
            comment="x" * (INBOUND_COMMENT_MAX + 500),
        ),
        sender,
        request(),
    )
    assert len(Report.objects.get(target_user=member).comment) == INBOUND_COMMENT_MAX


@pytest.mark.django_db
def test_a_flag_with_no_object_is_dropped(member):
    sender = peer_sender()
    detail = handle_flag(flag_activity([]), sender, request())
    assert Report.objects.count() == 0
    assert "no object" in detail


@pytest.mark.django_db
def test_a_flag_naming_nothing_we_hold_is_dropped(member):
    sender = peer_sender()
    detail = handle_flag(
        flag_activity(["https://nowhere.example/status/999"]), sender, request()
    )
    assert Report.objects.count() == 0
    assert "nothing named here" in detail


@pytest.mark.django_db
def test_a_non_string_content_does_not_become_a_traceback(member):
    """A peer may send any JSON. ``content`` being a dict must not crash the inbox."""
    sender = peer_sender()
    activity = flag_activity(["https://testserver/user/member/"])
    activity["content"] = {"unexpected": True}
    handle_flag(activity, sender, request())
    assert Report.objects.get(target_user=member).comment == ""


@pytest.mark.django_db
def test_the_pipeline_reports_flag_as_handled(member):
    sender = peer_sender()
    outcome = process_inbound_activity(
        flag_activity(["https://testserver/user/member/"]), sender, request()
    )
    assert outcome == "handled"
    assert Report.objects.filter(target_user=member).count() == 1


@pytest.mark.django_db
def test_an_inbound_flag_writes_no_notification(member):
    """R99 holds on the inbound path too."""
    sender = peer_sender()
    before = Notification.objects.count()
    handle_flag(flag_activity(["https://testserver/user/member/"]), sender, request())
    assert Notification.objects.count() == before


@pytest.mark.django_db
def test_a_peer_flag_survives_the_member_blocking_the_sending_server(member):
    """The R99 reason, tested from the other side.

    A member cannot keep a peer's report out of the queue by blocking the
    sending account, because the queue never consults ``notify()``.
    """
    sender = peer_sender()
    member.blocks.add(sender)
    handle_flag(flag_activity(["https://testserver/user/member/"]), sender, request())
    assert Report.objects.filter(target_user=member).count() == 1


# --- the domain block: matching -------------------------------------------


@pytest.mark.django_db
def test_normalize_domain_reduces_a_pasted_url_to_a_host():
    for pasted in [
        "https://Bad.Example/path",
        "http://bad.example/",
        "BAD.example",
        "  bad.example  ",
        "//bad.example",
        "https://user:pass@bad.example/x",
    ]:
        assert normalize_domain(pasted) == "bad.example", pasted


@pytest.mark.django_db
def test_a_parent_block_covers_subdomains():
    DomainBlock.objects.create(domain="bad.example")
    assert blocked_domain_for("anything.bad.example") is not None
    assert blocked_domain_for("deep.deep.bad.example") is not None
    assert blocked_domain_for("bad.example") is not None


@pytest.mark.django_db
def test_a_block_does_not_swallow_a_different_label():
    """``e.com`` must not block ``badexample.com``.

    A bare ``endswith`` would do exactly that, which is a way to block a
    server nobody meant to block by typing too little.
    """
    DomainBlock.objects.create(domain="e.com")
    assert blocked_domain_for("badexample.com") is None
    assert blocked_domain_for("e.com") is not None
    assert blocked_domain_for("sub.e.com") is not None


@pytest.mark.django_db
def test_the_longest_matching_block_wins():
    DomainBlock.objects.create(domain="bad.example")
    specific = DomainBlock.objects.create(domain="mail.bad.example")
    assert blocked_domain_for("mail.bad.example").pk == specific.pk
    assert blocked_domain_for("other.bad.example").domain == "bad.example"


@pytest.mark.django_db
def test_local_users_have_no_host_candidates(db):
    """A domain block is about somebody else's server, never our own."""
    local = create_member(localname="localone", password=PASSWORD)
    assert account_host_candidates(local) == set()


@pytest.mark.django_db
def test_a_mirror_matches_on_both_netloc_and_bare_host():
    """A peer on a non-default port carries the port (R52); either name works."""
    remote = User.objects.create(
        localname="dana@lan.example:3030",
        local=False,
        actor_url="https://lan.example:3030/users/dana",
    )
    hosts = account_host_candidates(remote)
    assert "lan.example:3030" in hosts
    assert "lan.example" in hosts
    DomainBlock.objects.create(domain="lan.example")
    assert blocked_domain_for("lan.example:3030") is not None


# --- the domain block: the sweep and the lift ------------------------------


@pytest.mark.django_db
def test_blocking_a_domain_suspends_its_mirrors_with_the_blocks_origin():
    blocked = mirror(localname="dana", host=BLOCKED_HOST)
    untouched = mirror(localname="innocent", host="fine.example")
    _block, suspended = block_domain(BLOCKED_HOST, reason="ZPROBE_BLOCK")
    blocked.refresh_from_db()
    untouched.refresh_from_db()
    assert blocked.suspended_at is not None
    assert blocked.suspension_origin == SuspensionOrigin.DOMAIN_BLOCK
    assert untouched.suspended_at is None
    assert [u.pk for u in suspended] == [blocked.pk]


@pytest.mark.django_db
def test_blocking_a_domain_covers_its_subdomain_mirrors():
    blocked = User.objects.create(
        localname="dana@mail.bad.example",
        local=False,
        actor_url="https://mail.bad.example/users/dana",
    )
    _block, suspended = block_domain("bad.example")
    blocked.refresh_from_db()
    assert blocked.suspended_at is not None
    assert len(suspended) == 1


@pytest.mark.django_db
def test_an_unblock_lifts_only_the_suspensions_that_block_imposed():
    mine = mirror(localname="dana", host=BLOCKED_HOST)
    theirs = mirror(localname="kept", host=BLOCKED_HOST)
    theirs.suspend(reason="individual decision")
    assert theirs.suspension_origin == SuspensionOrigin.LOCAL

    block, _suspended = block_domain(BLOCKED_HOST)
    mine.refresh_from_db()
    theirs.refresh_from_db()
    assert mine.suspension_origin == SuspensionOrigin.DOMAIN_BLOCK
    # The individually-suspended account keeps its own origin: suspend()
    # returned False, so the block never claimed it.
    assert theirs.suspension_origin == SuspensionOrigin.LOCAL

    unblock_domain(block)
    mine.refresh_from_db()
    theirs.refresh_from_db()
    assert mine.suspended_at is None
    assert theirs.suspended_at is not None


@pytest.mark.django_db
def test_an_unblock_does_not_lift_an_account_under_a_different_block():
    """Removing the outer block must not re-expose an inner block's account.

    Two overlapping blocks on the same host. Lifting the outer one clears
    what it swept — unless something that is *staying* still covers the
    host, in which case the account would come back visible here while the
    remaining rule still refuses it at the door.
    """
    sub = User.objects.create(
        localname="dana@mail.bad.example",
        local=False,
        actor_url="https://mail.bad.example/users/dana",
    )
    plain = User.objects.create(
        localname="gina@bad.example",
        local=False,
        actor_url="https://bad.example/users/gina",
    )
    parent, _ = block_domain("bad.example")
    sub.refresh_from_db()
    assert sub.suspension_origin == SuspensionOrigin.DOMAIN_BLOCK
    DomainBlock.objects.create(domain="mail.bad.example")

    _lifted_block, lifted = unblock_domain(parent)
    sub.refresh_from_db()
    plain.refresh_from_db()
    assert sub.suspended_at is not None
    assert plain.suspended_at is None
    assert [u.pk for u in lifted] == [plain.pk]
    assert blocked_domain_for("mail.bad.example") is not None


@pytest.mark.django_db
def test_blocking_a_domain_refuses_resolution_even_if_the_account_is_not_suspended():
    """The door is independent of the sweep.

    Unsuspending a mirror by hand must not reopen a blocked host.
    """
    blocked = mirror(localname="dana", host=BLOCKED_HOST)
    block_domain(BLOCKED_HOST)
    blocked.suspension_origin = ""
    blocked.suspended_at = None
    blocked.save(update_fields=["suspension_origin", "suspended_at"])
    req = request()
    assert resolve_known_actor(blocked.actor_url, req) is None
    assert resolve_sender(f"{blocked.actor_url}#main-key", req) is None


@pytest.mark.django_db
def test_a_blocked_host_cannot_create_a_new_mirror(db):
    """The second door: a block stops us taking in a fresh mirror of that host.

    Without this the block would only apply to accounts we happened to
    already hold, and any new activity from the host would create the
    mirror and carry on.
    """
    DomainBlock.objects.create(domain=BLOCKED_HOST)
    doc = {
        "id": f"https://{BLOCKED_HOST}/users/brandnew",
        "type": "Person",
        "preferredUsername": "brandnew",
        "publicKey": {"publicKeyPem": "-----BEGIN PUBLIC KEY-----"},
    }
    with pytest.raises(RemoteFetchError):
        mirror_user_from_person(doc)
    assert User.objects.filter(actor_url=doc["id"]).exists() is False


@pytest.mark.django_db
def test_an_unblock_reopens_resolution_for_a_host_that_is_not_blocked_elsewhere():
    """The door closes on the block and opens on the unblock.

    Asserting only "nothing found" after an unblock would pass whether or
    not the door ever opened. This resolves the same mirror on both sides
    of the block.
    """
    held = mirror(localname="dana", host=BLOCKED_HOST)
    assert resolve_known_actor(held.actor_url, request()) == held

    block, _swept = block_domain(BLOCKED_HOST)
    assert resolve_known_actor(held.actor_url, request()) is None

    unblock_domain(block)
    assert resolve_known_actor(held.actor_url, request()) == held
    held.refresh_from_db()
    assert held.suspended_at is None


@pytest.mark.django_db
def test_the_block_sweep_does_not_touch_local_accounts(db, member):
    block_domain("testserver")
    member.refresh_from_db()
    assert member.suspended_at is None


@pytest.mark.django_db
def test_the_block_sweep_skips_a_superuser_row_rather_than_raising_on_it(db):
    """R114's backstop must not turn a domain block into a 500.

    ``local=False`` with ``is_superuser=True`` is nonsense the schema does
    not prevent — a bad import or a hand-edited row can produce one. The
    ``AdminImmunityError`` in ``suspend()`` would fire mid-sweep and leave
    the moderator a block row, a half-completed sweep, and a traceback.
    The sweep therefore declines such rows up front, so the block still
    lands on every ordinary mirror from the host.
    """
    from reeltalk.social.models import AdminImmunityError

    odd = User.objects.create(
        localname="ghost@blocked.example",
        local=False,
        is_superuser=True,
        actor_url="https://blocked.example/users/ghost",
        inbox_url="https://blocked.example/users/ghost/inbox",
    )
    ordinary = User.objects.create(
        localname="real@blocked.example",
        local=False,
        actor_url="https://blocked.example/users/real",
        inbox_url="https://blocked.example/users/real/inbox",
    )
    block, suspended = block_domain("blocked.example")
    odd.refresh_from_db()
    ordinary.refresh_from_db()
    assert odd.suspended_at is None
    assert odd.pk not in [u.pk for u in suspended]
    # The rest of the sweep still completed — the skip is selective, not a
    # bail-out that leaves the whole block unenforced.
    assert ordinary.suspended_at is not None
    assert ordinary.suspension_origin == SuspensionOrigin.DOMAIN_BLOCK
    # And the immunity itself is real, not merely absent from this path.
    with pytest.raises(AdminImmunityError):
        odd.suspend()


# --- the per-account refusal ---------------------------------------------


@pytest.mark.django_db
def test_refusing_a_remote_account_suspends_the_mirror_locally(mod, member):
    remote = mirror()
    made, _ = file_report(reporter=member, target_user=remote, category="spam")
    logged_in(mod).post(reverse("moderation-refuse", args=[made.pk]), {"note": "no"})
    remote.refresh_from_db()
    assert remote.suspended_at is not None
    assert remote.suspension_origin == SuspensionOrigin.LOCAL


@pytest.mark.django_db
def test_refusing_broadcasts_nothing(mod, member):
    """We cannot sign another instance's actor document, and must not claim it."""
    import responses as responses_module

    with responses_module.RequestsMock() as mock:
        # The stub exists so a call would be *caught*, not so it must fire;
        # without this the mock fails on its own exit for an unused route
        # and the assertion below never runs.
        mock.assert_all_requests_are_fired = False
        mock.add(responses_module.POST, REMOTE_INBOX, status=202)
        remote = mirror()
        made, _ = file_report(reporter=member, target_user=remote, category="spam")
        logged_in(mod).post(reverse("moderation-refuse", args=[made.pk]), {})
        assert len(mock.calls) == 0


@pytest.mark.django_db
def test_refusing_a_local_target_is_refused(mod, member):
    """Refuse is the mirror-only verb; a local account goes through suspend."""
    local = create_member(localname="filer", password=PASSWORD)
    made, _ = file_report(reporter=local, target_user=member, category="spam")
    response = logged_in(mod).post(reverse("moderation-refuse", args=[made.pk]), {})
    assert response.status_code == 404
    member.refresh_from_db()
    assert member.suspended_at is None


@pytest.mark.django_db
def test_a_resolved_report_is_not_a_live_refuse_handle(mod, member):
    remote = mirror()
    made, _ = file_report(reporter=member, target_user=remote, category="spam")
    made.resolved_at = made.created
    made.save(update_fields=["resolved_at"])
    assert (
        logged_in(mod)
        .post(reverse("moderation-refuse", args=[made.pk]), {})
        .status_code
        == 404
    )


@pytest.mark.django_db
def test_a_non_moderator_cannot_refuse(member):
    remote = mirror()
    made, _ = file_report(reporter=member, target_user=remote, category="spam")
    assert (
        logged_in(member)
        .post(reverse("moderation-refuse", args=[made.pk]), {})
        .status_code
        == 403
    )


# --- the routes and the controls -----------------------------------------


@pytest.mark.django_db
def test_blocking_a_domain_through_the_route(mod):
    blocked = mirror(localname="dana", host=BLOCKED_HOST)
    response = logged_in(mod).post(
        reverse("moderation-block-domain"),
        {"domain": f"https://{BLOCKED_HOST}/", "note": "ZPROBE_ROUTE_BLOCK"},
    )
    assert response.status_code == 302
    assert DomainBlock.objects.filter(domain=BLOCKED_HOST).exists()
    blocked.refresh_from_db()
    assert blocked.suspended_at is not None


@pytest.mark.django_db
def test_blocking_an_empty_domain_is_refused_and_creates_nothing(mod):
    logged_in(mod).post(reverse("moderation-block-domain"), {"domain": "   "})
    assert DomainBlock.objects.count() == 0


@pytest.mark.django_db
def test_blocking_the_same_domain_twice_creates_one_row(mod):
    client = logged_in(mod)
    client.post(reverse("moderation-block-domain"), {"domain": BLOCKED_HOST})
    client.post(reverse("moderation-block-domain"), {"domain": BLOCKED_HOST})
    assert DomainBlock.objects.filter(domain=BLOCKED_HOST).count() == 1


@pytest.mark.django_db
def test_unblocking_through_the_route_lifts_the_sweep(mod):
    blocked = mirror(localname="dana", host=BLOCKED_HOST)
    logged_in(mod).post(reverse("moderation-block-domain"), {"domain": BLOCKED_HOST})
    block = DomainBlock.objects.get(domain=BLOCKED_HOST)
    logged_in(mod).post(reverse("moderation-unblock-domain", args=[block.id]))
    assert DomainBlock.objects.filter(domain=BLOCKED_HOST).exists() is False
    blocked.refresh_from_db()
    assert blocked.suspended_at is None


@pytest.mark.django_db
def test_a_non_moderator_cannot_block_a_domain(member):
    response = logged_in(member).post(
        reverse("moderation-block-domain"), {"domain": BLOCKED_HOST}
    )
    assert response.status_code == 403
    assert DomainBlock.objects.count() == 0


@pytest.mark.django_db
def test_an_anonymous_visitor_is_sent_to_login():
    response = Client().post(
        reverse("moderation-block-domain"), {"domain": BLOCKED_HOST}
    )
    assert response.status_code == 302
    assert "/login/" in response.url


@pytest.mark.django_db
def test_the_refuse_control_is_drawn_for_a_remote_target_and_not_a_local_one(
    mod, member
):
    remote = mirror()
    remote_report, _ = file_report(reporter=member, target_user=remote, category="spam")
    local_report, _ = file_report(
        reporter=create_member(localname="filer", password=PASSWORD),
        target_user=member,
        category="spam",
    )
    page = logged_in(mod).get(reverse("moderation")).content.decode()
    assert reverse("moderation-refuse", args=[remote_report.pk]) in page
    assert reverse("moderation-refuse", args=[local_report.pk]) not in page
    assert reverse("moderation-suspend", args=[local_report.pk]) in page


@pytest.mark.django_db
def test_the_blocked_servers_section_lists_a_block_with_its_reason(mod):
    DomainBlock.objects.create(
        domain=BLOCKED_HOST, created_by=mod, reason="ZPROBE_LISTED_REASON"
    )
    page = logged_in(mod).get(reverse("moderation")).content.decode()
    assert BLOCKED_HOST in page
    assert "ZPROBE_LISTED_REASON" in page
    assert (
        reverse("moderation-unblock-domain", args=[DomainBlock.objects.get().id])
        in page
    )


@pytest.mark.django_db
def test_the_nobody_may_act_on_rule_still_holds_for_a_refused_mirror(mod):
    """Refusing is not a promotion: the mirror is still an ordinary target."""
    remote = mirror()
    assert can_act_on(mod, remote) is True


@pytest.mark.django_db
def test_a_refused_mirror_is_hidden_from_the_feed_of_its_followers(member):
    """The generalisation's payoff: the existing suspension filter does the hiding."""
    remote = mirror()
    member.follows.add(remote)
    assert remote.pk in member.feed_member_ids()
    remote.suspend(reason="refused")
    assert remote.pk not in member.feed_member_ids()
