"""Email verification, model and token layer (2F-1, R118/R120).

Nothing in this increment sends mail and nothing gates sign-in — that is 2F-2
and 2F-3. What gets pinned here is the ground the rest of 2F stands on:

* the address is normalised and unique when set (R118), with the index
  partial so the address-less rows an instance already holds stay legal;
* the verified state is **derived**, so moving the address invalidates it
  with no save hook and nothing for a future writer to forget;
* a token is bound to the address it was minted for and cannot verify
  another one (R120);
* mint supersedes, expiry expires, and the liveness lock is a real lock
  rather than a filter that only looks like one.

The last section is a non-regression rather than a feature: an unverified
account still signs in, and ``is_active`` still means only what R102b pins it
to. If 2F-1 had shipped the gate early, that is where it would show.
"""

from datetime import timedelta

import pytest
from django.contrib.auth.backends import ModelBackend
from django.db import IntegrityError, transaction
from django.db.transaction import TransactionManagementError
from django.utils import timezone

from reeltalk.social.models import (
    EMAIL_VERIFICATION_TTL_HOURS,
    AddressMismatchError,
    EmailVerificationToken,
    User,
)

# What ``secrets.token_urlsafe`` can emit. Anything outside this set is not a
# code this model produced.
CODE_ALPHABET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")

PASSWORD = "s3cretpass"


def _member(localname="member", email="member@example.com"):
    return User.objects.create_user(localname=localname, password=PASSWORD, email=email)


def _verify(user):
    """Put ``user`` in the verified state the way ``consume()`` leaves it."""
    user.email_verified_at = timezone.now()
    user.verified_email = user.email
    user.save(update_fields=["email_verified_at", "verified_email"])
    return user


# --- the address: normalised, unique when set, absent is legal (R118) ----


@pytest.mark.django_db
def test_the_address_is_normalised_on_save():
    # Normalised in ``save()``, not only in the form: a management command, a
    # fixture or a data migration bypasses every form, and an address that
    # skips normalisation is an address the unique index cannot see.
    user = User.objects.create_user(
        localname="mixed", password=PASSWORD, email="  Foo@Example.COM  "
    )
    user.refresh_from_db()
    assert user.email == "foo@example.com"


@pytest.mark.django_db
def test_a_case_variant_of_a_taken_address_cannot_be_created():
    # Normalisation and the index working together is the whole point: without
    # the fold, ``SHARED@EXAMPLE.COM`` slips past a unique index built over
    # ``shared@example.com`` and one mailbox becomes two accounts.
    User.objects.create_user(
        localname="first", password=PASSWORD, email="shared@example.com"
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            User.objects.create_user(
                localname="second", password=PASSWORD, email="SHARED@EXAMPLE.COM"
            )
    assert User.objects.filter(localname="second").exists() is False


@pytest.mark.django_db
def test_several_accounts_may_have_no_address_at_all():
    # The non-vacuity of the *partial* index. A plain ``unique=True`` on
    # ``email`` cannot be built over more than one empty string, so this is
    # the test that fails if the ``condition=~Q(email="")`` is ever dropped
    # — which is exactly the mistake that breaks the migration on a live DB.
    #
    # Three blanks is not arbitrary: that is how many address-less accounts
    # this instance has (the standing probe member, the instance
    # representative, and the dedup fixture), all of which must stay legal.
    for name in ("blank_one", "blank_two", "blank_three"):
        User.objects.create_user(localname=name, password=PASSWORD)
    assert User.objects.filter(email="").count() == 3


@pytest.mark.django_db
def test_two_accounts_cannot_share_a_real_address():
    User.objects.create_user(
        localname="first", password=PASSWORD, email="dup@example.com"
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            User.objects.create_user(
                localname="second", password=PASSWORD, email="dup@example.com"
            )


# --- the verified state is derived, not stored ---------------------------


@pytest.mark.django_db
def test_an_account_with_an_address_is_not_thereby_verified():
    # The address is a string somebody typed until a link to it is clicked.
    # This is the distinction 2F exists to make.
    user = _member()
    assert user.email_verified is False
    assert user.email_verified_at is None
    assert user.verified_email == ""


@pytest.mark.django_db
def test_verification_holds_when_the_verified_address_is_the_current_one():
    user = _verify(_member())
    assert user.email_verified is True


@pytest.mark.django_db
def test_a_timestamp_with_no_verified_address_verifies_nothing():
    # ``email_verified_at`` alone is proof of *some* address, possibly not
    # this one. Both halves are independently necessary.
    user = _member()
    user.email_verified_at = timezone.now()
    user.save(update_fields=["email_verified_at"])
    assert user.email_verified is False


@pytest.mark.django_db
def test_a_verified_address_with_no_timestamp_verifies_nothing():
    user = _member()
    user.verified_email = user.email
    user.save(update_fields=["verified_email"])
    assert user.email_verified is False


@pytest.mark.django_db
def test_moving_the_address_invalidates_verification_with_no_hook():
    # THE property this shape exists for. The site admin may edit an address
    # (R123 permits it, R124 makes it the routine typo recovery) and must not
    # thereby leave the account asserting control of an address nobody proved.
    #
    # A stored boolean would need a hook to notice the address moved. Here
    # there is nothing to notice: the equality simply stops holding.
    user = _verify(_member())
    assert user.email_verified is True

    user.email = "someone.else@example.com"
    user.save(update_fields=["email"])
    user.refresh_from_db()

    assert user.email_verified is False
    # The old proof is not erased — it still records what *was* verified,
    # which is what the admin's token inline (R123) needs to read.
    assert user.verified_email == "member@example.com"
    assert user.email_verified_at is not None


@pytest.mark.django_db
def test_both_sides_of_the_comparison_are_normalised():
    # The verified state is a string comparison, so if only one side folded,
    # a legitimately verified address could read as unverified on casing
    # alone — a member stuck on a link that "doesn't work" for a reason
    # nobody can see.
    user = User.objects.create_user(
        localname="mixed", password=PASSWORD, email="Foo@Example.com"
    )
    user.email_verified_at = timezone.now()
    user.verified_email = "FOO@EXAMPLE.COM"
    user.save(update_fields=["email_verified_at", "verified_email"])
    user.refresh_from_db()
    assert user.email == "foo@example.com"
    assert user.verified_email == "foo@example.com"
    assert user.email_verified is True


# --- the token: shape ----------------------------------------------------


@pytest.mark.django_db
def test_mint_binds_the_token_to_the_address_on_the_account():
    user = _member()
    token = EmailVerificationToken.mint(user)
    assert token.user == user
    assert token.email == "member@example.com"
    assert token.used_at is None
    assert token.superseded_at is None
    assert token.is_live is True


@pytest.mark.django_db
def test_a_token_address_is_normalised_like_the_account_address():
    token = EmailVerificationToken.objects.create(
        user=_member(),
        email="  Token@Example.COM  ",
        expires_at=timezone.now() + timedelta(hours=1),
    )
    token.refresh_from_db()
    assert token.email == "token@example.com"


@pytest.mark.django_db
def test_the_code_is_a_long_random_non_editable_credential():
    token = EmailVerificationToken.mint(_member())
    assert len(token.code) >= 32
    assert set(token.code) <= CODE_ALPHABET
    # Non-editable: a credential whose value an admin form can retype is not
    # a credential.
    assert EmailVerificationToken._meta.get_field("code").editable is False


@pytest.mark.django_db
def test_mint_never_repeats_a_code():
    user = _member()
    codes = {EmailVerificationToken.mint(user).code for _ in range(25)}
    assert len(codes) == 25


@pytest.mark.django_db
def test_a_new_token_lives_for_seventy_two_hours():
    # Asserted against 72h literally rather than against the constant, so a
    # change to the constant turns this red — the number is the decision
    # (R120), not the name it is stored under.
    token = EmailVerificationToken.mint(_member())
    window = token.expires_at - token.created_at
    assert abs(window - timedelta(hours=72)) < timedelta(seconds=1)
    assert EMAIL_VERIFICATION_TTL_HOURS == 72


@pytest.mark.django_db
def test_mint_refuses_an_account_with_no_address():
    # Not fussiness. A token bound to the empty string would satisfy
    # ``verified_email == email`` for an account that has no address at all,
    # so consuming it would read as a verified address on nothing.
    plain = User.objects.create_user(localname="plain", password=PASSWORD)
    with pytest.raises(ValueError, match="no email address"):
        EmailVerificationToken.mint(plain)
    assert EmailVerificationToken.objects.count() == 0


# --- the token: supersede, spend, expire ------------------------------


@pytest.mark.django_db
def test_minting_supersedes_the_prior_live_token():
    user = _member()
    first = EmailVerificationToken.mint(user)
    second = EmailVerificationToken.mint(user)
    first.refresh_from_db()

    assert first.superseded_at is not None
    assert first.is_live is False
    assert "replaced" in first.unusable_reason()
    # Supersede is not a click. ``used_at`` is the provenance R123 is built
    # on — "a human clicked this" — and a resend must never be able to write it.
    assert first.used_at is None
    assert second.is_live is True


@pytest.mark.django_db
def test_supersede_leaves_a_spent_token_exactly_as_it_was():
    # The two facts stay distinct in both directions: a click is not a
    # supersede, and a supersede does not re-write a click.
    user = _member()
    first = EmailVerificationToken.mint(user)
    first.consume()
    spent_at = first.used_at

    EmailVerificationToken.mint(user)
    first.refresh_from_db()
    assert first.used_at == spent_at
    assert first.superseded_at is None


@pytest.mark.django_db
def test_only_one_token_is_live_per_account_at_a_time():
    user = _member()
    for _ in range(4):
        EmailVerificationToken.mint(user)
    assert EmailVerificationToken.live().filter(user=user).count() == 1
    assert EmailVerificationToken.objects.filter(user=user).count() == 4


@pytest.mark.django_db
def test_the_live_queryset_and_the_live_property_agree():
    # ``lock_live()`` filters on the queryset and the view reports
    # ``unusable_reason()``, so if the two drifted a token would be refused
    # with no reason given, or accepted while a reason existed. Four tokens,
    # one in each state, built in the order that produces each one.
    user = _member()

    superseded = EmailVerificationToken.mint(user)
    spent = EmailVerificationToken.mint(user)  # supersedes the first
    spent.consume()
    expired = EmailVerificationToken.mint(user)  # nothing live to supersede
    expired.expires_at = timezone.now() - timedelta(minutes=1)
    expired.save(update_fields=["expires_at"])
    live = EmailVerificationToken.mint(user)  # `expired` is dead, not superseded

    assert EmailVerificationToken.live().filter(user=user).count() == 1
    assert EmailVerificationToken.live().filter(user=user).first().pk == live.pk
    for token in (superseded, spent, expired, live):
        assert token.is_live is (token.unusable_reason() is None)


@pytest.mark.django_db
def test_expiry_refuses_at_seventy_three_hours_and_admits_at_seventy_one():
    # The window's edge, expressed as elapsed time since the mint rather than
    # as an absolute timestamp to be squinted at. A token minted 73h ago with
    # a 72h TTL is dead; one minted 71h ago has an hour left.
    #
    # ``created_at`` is ``auto_now_add``, so it only self-fills on insert —
    # naming it in ``update_fields`` is what lets the test wind the clock.
    token = EmailVerificationToken.mint(_member())

    minted_73h_ago = timezone.now() - timedelta(hours=73)
    token.created_at = minted_73h_ago
    token.expires_at = minted_73h_ago + timedelta(hours=EMAIL_VERIFICATION_TTL_HOURS)
    token.save(update_fields=["created_at", "expires_at"])
    assert token.is_live is False
    assert "expired" in token.unusable_reason()
    with transaction.atomic():
        assert EmailVerificationToken.lock_live(token.code) is None

    minted_71h_ago = timezone.now() - timedelta(hours=71)
    token.created_at = minted_71h_ago
    token.expires_at = minted_71h_ago + timedelta(hours=EMAIL_VERIFICATION_TTL_HOURS)
    token.save(update_fields=["created_at", "expires_at"])
    assert token.is_live is True
    assert token.unusable_reason() is None
    with transaction.atomic():
        assert EmailVerificationToken.lock_live(token.code) is not None


@pytest.mark.django_db
def test_a_spent_token_is_refused_on_replay():
    # Single use is enforced at the lock, which is the only entry point the
    # verify path uses. A second click gets ``None``, not a second spend.
    token = EmailVerificationToken.mint(_member())
    token.consume()
    with transaction.atomic():
        assert EmailVerificationToken.lock_live(token.code) is None
    assert "already been used" in token.unusable_reason()


@pytest.mark.django_db
def test_a_superseded_token_is_refused_at_the_lock():
    user = _member()
    first = EmailVerificationToken.mint(user)
    EmailVerificationToken.mint(user)
    with transaction.atomic():
        assert EmailVerificationToken.lock_live(first.code) is None


@pytest.mark.django_db
def test_lock_live_hands_back_a_live_token():
    token = EmailVerificationToken.mint(_member())
    with transaction.atomic():
        locked = EmailVerificationToken.lock_live(token.code)
        assert locked is not None
        assert locked.pk == token.pk


@pytest.mark.django_db
def test_lock_live_refuses_an_unknown_code():
    with transaction.atomic():
        assert EmailVerificationToken.lock_live("no-such-code-000") is None


@pytest.mark.django_db(transaction=True)
def test_lock_live_is_a_real_row_lock_not_a_plain_filter():
    # Non-vacuity, the same test the invite shape carries. ``select_for_update``
    # outside a transaction is an error in Django, so if ``lock_live`` were a
    # bare ``.filter().first()`` this would return quietly — and "one click per
    # token" would be a comment rather than a property of the code. A member
    # and their mail client's link prefetcher hit these links at the same
    # moment in ordinary life.
    token = EmailVerificationToken.mint(_member())
    with pytest.raises(TransactionManagementError):
        EmailVerificationToken.lock_live(token.code)
    with transaction.atomic():
        assert EmailVerificationToken.lock_live(token.code) is not None


# --- the address binding (R120) ----------------------------------------


@pytest.mark.django_db
def test_a_token_minted_for_one_address_cannot_verify_another():
    # The sequence this binding exists to kill: mint to a typo, the admin
    # corrects the address (R124), somebody clicks the old link. Without the
    # binding that click marks the corrected address verified on the strength
    # of mail that only ever went to the typo — and password reset, the next
    # thing built on this, inherits a "verified" that attests nothing.
    user = _member(email="typo@example.com")
    token = EmailVerificationToken.mint(user)

    user.email = "corrected@example.com"
    user.save(update_fields=["email"])

    with pytest.raises(AddressMismatchError):
        token.consume()

    user.refresh_from_db()
    assert user.email_verified is False
    assert user.email_verified_at is None
    assert user.verified_email == ""
    token.refresh_from_db()
    assert token.used_at is None


@pytest.mark.django_db
def test_consume_spends_the_token_and_verifies_the_address_it_was_minted_for():
    user = _member()
    token = EmailVerificationToken.mint(user)
    token.consume()
    user.refresh_from_db()
    token.refresh_from_db()

    assert user.email_verified is True
    assert user.verified_email == "member@example.com"
    assert token.used_at is not None
    assert token.is_live is False
    # The proof and the spend are the same instant, so they cannot drift:
    # there is no window where the account is verified by a token that has
    # not been spent, or the reverse.
    assert user.email_verified_at == token.used_at


@pytest.mark.django_db
def test_the_binding_survives_a_case_difference():
    # Both sides normalise, so a token is never refused for a reason nobody
    # can see. Proved end to end rather than only at the field level.
    user = User.objects.create_user(
        localname="mixed", password=PASSWORD, email="Member@Example.COM"
    )
    token = EmailVerificationToken.mint(user)
    token.consume()
    user.refresh_from_db()
    assert user.email_verified is True


# --- 2F-1 gates nothing -------------------------------------------------


@pytest.mark.django_db
def test_an_unverified_account_still_signs_in():
    # The increment's own boundary, pinned rather than asserted in prose. The
    # gate is 2F-3 and lives in a custom auth backend; if any part of it had
    # leaked into the model layer, this is where it would show.
    user = _member(localname="unverified_member")
    assert user.email_verified is False
    assert (
        ModelBackend().authenticate(
            None, username="unverified_member", password=PASSWORD
        )
        == user
    )


@pytest.mark.django_db
def test_verification_does_not_widen_or_narrow_is_active():
    # R102b pins ``is_active`` to suspension and ban. Verification is a
    # third, unrelated state and must not move it in either direction — the
    # three refusals have to stay distinguishable.
    user = _verify(_member(localname="verified_member"))
    assert user.is_active is True

    user.suspend()
    user.refresh_from_db()
    assert user.is_active is False
    # Suspension says nothing about the address, and vice versa.
    assert user.email_verified is True


@pytest.mark.django_db
def test_deleting_an_account_takes_its_tokens_with_it():
    # CASCADE: a token outliving its subject is a credential pointing at
    # nothing. (Contrast ``Invite.used_by``, which is SET_NULL so a spent
    # seat stays spent — a different question, answered differently.)
    user = _member()
    EmailVerificationToken.mint(user)
    EmailVerificationToken.mint(user)
    # Held before the delete: ``user.delete()`` clears the pk, and a deleted
    # instance cannot be used in a filter.
    user_id = user.pk
    assert EmailVerificationToken.objects.filter(user_id=user_id).count() == 2
    user.delete()
    assert EmailVerificationToken.objects.filter(user_id=user_id).count() == 0


@pytest.mark.django_db
def test_the_readout_names_which_state_a_token_is_in():
    # Cheap, and it is the string the admin's token inline (R123) renders —
    # the thing that lets an admin see why a member is stuck without being
    # able to un-stick them by hand.
    user = _member()
    live = EmailVerificationToken.mint(user)
    assert "(live)" in str(live)

    spent = EmailVerificationToken.mint(user)
    spent.consume()
    assert "(used)" in str(spent)

    stale = EmailVerificationToken.mint(user)
    stale.expires_at = timezone.now() - timedelta(minutes=1)
    stale.save(update_fields=["expires_at"])
    assert "(expired)" in str(stale)

    # ``live`` was superseded in the database by the second mint; the
    # in-memory instance knows nothing about it until it is re-read.
    live.refresh_from_db()
    assert "(superseded)" in str(live)
