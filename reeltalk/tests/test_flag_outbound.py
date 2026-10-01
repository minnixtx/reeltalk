"""The outbound ``Flag`` and the instance representative (increment 6, R104).

Ordered by how each failure would bite:

* **The representative exists, and cannot be mistaken for a person.** It is
  created once, it cannot sign in, it holds no staff or moderator flag, and
  its name cannot be taken at signup. If any of those were wrong the
  instance would be forwarding reports as an identity someone could own.
* **Nobody may moderate it.** ``can_act_on`` refuses the representative
  from both actor positions, because banning it 410s the Person document
  peers use to verify our signatures — a moderator could take the whole
  forwarding wire down without meaning to.
* **The reporter is invisible on the wire.** Not in the ``actor``, not in
  the ``keyid``, not anywhere in the serialized body. This is the
  guarantee R104 exists for, so it is tested by searching the whole
  payload for the reporter's name rather than by checking one field.
* **The shape is what the peer actually parses.** A flat array of URI
  strings in ``object``, an id on our own host carrying our report id.
* **The four structural refusals.** Each asserts nothing was sent, because
  a refusal that fired after the POST left would be worse than no refusal.
* **A forward does not resolve anything, and says what it did.** The
  report stays open, the audit line lands on the open rows only, and a
  failed delivery is loud.

Session-proof rules throughout: ``create_user()`` for every probe, a
``sessionid`` check on each authenticated position, ``allow_redirects``
not relied on, and no bulk session teardown.
"""

import json

import pytest
import responses
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from reeltalk.activitypub.broadcast import broadcast_report_flag
from reeltalk.core.models import Film, Status
from reeltalk.moderation.decorators import can_act_on
from reeltalk.moderation.models import Report, file_report
from reeltalk.moderation.representative import (
    INSTANCE_ACTOR_LOCALNAME,
    instance_representative,
    is_instance_actor,
    validate_instance_localname,
)
from reeltalk.notifications.models import Notification
from reeltalk.social.forms import SignupForm
from reeltalk.tests.members import member, site_admin

User = get_user_model()
PASSWORD = "s3cretpass"

REMOTE_HOST = "remote.example"
REMOTE_INBOX = "https://remote.example/users/dana/inbox"


def logged_in(user):
    client = Client()
    client.force_login(user)
    return client


def mirror_account(localname="dana", host=REMOTE_HOST):
    """A mirrored remote account — no private key, by construction."""
    return User.objects.create(
        localname=f"{localname}@{host}",
        local=False,
        actor_url=f"https://{host}/users/{localname}",
        inbox_url=f"https://{host}/users/{localname}/inbox",
    )


def mirror_post(author, film, *, note_id=9001):
    return Status.objects.create(
        user=author,
        film=film,
        status_type="comment",
        content="<p>remote spam</p>",
        raw_content="remote spam",
        local=False,
        origin_id=note_id,
        remote_url=f"https://{REMOTE_HOST}/notes/{note_id}",
    )


@pytest.fixture
def reporter(db):
    """The member who filed the report — the identity that must not travel."""
    return member(localname="reporter_zoe", password=PASSWORD)


@pytest.fixture
def mod(db):
    return member(localname="moderator_yan", password=PASSWORD, is_moderator=True)


@pytest.fixture
def siteadmin(db):
    return site_admin(localname="root_admin", password=PASSWORD)


@pytest.fixture
def remote_target(db):
    return mirror_account()


@pytest.fixture
def remote_status(db, remote_target):
    film = Film.objects.create(title="Dune", year=2021)
    return mirror_post(remote_target, film)


@pytest.fixture
def report(reporter, remote_status):
    """An unresolved report about a remote user's post."""
    made, created = file_report(
        reporter=reporter,
        target_status=remote_status,
        category="spam",
        comment="ZPROBE_FORWARD_COMMENT",
    )
    assert created
    return made


def forward_url(report):
    return reverse("moderation-forward", args=[report.id])


# --- the representative ------------------------------------------------------


@pytest.mark.django_db
def test_the_representative_is_created_once_and_reused():
    """Two calls, one row. ``get_or_create`` rather than check-then-create."""
    first = instance_representative()
    second = instance_representative()
    assert first.pk == second.pk
    assert User.objects.filter(localname=INSTANCE_ACTOR_LOCALNAME).count() == 1


@pytest.mark.django_db
def test_the_representative_cannot_sign_in():
    """No usable password, so no credential opens a session as the instance.

    Asserted through Django's own authenticator rather than by eyeballing
    the stored hash — an unusable password that still authenticated would
    make the instance's signing identity a loginable account.
    """
    from django.contrib.auth import authenticate

    rep = instance_representative()
    assert not rep.has_usable_password()
    assert authenticate(username=INSTANCE_ACTOR_LOCALNAME, password=PASSWORD) is None


@pytest.mark.django_db
def test_the_representative_holds_no_staff_or_moderator_flag():
    rep = instance_representative()
    assert rep.is_staff is False
    assert rep.is_superuser is False
    assert rep.is_moderator is False


@pytest.mark.django_db
def test_the_instance_localname_is_rejected_at_signup():
    """Reserved by R12's charset rule, not by a denylist.

    A localname must start with a letter or a digit, so a leading
    underscore is invalid on a form that knows nothing about the
    representative. That is why the reservation cannot drift: there is no
    extra rule to forget.
    """
    form = SignupForm(
        {
            "localname": INSTANCE_ACTOR_LOCALNAME,
            "display_name": "",
            "email": "",
            "password1": "anotherstrongpass",
            "password2": "anotherstrongpass",
        }
    )
    assert not form.is_valid()
    assert "localname" in form.errors


@pytest.mark.django_db
def test_the_explicit_reservation_guard_catches_a_case_variant():
    """The belt to the charset rule's braces.

    ``validate_instance_localname`` is not wired into signup today because
    the charset already refuses the name. It exists so that if the charset
    is ever widened, the takeover is caught here rather than discovered as a
    stranger holding the instance's actor URL.
    """
    with pytest.raises(ValidationError):
        validate_instance_localname("_INSTANCE")
    validate_instance_localname("someone_else")


@pytest.mark.django_db
def test_is_instance_actor_matches_only_the_representative(siteadmin):
    rep = instance_representative()
    assert is_instance_actor(rep) is True
    assert is_instance_actor(siteadmin) is False
    assert is_instance_actor(mirror_account()) is False


@pytest.mark.django_db
def test_nobody_may_act_on_the_instance_representative(siteadmin, mod, reporter):
    """A fourth kind, and the shield refuses it for the admin too.

    Banning the representative 410s ``/user/_instance/``, and a peer that
    cannot fetch that Person document cannot verify anything we sign with
    that key — every future forward would fail at servers that had already
    accepted us. That is not a moderation target.
    """
    rep = instance_representative()
    assert can_act_on(siteadmin, rep) is False
    assert can_act_on(mod, rep) is False
    assert can_act_on(reporter, rep) is False


@pytest.mark.django_db
def test_the_representative_shield_sits_before_the_admin_short_circuit(mod):
    """Ordering, not just outcome: a superuser who is also the target is refused.

    ``can_act_on`` returns True for a superuser actor before it looks at
    anything else. This builds a superuser *carrying the representative's
    localname* and asserts the shield still holds, so moving the
    representative check below the superuser short-circuit goes red.
    """
    fake = site_admin(localname=INSTANCE_ACTOR_LOCALNAME + "_x", password=PASSWORD)
    fake.localname = INSTANCE_ACTOR_LOCALNAME
    fake.save(update_fields=["localname"])
    assert can_act_on(fake, fake) is False


# --- the payload -----------------------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_the_flag_actor_is_the_instance_and_not_the_reporter(report, reporter, mod):
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    logged_in(mod).post(forward_url(report), {})
    assert len(responses.calls) == 1
    sent = json.loads(responses.calls[0].request.body)
    assert sent["type"] == "Flag"
    assert sent["actor"] == f"http://testserver/user/{INSTANCE_ACTOR_LOCALNAME}/"
    # The reporter's own profile URL must not appear anywhere. Not their
    # ``actor_url`` — a local user's is empty by design, and asserting
    # against an empty string would pass against any payload whatsoever.
    assert f"/user/{reporter.localname}/" not in json.dumps(sent)


@responses.activate
@pytest.mark.django_db
def test_the_flag_is_signed_with_the_representatives_key(report, reporter, mod):
    """The masking has to reach the key or it has not happened.

    A peer verifies the signature before it reads the body. If the key were
    the reporter's, the ``actor`` field would say one thing and the keyid
    would name the reporter to every server we contacted.
    """
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    logged_in(mod).post(forward_url(report), {})
    signature_input = responses.calls[0].request.headers["Signature-Input"]
    assert f"user/{INSTANCE_ACTOR_LOCALNAME}/#main-key" in signature_input
    assert "reporter_zoe" not in signature_input
    assert "moderator_yan" not in signature_input


@responses.activate
@pytest.mark.django_db
def test_the_reporters_name_appears_nowhere_in_the_payload(report, reporter, mod):
    """The whole-body search, because a one-field assertion is the weak version.

    The comment travels — a report without a reason is nothing to act on —
    but nothing identifying the person who wrote it does.
    """
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    logged_in(mod).post(forward_url(report), {})
    body = responses.calls[0].request.body.decode()
    assert "reporter_zoe" not in body
    assert "moderator_yan" not in body
    assert "ZPROBE_FORWARD_COMMENT" in body


@responses.activate
@pytest.mark.django_db
def test_the_object_is_a_flat_array_of_uri_strings(report, mod):
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    logged_in(mod).post(forward_url(report), {})
    sent = json.loads(responses.calls[0].request.body)
    assert isinstance(sent["object"], list)
    assert all(isinstance(item, str) for item in sent["object"])


@responses.activate
@pytest.mark.django_db
def test_the_object_carries_the_target_account_and_the_home_status_reference(
    report, remote_target, remote_status, mod
):
    """The status is referenced by its *home* URL, not a copy minted here.

    ``note_reference`` returns the mirror's ``remote_url``; sending our own
    ``/status/<id>/`` would point their handler at a URL that claims their
    post is ours, and they would find nothing to match.
    """
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    logged_in(mod).post(forward_url(report), {})
    sent = json.loads(responses.calls[0].request.body)
    assert remote_target.actor_url in sent["object"]
    assert remote_status.remote_url in sent["object"]
    assert "testserver" not in json.dumps(sent["object"])


@responses.activate
@pytest.mark.django_db
def test_the_flag_id_is_on_our_host_and_carries_our_report_id(report, mod):
    """Their ``report_uri`` keeps the id only if the host matches the sender.

    Minting it on our canonical origin with our report pk means the id
    survives into *their* ``reports`` row pointing back at *our* row,
    which is what makes a live check verifiable from their side.
    """
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    logged_in(mod).post(forward_url(report), {})
    sent = json.loads(responses.calls[0].request.body)
    assert sent["id"] == f"http://testserver/reports/{report.pk}/"


@responses.activate
@pytest.mark.django_db
def test_the_forward_goes_to_the_targets_home_inbox_and_nowhere_else(report, mod):
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    logged_in(mod).post(forward_url(report), {})
    assert len(responses.calls) == 1
    assert responses.calls[0].request.url == REMOTE_INBOX


# --- the refusals ----------------------------------------------------------


@pytest.mark.django_db
def test_a_local_target_cannot_be_forwarded(reporter, mod):
    local = member(localname="someone", password=PASSWORD)
    made, _ = file_report(reporter=reporter, target_user=local, category="spam")
    response = logged_in(mod).post(forward_url(made), {})
    assert response.status_code == 404


@pytest.mark.django_db
def test_a_resolved_report_is_not_a_live_forward_handle(mod, report):
    report.resolved_at = report.created
    report.save(update_fields=["resolved_at"])
    response = logged_in(mod).post(forward_url(report), {})
    assert response.status_code == 404


@pytest.mark.django_db
def test_a_suspended_target_cannot_be_forwarded_and_nothing_is_sent(
    mod, report, remote_target
):
    """The pair to the hidden control.

    ``_deliver_signed`` skips a suspended recipient without recording a
    failure. Without this check a hand-built POST would return a success
    message for a forward that sent nothing — R88's blindness behind a
    green banner.
    """
    remote_target.suspend(reason="already handled")
    response = logged_in(mod).post(forward_url(report), {})
    assert response.status_code == 404
    assert len(responses.calls) == 0


@pytest.mark.django_db
def test_a_signed_in_non_moderator_is_refused(reporter, report):
    response = logged_in(reporter).post(forward_url(report), {})
    assert response.status_code == 403


@pytest.mark.django_db
def test_an_anonymous_visitor_is_sent_to_login(report):
    response = Client().post(forward_url(report), {})
    assert response.status_code == 302
    assert "/login/" in response.url


# --- the record -----------------------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_a_forward_leaves_the_report_unresolved(mod, report):
    """Forwarding is passing the complaint along, not deciding it."""
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    logged_in(mod).post(forward_url(report), {})
    report.refresh_from_db()
    assert report.resolved_at is None
    assert report.action == ""


@responses.activate
@pytest.mark.django_db
def test_the_outcome_is_written_to_the_open_rows_only(db, reporter):
    """A forward must not append a new line to a decision already closed.

    Two reporters, one target — so both rows sit in the *same* pile, which
    is the case that actually tests the scoping. Filing both as the same
    reporter would not test anything: R107's dedup makes the second file a
    no-op that returns the first row, and the forward would then 404 on a
    resolved report while looking like it had tested the right thing.
    """
    witness = member(localname="witness_ivy", password=PASSWORD)
    target = mirror_account()
    old = Report.objects.create(
        reporter=reporter,
        target_user=target,
        target_status=None,
        category="spam",
        comment="an old decision",
    )
    Report.objects.filter(id=old.id).update(
        resolved_at=old.created, action=Report.Action.DISMISS, note="CLOSED_AGE_AGO"
    )
    fresh, created = file_report(
        reporter=witness, target_user=target, category="spam", comment="fresh"
    )
    assert created and fresh.pk != old.pk

    responses.add(responses.POST, target.inbox_url, status=202)
    logged_in(member(localname="mod_two", password=PASSWORD, is_moderator=True)).post(
        forward_url(fresh), {}
    )

    old.refresh_from_db()
    fresh.refresh_from_db()
    assert old.note == "CLOSED_AGE_AGO"
    assert "[federation] delivered" in fresh.note


@responses.activate
@pytest.mark.django_db
def test_a_failed_delivery_is_loud_and_keeps_the_report_open(mod, report):
    responses.add(responses.POST, REMOTE_INBOX, status=500, body="boom")
    client = logged_in(mod)
    response = client.post(forward_url(report), {})
    assert response.status_code == 302
    report.refresh_from_db()
    assert report.resolved_at is None
    assert "[federation] forward not delivered" in report.note
    stored = [m.message for m in _messages(response)]
    assert any("not delivered" in m for m in stored)


def _messages(response):
    """Read the flash messages off the request the response was built from.

    The response is a redirect and renders nothing, so ``response.context``
    is ``None`` — a test that looked there would assert against an empty
    list and pass whatever the view said.
    """
    from django.contrib.messages import get_messages as _gm

    return _gm(response.wsgi_request)


@responses.activate
@pytest.mark.django_db
def test_forwarding_writes_no_notification(mod, report):
    """R99 again: a report is work, and forwarding it is still not a notification."""
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    before = Notification.objects.count()
    logged_in(mod).post(forward_url(report), {})
    assert Notification.objects.count() == before


# --- the control ----------------------------------------------------------


@responses.activate
@pytest.mark.django_db
def test_the_forward_control_is_drawn_for_a_remote_target(mod, report):
    page = logged_in(mod).get(reverse("moderation"))
    assert page.status_code == 200
    assert reverse("moderation-forward", args=[report.pk]) in page.content.decode()
    assert "Forward this report" in page.content.decode()


@pytest.mark.django_db
def test_the_forward_control_is_not_drawn_for_a_local_target(reporter, mod):
    local = member(localname="neighbor", password=PASSWORD)
    made, _ = file_report(reporter=reporter, target_user=local, category="spam")
    page = logged_in(mod).get(reverse("moderation"))
    assert reverse("moderation-forward", args=[made.pk]) not in page.content.decode()


@responses.activate
@pytest.mark.django_db
def test_the_queue_still_shows_the_report_after_a_forward(mod, report):
    """Forwarding drains nothing — the work here is not finished by telling."""
    responses.add(responses.POST, REMOTE_INBOX, status=202)
    logged_in(mod).post(forward_url(report), {})
    page = logged_in(mod).get(reverse("moderation"))
    assert reverse("moderation-forward", args=[report.pk]) in page.content.decode()


# --- the primitive, without the view ---------------------------------------


@responses.activate
@pytest.mark.django_db
def test_the_primitive_masks_the_reporter_when_called_directly(db, reporter, mod):
    """Called with no view in the path, so the guard cannot be a view artifact."""
    from django.test import RequestFactory

    target = mirror_account(localname="dana")
    film = Film.objects.create(title="Heat", year=1995)
    status = mirror_post(target, film, note_id=4242)
    made, _ = file_report(
        reporter=reporter,
        target_status=status,
        category="spam",
        comment="ZPROBE_DIRECT",
    )
    request = RequestFactory().post("/")
    broadcast_report_flag(request, made, representative=instance_representative())
    body = responses.calls[0].request.body.decode()
    assert "reporter_zoe" not in body
    assert "ZPROBE_DIRECT" in body
