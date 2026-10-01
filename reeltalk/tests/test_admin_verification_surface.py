"""The admin's verification surface (2F-3b).

Two things are being pinned here, and they pull in opposite directions, which
is why they are one file rather than scattered:

* **The admin can see everything and change nothing** (R123). The status
  line, the token inline and the send button all exist so a stuck member can
  be diagnosed. The two verified columns appear nowhere on the form — not
  editable, not read-only, not at all — and that absence is the entire
  mechanism. A read-only *display* of the derived state is fine; a field
  for the stored state, even a disabled one, is a second writer waiting to
  be enabled.
* **The admin's send is the member's send** (R124). It goes through
  ``send_verification_email``, the same helper signup uses, so there is one
  send path on the instance rather than two that can drift.

**Mock-only vs live.** Everything here runs on locmem mail and Django's
test client. It proves the wiring, the read, the refusal of a hand-verify,
and that the send mints and enqueues. It does not prove a real mail server
accepts the admin-triggered message — that is §2F-4.

**R126 is pinned too**, at the bottom of the file: the unverified signal
must not reach shared chrome. That test reads the base template from disk
because the rule is about a file nobody has edited yet.
"""

from pathlib import Path

import pytest
from django.contrib.auth import authenticate
from django.test import TestCase
from django_q.models import OrmQ, SignedPackage

from reeltalk.social.admin import TOKEN_INLINE_LIMIT, UserAdmin
from reeltalk.social.models import EmailVerificationToken, User
from reeltalk.social.tasks import SEND_TOKEN_FUNC
from reeltalk.tests.members import member as create_member
from reeltalk.tests.members import site_admin as create_site_admin
from reeltalk.tests.members import unverified_member

REPO_ROOT = Path(__file__).resolve().parents[2]

STRONG = "a-strong-pass-phrase"


@pytest.fixture
def admin_user(db):
    return create_site_admin(localname="admin", password=STRONG)


@pytest.fixture
def admin_client(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.fixture
def stuck(db):
    """A local member with a real address and no proof — the dead-on-arrival case."""
    return unverified_member(
        localname="stuck", password=STRONG, email="stuck@example.test"
    )


def change_payload(user, **overrides):
    """A full change-form POST that preserves the account's own values.

    Built from the row rather than hardcoded so a test that presses send
    cannot accidentally also flip a role it never meant to touch.
    """
    data = {
        "display_name": user.display_name or "",
        "email": user.email or "",
        "is_staff": "on" if user.is_staff else "",
        "is_superuser": "on" if user.is_superuser else "",
        "is_moderator": "on" if user.is_moderator else "",
        "report_email": "on" if user.report_email else "",
    }
    data.update(overrides)
    return data


def press_send(admin_client, user, **overrides):
    """POST the change form with the send button pressed.

    ``_send_verification`` is the button's own name, so including it here is
    what makes this "pressed the button" rather than "saved the form" — and
    the tests below only mean anything if it is present.
    """
    url = f"/admin/social/user/{user.pk}/change/"
    data = change_payload(user, **overrides)
    data["_send_verification"] = "1"
    with TestCase.captureOnCommitCallbacks(execute=True):
        return admin_client.post(url, data)


def queued():
    return [SignedPackage.loads(q.payload) for q in OrmQ.objects.all()]


def body(resp):
    return resp.content.decode()


# --- R123: the verified state is not editable, and not even present ------


@pytest.mark.django_db
def test_the_verified_columns_are_not_fields_on_the_admin_form(admin_client, stuck):
    url = f"/admin/social/user/{stuck.pk}/change/"
    resp = admin_client.get(url)
    assert resp.status_code == 200
    form = resp.context["adminform"].form
    assert "email_verified_at" not in form.fields
    assert "verified_email" not in form.fields


@pytest.mark.django_db
def test_the_verified_columns_are_in_no_fieldset_on_either_page(db):
    """Both the change page and the add page.

    Checked against ``get_fieldsets`` rather than the class attribute,
    because that is the method Django actually reads and an override is
    exactly where a field could sneak back in.
    """
    model_admin = UserAdmin(User, None)
    probe = User.objects.create_user(localname="probe", password=STRONG)
    for page, obj in (("change", probe), ("add", None)):
        flat = []
        for _title, opts in model_admin.get_fieldsets(None, obj):
            flat.extend(opts["fields"])
        assert "email_verified_at" not in flat, page
        assert "verified_email" not in flat, page


@pytest.mark.django_db
def test_posting_the_verified_columns_through_the_admin_changes_nothing(
    admin_client, stuck
):
    """The columns are absent from the form, so a hand-crafted POST cannot use them.

    This is the attack the absence is for: someone who reads the model and
    guesses the field name, rather than the admin UI.
    """
    url = f"/admin/social/user/{stuck.pk}/change/"
    resp = admin_client.post(
        url,
        {
            **change_payload(stuck),
            "email_verified_at": "2026-09-30 12:00:00",
            "verified_email": "stuck@example.test",
        },
    )
    assert resp.status_code == 302
    stuck.refresh_from_db()
    assert stuck.email_verified_at is None
    assert stuck.verified_email == ""
    assert stuck.email_verified is False


# --- the send action -----------------------------------------------------


@pytest.mark.django_db
def test_the_send_button_is_on_the_change_page_for_a_stuck_account(admin_client, stuck):
    resp = admin_client.get(f"/admin/social/user/{stuck.pk}/change/")
    assert "_send_verification" in body(resp)


@pytest.mark.django_db
def test_pressing_send_queues_the_same_task_signup_queues(admin_client, stuck):
    """Same helper, same task, same payload shape — not an admin-only send.

    Asserted against the queue rather than the outbox because the request
    must not talk to a mail server; the enqueue *is* the observable.
    """
    resp = press_send(admin_client, stuck)
    assert resp.status_code == 302

    funcs = [q["func"] for q in queued()]
    assert funcs == [SEND_TOKEN_FUNC]
    token = EmailVerificationToken.objects.get(user=stuck)
    assert [list(q["args"]) for q in queued()] == [[token.pk]]
    assert token.email == "stuck@example.test"


@pytest.mark.django_db
def test_pressing_send_never_writes_a_verified_state(admin_client, stuck):
    """The whole of R123 in one assertion.

    The mail only ever asks. If this ever fails, "verified" has stopped
    meaning *someone clicked a link we sent*, and every trust decision
    built on it — password reset above all — is now ambiguous.
    """
    press_send(admin_client, stuck)
    stuck.refresh_from_db()
    assert stuck.email_verified_at is None
    assert stuck.verified_email == ""
    assert stuck.email_verified is False
    assert authenticate(username="stuck", password=STRONG) is None


@pytest.mark.django_db
def test_pressing_send_records_the_admin_as_the_source(admin_client, stuck):
    """A send from the admin carries a source, unlike a fixture mint.

    ``request_ip`` is what makes the per-source throttle and the audit
    trail read the same whatever route the send came in through.
    """
    press_send(admin_client, stuck)
    token = EmailVerificationToken.objects.get(user=stuck)
    assert token.request_ip


@pytest.mark.django_db
def test_pressing_send_replaces_a_live_link(admin_client, stuck):
    """The consequence the button warns about, made real.

    Mint-supersedes is what makes single-use mean anything, so a second
    send must kill the first — and the first must then refuse its click.
    """
    first = EmailVerificationToken.mint(stuck)
    press_send(admin_client, stuck)
    first.refresh_from_db()
    assert first.link_state == "superseded"
    assert EmailVerificationToken.live().filter(user=stuck).count() == 1


@pytest.mark.django_db
def test_correcting_a_typo_and_sending_in_one_save_mails_the_corrected_address(
    admin_client, stuck
):
    """R124's recovery flow as a single action.

    The order matters and is easy to get wrong: ``change_email`` runs
    first and leaves the row at the new address, so the mint that follows
    binds to the correction rather than to the typo.
    """
    press_send(admin_client, stuck)
    stale = EmailVerificationToken.objects.get(user=stuck)

    resp = press_send(admin_client, stuck, email="fixed@example.test")
    assert resp.status_code == 302

    stuck.refresh_from_db()
    assert stuck.email == "fixed@example.test"
    token = EmailVerificationToken.objects.filter(user=stuck).first()
    assert token.email == "fixed@example.test"
    assert EmailVerificationToken.live().filter(user=stuck).count() == 1

    stale.refresh_from_db()
    assert stale.link_state == "superseded"


@pytest.mark.django_db
def test_a_non_staff_member_cannot_trigger_a_send(client, stuck):
    """The action sits behind the admin's own door, not behind a check."""
    plain = create_member(localname="nosy", password=STRONG)
    client.force_login(plain)
    resp = client.post(
        f"/admin/social/user/{stuck.pk}/change/",
        change_payload(stuck, _send_verification="1"),
    )
    assert resp.status_code == 302
    assert "/admin/login/" in resp.url
    assert not EmailVerificationToken.objects.filter(user=stuck).exists()


# --- the status line -----------------------------------------------------


@pytest.mark.django_db
def test_the_status_line_says_verified_for_a_verified_member(admin_client):
    verified = create_member(localname="solid", password=STRONG)
    html = body(admin_client.get(f"/admin/social/user/{verified.pk}/change/"))
    assert "Verified" in html
    assert "solid@example.test" in html
    assert "_send_verification" not in html


@pytest.mark.django_db
def test_the_status_line_warns_when_a_live_link_already_exists(admin_client, stuck):
    EmailVerificationToken.mint(stuck)
    html = body(admin_client.get(f"/admin/social/user/{stuck.pk}/change/"))
    assert "still live until" in html
    assert "Send a new link (replaces the live one)" in html


@pytest.mark.django_db
def test_the_status_line_says_no_live_link_when_there_is_none(admin_client, stuck):
    html = body(admin_client.get(f"/admin/social/user/{stuck.pk}/change/"))
    assert "No live link" in html
    assert "Send verification email" in html


@pytest.mark.django_db
def test_an_addressless_account_says_so_and_offers_no_button(admin_client):
    blank = User.objects.create_user(localname="blank", password=STRONG, email="")
    html = body(admin_client.get(f"/admin/social/user/{blank.pk}/change/"))
    assert "no address to verify" in html
    assert "Nothing to send" in html
    assert "_send_verification" not in html


@pytest.mark.django_db
def test_the_instance_representative_is_told_to_leave_it_alone(admin_client):
    """Without its own sentence the rep reads as a stuck account, and an
    admin who "fixes" that breaks the signing identity for every Flag."""
    from reeltalk.moderation.representative import instance_representative

    rep = instance_representative()
    html = body(admin_client.get(f"/admin/social/user/{rep.pk}/change/"))
    assert "Instance representative" in html
    assert "Do not give it one" in html
    assert "_send_verification" not in html


@pytest.mark.django_db
def test_a_remote_mirror_is_told_verification_does_not_apply(admin_client):
    mirror = User.objects.create_user(
        localname="faraway@example.org",
        password=STRONG,
        email="faraway@example.org",
        local=False,
    )
    html = body(admin_client.get(f"/admin/social/user/{mirror.pk}/change/"))
    assert "Remote account" in html
    assert "_send_verification" not in html


# --- the token inline ----------------------------------------------------


@pytest.mark.django_db
def test_a_failed_send_shows_its_error_verbatim(admin_client, stuck):
    """The diagnostic must not be cut short or paraphrased.

    The 2E lesson is that the long SMTP line is the thing that explains a
    broken deploy; an inline that shows only "failed" sends the admin to
    the worker log for the one fact they need.
    """
    token = EmailVerificationToken.mint(stuck)
    token.send_error = (
        "554 5.7.1 <stuck@example.test>: Recipient address rejected: "
        "Sender is not same as SMTP authenticate username"
    )
    token.save(update_fields=["send_error"])

    html = body(admin_client.get(f"/admin/social/user/{stuck.pk}/change/"))
    assert "Sender is not same as SMTP authenticate username" in html
    assert "failed" in html


@pytest.mark.django_db
def test_a_queued_send_is_distinguished_from_a_sent_one(admin_client, stuck):
    EmailVerificationToken.mint(stuck)
    html = body(admin_client.get(f"/admin/social/user/{stuck.pk}/change/"))
    assert "queued" in html
    assert "sent 2" not in html


@pytest.mark.django_db
def test_the_inline_never_shows_a_spendable_token(admin_client, stuck):
    """No hand-verify through the display layer.

    A full live code on screen is a credential the admin could open and
    click themselves, which would put a verified flag on the account
    without the member ever seeing the mail — exactly what R123 forbids,
    arriving through the page instead of the form.
    """
    token = EmailVerificationToken.mint(stuck)
    html = body(admin_client.get(f"/admin/social/user/{stuck.pk}/change/"))
    assert token.code not in html
    assert token.code[:8] in html


@pytest.mark.django_db
def test_the_inline_lists_every_send_newest_first(admin_client, stuck):
    first = EmailVerificationToken.mint(stuck)
    second = EmailVerificationToken.mint(stuck)
    html = body(admin_client.get(f"/admin/social/user/{stuck.pk}/change/"))
    assert html.index(second.code[:8]) < html.index(first.code[:8])
    assert "superseded" in html
    assert "live" in html


def _send_table(html):
    """Just the verification-sends table.

    Scoped because the admin page renders its own fieldset rows as table
    rows, so a page-wide ``<tr>`` count measures Django's chrome rather
    than what this inline drew.
    """
    start = html.index('class="verification-sends"')
    return html[start : html.index("</table>", start)]


@pytest.mark.django_db
def test_the_inline_bounds_what_it_renders_and_says_so(admin_client, stuck):
    for _ in range(TOKEN_INLINE_LIMIT + 3):
        EmailVerificationToken.mint(stuck)
    html = body(admin_client.get(f"/admin/social/user/{stuck.pk}/change/"))
    assert "earlier send(s) not shown" in html
    table = _send_table(html)
    assert table.count("<tr>") == TOKEN_INLINE_LIMIT + 1  # +1 for the header


@pytest.mark.django_db
def test_an_account_that_has_never_been_sent_says_so(admin_client, stuck):
    html = body(admin_client.get(f"/admin/social/user/{stuck.pk}/change/"))
    assert "has ever been sent" in html


# --- where the send leaves the admin -------------------------------------


@pytest.mark.django_db
def test_pressing_send_stays_on_the_account(admin_client, stuck):
    """Not the changelist.

    The status line the admin just read is on this page, and bouncing them
    off it after a send means re-finding the account to see whether the
    send worked.
    """
    resp = press_send(admin_client, stuck)
    assert resp.status_code == 302
    assert resp.url == f"/admin/social/user/{stuck.pk}/change/"


# --- R126: nothing reaches shared chrome ---------------------------------

# The distinctive sentences this increment introduces. None of them may
# appear in the site's base template, because a site-wide banner turns one
# member's unfinished signup into a public property of the instance.
ADMIN_ONLY_PHRASES = (
    "Not verified",
    "cannot sign in until someone clicks",
    "unverified account",
    "has not confirmed its email",
)


def test_the_unverified_signal_is_not_in_the_site_base_template():
    base = (REPO_ROOT / "templates" / "base.html").read_text()
    for phrase in ADMIN_ONLY_PHRASES:
        assert phrase not in base, phrase


def test_no_shared_template_carries_the_admin_verification_copy():
    """Every template this repo owns, scanned from disk.

    A file-based guard rather than a rendered one, because the rule is
    prospective: nothing today puts this copy in shared chrome, and the
    cheap way to keep it that way is to fail the moment someone does. The
    Django admin's own templates are not here — they live in site-packages
    and are only reachable by staff, which is exactly where R126 wants the
    copy to be.
    """
    roots = [REPO_ROOT / "templates", *(REPO_ROOT / "reeltalk").glob("*/templates")]
    scanned = 0
    for root in roots:
        assert root.is_dir(), f"expected template root {root}"
        for template in root.rglob("*.html"):
            scanned += 1
            text = template.read_text()
            for phrase in ADMIN_ONLY_PHRASES:
                assert phrase not in text, f"{template.name}: {phrase}"
    assert scanned, "the guard scanned no templates"
