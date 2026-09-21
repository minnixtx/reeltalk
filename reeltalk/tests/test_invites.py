"""Invites (R82): one link, one account, then it is spent.

Three things get exercised here. The ``Invite`` model and the atomic
redemption that is the whole reason a single-use link is safe rather than
merely single-use-looking. The ``invite_scope`` setting that decides who may
mint a link. And the two views: the POST that mints one on the inviter's
own profile, and the public landing page the invitee opens.

The load-bearing property is that the liveness check and the marking happen
inside one row lock. Checked-and-then-marked as two statements is true until
two strangers open the same link in the same second, and on an invite-only
instance that is an extra account the owner never approved.
"""

from datetime import timedelta

import pytest
from django.contrib.auth.models import AnonymousUser
from django.db import transaction
from django.db.transaction import TransactionManagementError
from django.test import Client
from django.utils import timezone

from reeltalk.social.models import INVITE_TTL_DAYS, Invite, SiteSettings, User

# What ``secrets.token_urlsafe`` can emit. The route's charset matches it, so
# a code outside this set cannot be a real one.
CODE_ALPHABET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")

SIGNUP_PAYLOAD = {
    "localname": "joiner",
    "display_name": "",
    "email": "",
    "password1": "s3cretpass",
    "password2": "s3cretpass",
}

PITCH = "Invite a fellow movie freak to ReelTalk"


@pytest.fixture
def member(db):
    return User.objects.create_user(localname="member", password="s3cretpass")


@pytest.fixture
def owner(db):
    return User.objects.create_superuser(localname="owner", password="s3cretpass")


def _expire(invite):
    invite.expires_at = timezone.now() - timedelta(minutes=1)
    invite.save()
    return invite


# --- the model ---------------------------------------------------------------


@pytest.mark.django_db
def test_mint_credits_the_inviter_and_emits_a_long_random_code(member):
    invite = Invite.mint(member)
    assert invite.created_by == member
    assert invite.used_by is None and invite.used_at is None
    # A credential, not a human-readable word: long and URL-safe.
    assert len(invite.code) >= 24
    assert set(invite.code) <= CODE_ALPHABET
    assert invite.is_live


@pytest.mark.django_db
def test_mint_never_repeats_a_code(member):
    codes = {Invite.mint(member).code for _ in range(25)}
    assert len(codes) == 25


@pytest.mark.django_db
def test_a_new_invite_lives_for_the_ttl(member):
    invite = Invite.mint(member)
    window = invite.expires_at - invite.created_at
    # ``auto_now_add`` stamps created_at in the database a fraction of a
    # second after the timezone.now() that set expires_at, so exact equality
    # is the wrong question here — a second of slack asks the right one.
    assert abs(window - timedelta(days=INVITE_TTL_DAYS)) < timedelta(seconds=1)


@pytest.mark.django_db
def test_redeeming_spends_the_invite(member):
    invite = Invite.mint(member)
    joiner = User.objects.create_user(localname="joiner", password="s3cretpass")
    invite.redeem(joiner)
    invite.refresh_from_db()
    assert invite.used_by == joiner
    assert invite.used_at is not None
    assert not invite.is_live
    assert "already been used" in invite.unusable_reason()


@pytest.mark.django_db
def test_expiry_is_its_own_reason(member):
    invite = _expire(Invite.mint(member))
    assert not invite.is_live
    assert "expired" in invite.unusable_reason()


@pytest.mark.django_db
def test_lock_live_refuses_unknown_used_and_expired(member):
    assert Invite.lock_live("no-such-code-000") is None

    used = Invite.mint(member)
    used.redeem(User.objects.create_user(localname="joiner", password="s3cretpass"))
    with transaction.atomic():
        assert Invite.lock_live(used.code) is None

    expired = _expire(Invite.mint(member))
    with transaction.atomic():
        assert Invite.lock_live(expired.code) is None


@pytest.mark.django_db
def test_lock_live_hands_back_a_live_invite(member):
    invite = Invite.mint(member)
    with transaction.atomic():
        locked = Invite.lock_live(invite.code)
        assert locked is not None
        assert locked.pk == invite.pk


@pytest.mark.django_db(transaction=True)
def test_lock_live_is_a_real_row_lock_not_a_plain_filter():
    # Non-vacuity (R79): select_for_update outside a transaction is an
    # error in Django. If lock_live were a bare .filter().first() this would
    # return quietly, and "exactly one account per invite" would be a
    # comment rather than a property of the code.
    joiner = User.objects.create_user(localname="joiner", password="s3cretpass")
    invite = Invite.mint(joiner)
    with pytest.raises(TransactionManagementError):
        Invite.lock_live(invite.code)
    with transaction.atomic():
        assert Invite.lock_live(invite.code) is not None


@pytest.mark.django_db
def test_deleting_the_invited_account_does_not_reopen_the_link(owner):
    invite = Invite.mint(owner)
    joiner = User.objects.create_user(localname="joiner", password="s3cretpass")
    invite.redeem(joiner)
    joiner.delete()
    invite.refresh_from_db()
    # The seat stays spent even though the person who took it is gone: the
    # link was already handed around once.
    assert invite.used_by is None
    assert invite.used_at is not None
    assert not invite.is_live


@pytest.mark.django_db
def test_deleting_the_inviter_takes_unsent_invites_with_it(member):
    invite = Invite.mint(member)
    member.delete()
    assert not Invite.objects.filter(pk=invite.pk).exists()


# --- who may mint ------------------------------------------------------------


@pytest.mark.django_db
def test_the_default_scope_is_admins_only(owner, member):
    site = SiteSettings.get_instance()
    assert site.invite_scope == SiteSettings.INVITE_ADMINS
    assert site.may_send_invites(owner) is True
    assert site.may_send_invites(member) is False
    assert site.may_send_invites(AnonymousUser()) is False


@pytest.mark.django_db
def test_the_all_scope_reaches_members_but_not_anonymous(member):
    site = SiteSettings.get_instance()
    site.invite_scope = SiteSettings.INVITE_ALL
    site.save()
    assert site.may_send_invites(member) is True
    assert site.may_send_invites(AnonymousUser()) is False


# --- minting from the profile ------------------------------------------------


@pytest.mark.django_db
def test_minting_requires_being_logged_in(client):
    resp = client.post("/invite/create/")
    assert resp.status_code == 302
    assert "/login/" in resp["Location"]
    assert Invite.objects.count() == 0


@pytest.mark.django_db
def test_minting_is_post_only(client, owner):
    client.force_login(owner)
    assert client.get("/invite/create/").status_code == 405


@pytest.mark.django_db
def test_an_admin_mints_a_link_onto_their_own_profile(client, owner):
    client.force_login(owner)
    resp = client.post("/invite/create/")
    invite = Invite.objects.get()
    assert invite.created_by == owner
    assert resp["Location"] == f"/user/{owner.localname}/?invite={invite.code}"
    body = client.get(resp["Location"]).content.decode()
    assert PITCH in body
    assert invite.code in body


@pytest.mark.django_db
def test_a_member_is_refused_under_the_admins_scope(client, member):
    client.force_login(member)
    resp = client.post("/invite/create/")
    assert Invite.objects.count() == 0
    assert resp["Location"] == f"/user/{member.localname}/"
    # Refused out loud — a silent no-op reads as a broken button.
    assert "only lets its admins send invites" in (
        client.get(resp["Location"]).content.decode()
    )


@pytest.mark.django_db
def test_a_member_mints_once_the_scope_is_opened(client, member):
    site = SiteSettings.get_instance()
    site.invite_scope = SiteSettings.INVITE_ALL
    site.save()
    client.force_login(member)
    assert client.post("/invite/create/").status_code == 302
    assert Invite.objects.count() == 1


# --- the invitee's landing page ----------------------------------------------


@pytest.mark.django_db
def test_a_live_link_shows_the_signup_form_naming_the_inviter(client, owner):
    invite = Invite.mint(owner)
    body = client.get(f"/invite/{invite.code}/").content.decode()
    assert "invited you to join ReelTalk" in body
    assert owner.get_full_name() in body
    assert 'name="password1"' in body


@pytest.mark.django_db
def test_a_link_opens_signup_on_an_invite_only_instance(client, owner):
    # The whole point: /signup/ is shut, and the link still gets through.
    site = SiteSettings.get_instance()
    site.signup_policy = SiteSettings.INVITE
    site.save()
    invite = Invite.mint(owner)
    resp = client.post(f"/invite/{invite.code}/", SIGNUP_PAYLOAD)
    assert resp.status_code == 302
    assert resp["Location"] == "/welcome/"
    invite.refresh_from_db()
    assert invite.used_by == User.objects.get(localname="joiner")
    assert invite.used_at is not None


@pytest.mark.django_db
def test_a_link_cannot_open_a_second_account(owner):
    invite = Invite.mint(owner)
    url = f"/invite/{invite.code}/"
    assert Client().post(url, SIGNUP_PAYLOAD).status_code == 302

    second = Client()
    resp = second.post(url, dict(SIGNUP_PAYLOAD, localname="second"))
    assert resp.status_code == 200
    assert not User.objects.filter(localname="second").exists()
    assert "already been used" in resp.content.decode()
    assert Invite.objects.get().used_by.localname == "joiner"


@pytest.mark.django_db
def test_a_used_link_offers_no_form(client, owner):
    invite = Invite.mint(owner)
    invite.redeem(User.objects.create_user(localname="joiner", password="s3cretpass"))
    body = client.get(f"/invite/{invite.code}/").content.decode()
    assert 'name="password1"' not in body
    assert "already been used" in body


@pytest.mark.django_db
def test_an_expired_link_offers_no_form(client, owner):
    invite = _expire(Invite.mint(owner))
    body = client.get(f"/invite/{invite.code}/").content.decode()
    assert 'name="password1"' not in body
    assert "expired" in body


@pytest.mark.django_db
def test_an_unknown_code_is_refused_not_crashed(client):
    body = client.get("/invite/no-such-code-000/").content.decode()
    assert "not valid" in body
    assert 'name="password1"' not in body


@pytest.mark.django_db
def test_a_malformed_code_never_reaches_the_database(client):
    # '!' is outside the minted alphabet, so the route itself turns it away.
    assert client.get("/invite/not!valid!code!/").status_code == 404


@pytest.mark.django_db
def test_visiting_signed_in_does_not_spend_the_link(client, owner, member):
    invite = Invite.mint(owner)
    client.force_login(member)
    resp = client.get(f"/invite/{invite.code}/")
    assert resp.status_code == 302
    assert resp["Location"] == "/"
    invite.refresh_from_db()
    assert invite.used_at is None


@pytest.mark.django_db
def test_a_rejected_form_leaves_the_invite_live(client, owner):
    invite = Invite.mint(owner)
    resp = client.post(
        f"/invite/{invite.code}/", dict(SIGNUP_PAYLOAD, password2="other-pass")
    )
    assert resp.status_code == 200
    invite.refresh_from_db()
    assert invite.used_at is None
    # And it says so against the form, not by dropping the invite.
    assert 'name="password1"' in resp.content.decode()
    assert "invited you to join ReelTalk" in resp.content.decode()


# --- the box on the profile --------------------------------------------------


@pytest.mark.django_db
def test_the_box_sits_on_your_own_profile_and_nobody_elses(client, owner, member):
    client.force_login(owner)
    assert PITCH in client.get("/user/owner/").content.decode()
    assert PITCH not in client.get(f"/user/{member.localname}/").content.decode()


@pytest.mark.django_db
def test_the_box_is_hidden_for_a_member_without_permission(client, member):
    client.force_login(member)
    assert PITCH not in client.get(f"/user/{member.localname}/").content.decode()


@pytest.mark.django_db
def test_the_box_is_hidden_for_anonymous(client, owner):
    assert PITCH not in Client().get("/user/owner/").content.decode()


@pytest.mark.django_db
def test_a_code_renders_only_for_the_member_who_minted_it(client, owner, member):
    # Scope opened so `can` is not what is doing the work here — this is
    # the ownership check, which stops the profile from becoming an oracle
    # that confirms whether a guessed code exists.
    site = SiteSettings.get_instance()
    site.invite_scope = SiteSettings.INVITE_ALL
    site.save()
    invite = Invite.mint(owner)

    client.force_login(member)
    body = client.get(
        f"/user/{member.localname}/?invite={invite.code}"
    ).content.decode()
    assert "invite-url" not in body
    assert invite.code not in body

    client.force_login(owner)
    own = client.get(f"/user/owner/?invite={invite.code}").content.decode()
    assert "invite-url" in own
    # Absolute, and built from the request's own scheme + host.
    assert f'value="http://testserver/invite/{invite.code}/"' in own


@pytest.mark.django_db
def test_the_copy_script_loads_only_where_there_is_a_button(client, owner, member):
    client.force_login(owner)
    assert "js/invite.js" in client.get("/user/owner/").content.decode()
    client.force_login(member)
    assert (
        "js/invite.js" not in client.get(f"/user/{member.localname}/").content.decode()
    )


# --- the admin ledger --------------------------------------------------------


@pytest.mark.django_db
def test_the_ledger_reads_but_does_not_print_a_live_code(client, owner):
    client.force_login(owner)
    invite = Invite.mint(owner)
    changelist = client.get("/admin/social/invite/").content.decode()
    assert invite.created_by.localname in changelist
    assert invite.code not in changelist
    assert invite.code[:8] in changelist


@pytest.mark.django_db
def test_the_detail_page_shows_the_full_code_read_only(client, owner):
    client.force_login(owner)
    invite = Invite.mint(owner)
    detail = client.get(f"/admin/social/invite/{invite.pk}/change/").content.decode()
    assert invite.code in detail
    assert 'name="code"' not in detail


@pytest.mark.django_db
def test_invites_cannot_be_added_from_the_admin(client, owner):
    client.force_login(owner)
    assert client.get("/admin/social/invite/add/").status_code == 403


@pytest.mark.django_db
def test_the_admin_can_switch_who_may_invite(client, owner):
    # Asserted against the edit form rather than the changelist header: the
    # header renders the field's verbose name ("invite scope", lower case),
    # which is a string a test can match by accident and a rename can break
    # for no reason. The form field and its two options are the thing the
    # owner actually needs to be able to reach.
    site = SiteSettings.get_instance()
    client.force_login(owner)
    change_url = f"/admin/social/sitesettings/{site.pk}/change/"
    body = client.get(change_url).content.decode()
    assert 'name="invite_scope"' in body
    assert "Admins only" in body
    assert "All members" in body

    resp = client.post(
        change_url,
        {
            "name": site.name,
            "description": site.description,
            "signup_policy": site.signup_policy,
            "invite_scope": SiteSettings.INVITE_ALL,
        },
    )
    assert resp.status_code == 302
    assert SiteSettings.get_instance().invite_scope == SiteSettings.INVITE_ALL
