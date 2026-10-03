"""Auth views: index, signup, the first-run setup wizard (PLAN.md §3.7),
and the user profile surface (M5).

First-run flow (R12): a fresh instance with no superuser redirects both the
index and signup to /setup/, where the first account is created as the
instance admin. Once a superuser exists, signup follows the site settings'
policy (§3.7): open, or closed until an admin creates the account.

The profile page (M5) takes over the human side of the actor URL (R40):
ActivityPub clients still get the Person document by content negotiation,
but browsers now get a real profile (avatar, bio, films link) instead of a
redirect to the films page. Remote mirrors are profiled too — their route
matches the <preferredUsername>@<netloc> localname, and a missing avatar or
bio triggers one throttled best-effort fetch of the home Person document.

The profile also carries the follow relationship (M5 increment 2): a local
target is a plain M2M change (same instance — no delivery), a remote target
goes through the signed Follow / Undo(Follow) delivery (M4). And ``/find/``
resolves a ``user@domain`` handle to a profile — same-domain handles
directly, other domains via webfinger (RFC 6454) — without auto-following:
one POST changes at most one relationship, and the follow action itself
lives on the profile page.
"""

from urllib.parse import urlparse

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import transaction
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseGone,
    HttpResponseNotAllowed,
    JsonResponse,
)
from django.shortcuts import redirect, render, reverse
from django.views.decorators.http import require_POST

import reeltalk
from reeltalk.activitypub.follow import follow_user, unfollow_user
from reeltalk.activitypub.identity import (
    absolute_uri,
    accepts_activitypub,
    person_document,
)
from reeltalk.activitypub.mirrors import (
    RemoteFetchError,
    ensure_mirror,
    refresh_mirror_profile,
    webfinger_actor_url,
)
from reeltalk.core.models import (
    FEED_PAGE_SIZE,
    Shelf,
    feed_entries,
    popular_genres,
    trending_films,
)
from reeltalk.core.utils import render_markdown
from reeltalk.moderation.decorators import can_act_on
from reeltalk.moderation.models import report_state
from reeltalk.notifications.models import Notification, notify
from reeltalk.proxy_trust import client_ip

from . import attempts
from .forms import (
    PasswordResetConfirmForm,
    PasswordResetRequestForm,
    ProfileForm,
    ResendVerificationForm,
    SignupForm,
)
from .models import (
    AddressMismatchError,
    CredentialAttempt,
    EmailVerificationToken,
    Invite,
    SiteSettings,
    User,
)
from .password_reset import (
    RESET_PATH,
    classify_reset_code,
    complete_reset,
    request_reset,
)
from .verify import RESEND_PATH, request_resend, send_verification_email

# The routes that must stay reachable through R119's gate, by name.
#
# The gate lives in ``user_can_authenticate``, so by construction it does not
# intercept a logged-out route — and "by construction" is precisely the kind
# of thing that stops being true the day someone adds ``login_required`` to
# the resend view to "tidy it up", silently deleting the only exit from the
# lockout. The peer read of Mastodon is why this list exists: its gate is
# global and every page an unconfirmed user must reach is carved out
# explicitly, including the sign-in page itself, *"so the gate can't bounce
# you out of the login flow itself"*.
#
# A test walks this list and drives each route anonymously with an unverified
# account in the database, asserting none of them bounce to the login page.
# Adding one of these verbs to any of these routes turns that test red, which
# is the whole point of naming the exemption rather than relying on it.
#
# The 2G reset routes belong here for the same reason the resend route does,
# and one reason more besides. R119 means the person who needs a password
# reset is by definition someone who cannot sign in, so a reset page behind
# ``login_required`` could not rescue the case it exists for — the same
# argument R122 makes for resend. And because an *unverified* account is
# refused a reset (owner decision, 2026-10-02), the reset page is the only
# place such a member is ever told that; if it bounced to sign-in, the
# refusal would be delivered as a redirect loop rather than a sentence.
GATED_EXEMPT_URLS = (
    "login",
    "verify-link",
    "verify-resend",
    "password-reset",
    "password-reset-confirm",
    "signup",
    "setup",
)


def has_admin() -> bool:
    return User.objects.filter(is_superuser=True).exists()


def index(request):
    if not has_admin():
        return redirect("setup")
    # The home rail (M6 artwork C, R61) is instance-wide and public: its links
    # go only to pages an anonymous visitor can already open (film pages, the
    # genre subfeed). Counts stay the same for every viewer — a blocked user's
    # review still counts toward a film's tally; only the lists hide them.
    data = {
        "site": SiteSettings.get_instance(),
        "trending": trending_films(),
        "genres": popular_genres(),
    }
    if request.user.is_authenticated:
        # The v0.1 timeline (§3.6/§3.7): shelf events + statuses (R33/R35);
        # anonymous visitors get the sign-up CTA in the feed's place.
        #
        # Page 1 of the cursor-paged feed (§2I increment 2, R132). The page
        # size is a first-paint knob only — increment 3 loads the rest on
        # scroll — but the "Older" link is rendered from the same
        # ``next_cursor`` whether or not that JS is running, which is what
        # decision 6 asks for: one template, one link, and JS changing only
        # how it gets triggered, never whether the page works without it.
        entries, next_cursor = feed_entries(
            request.user,
            limit=FEED_PAGE_SIZE,
            cursor=request.GET.get("c") or None,
        )
        data["feed"] = entries
        data["next_cursor"] = next_cursor
    return render(request, "home.html", data)


@login_required
def feed_page(request):
    """One page of the home feed as a bare HTML fragment (§2I increment 3).

    The endless scroll fetches this and appends it into the page's existing
    ``<ul class="review-list">``. It renders the same row partial the home page
    renders, which is the point: the ``.review-open`` overlay, its ``status_id``
    gate and its ``aria-label`` cannot drift from a copy, because there is no
    copy (§2I decision 4, and the reason JSON + client templating was rejected).

    ``login_required`` rather than an inline check: this is the member's own feed,
    and an anonymous fragment request has no answer that is not a redirect.

    No pushed URL and no session state (§2I decision 5) — the page a reader has
    scrolled to is not a place worth naming, and sharing a specific post already
    has ``/status/<id>/``.
    """
    entries, next_cursor = feed_entries(
        request.user,
        limit=FEED_PAGE_SIZE,
        cursor=request.GET.get("c") or None,
    )
    return render(
        request,
        "_feed_page.html",
        {"feed": entries, "next_cursor": next_cursor},
    )


def about(request):
    """Instance info page (§3.7 v0.1): name, domain, software, version."""
    return render(
        request,
        "about.html",
        {
            "site": SiteSettings.get_instance(),
            "domain": settings.DOMAIN,
            "version": reeltalk.__version__,
        },
    )


def welcome(request):
    """Getting-started page (M6): the core loop for new users.

    Static and public (Letterboxd-style, owner decision) — anonymous visitors
    read it with a signup CTA; signed-in users see the same four steps with
    live links. Reached right after signup and from the footer on every page.
    """
    return render(request, "welcome.html")


def signup(request):
    if not has_admin():
        return redirect("setup")
    if request.user.is_authenticated:
        return redirect("index")
    site = SiteSettings.get_instance()
    if site.signup_policy != SiteSettings.OPEN:
        # Invite-only: the door is this page plus a link. R82 gave a member
        # something to send, so the closed notice now says where the link
        # comes from instead of just refusing.
        return render(request, "signup.html", {"closed": True})

    if request.method == "POST":
        ip = client_ip(request)
        # Refuse before counting or creating anything once this address has
        # spent the signup budget. The page is rendered from a fresh, empty
        # form with the wait as a flash message — NOT the submitted form
        # re-bound and re-validated — because re-validating would run the
        # name/email uniqueness checks and print "that name is already
        # taken", which is exactly the enumeration answer the throttle
        # exists to stop being harvested at volume. The empty form discards
        # the typed values: an accepted cost of a blocked state.
        if attempts.attempts_blocked(CredentialAttempt.SIGNUP, ip):
            messages.error(
                request,
                attempts.reveal_message(
                    attempts.retry_at(CredentialAttempt.SIGNUP, ip)
                ),
            )
            return render(request, "signup.html", {"form": SignupForm()})

        form = SignupForm(request.POST)
        valid = form.is_valid()
        # Count the attempt whenever it reached a uniqueness lookup —
        # whether the name came back free or taken — so a bot walking a
        # list of names through the oracle is bounded on the walk itself,
        # not only on the accounts it actually manages to create. A
        # submission that never reached a lookup (a name that failed the
        # format regex first) set no flag and is not counted.
        if form.reached_uniqueness_check:
            attempts.record_attempt(CredentialAttempt.SIGNUP, ip)
        if not valid:
            return render(request, "signup.html", {"form": form})

        data = form.cleaned_data
        user = User.objects.create_user(
            localname=data["localname"],
            email=data.get("email", ""),
            password=data["password1"],
            display_name=data.get("display_name", ""),
        )
        # Ask the new account to prove its address (2F-2). One helper rather
        # than a send inlined at each of the three creation routes: three
        # call sites is three places to forget, and forgetting here is silent
        # — the account still appears, it just never gets a link. Nothing
        # sends in this request; the guard that shouts when the backend
        # cannot deliver is the only part that runs here.
        send_verification_email(user, request_ip=ip)
        # **Signup no longer logs you in** (R119). It cannot: the account has
        # no proof of its address yet, and the gate that refuses it is the
        # same one this feature exists to make meaningful. Logging in here
        # would mean either an account that gets a session it is not allowed
        # to have, or a gate with a hole punched in its own creation path.
        #
        # The person who just signed up is sent to the resend page, which is
        # deliberately the same page that recovers a lost link — so the first
        # thing a new member sees is the thing they will need if the mail
        # never arrives. R119 and R122 are a pair: the gate is only
        # acceptable because this exit exists.
        messages.success(
            request,
            "Almost there — we sent a verification link to your email "
            "address. Click it to finish, or ask for another one below.",
        )
        return redirect(RESEND_PATH)
    return render(request, "signup.html", {"form": SignupForm()})


def setup(request):
    """First-run wizard: create the instance's first admin account."""
    if has_admin() or request.user.is_authenticated:
        return redirect("index")
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        user = User.objects.create_superuser(
            localname=data["localname"],
            email=data.get("email", ""),
            password=data["password1"],
            display_name=data.get("display_name", ""),
        )
        # Same single helper as signup (2F-2). The first-run wizard is the
        # worst case for a silent non-send: this is the account that owns
        # the instance, and PROGRESS §2F's deploy note says the mail config
        # has to be in place before it runs, because with the console
        # backend the guard shouts here — in the request the admin is
        # standing in — rather than three increments later.
        send_verification_email(user, request_ip=client_ip(request))
        # The wizard no longer logs the new admin in, and **this is the
        # sharpest edge in the whole increment**. ``has_admin()`` is now true,
        # so the wizard is gone; the admin cannot sign in until they click
        # the link; and if ``EMAIL_*`` was not configured before this ran,
        # there is no /admin/ to go configure it from. The guard above is
        # what makes that loud rather than silent, and the resend page below
        # is the way back out — but the requirement is to configure mail
        # *first*, not to recover afterwards.
        messages.success(
            request,
            "Instance set up. Check the email address you used to create "
            "this admin account and click the verification link — you "
            "cannot sign in until you do.",
        )
        return redirect(RESEND_PATH)
    return render(request, "social/setup.html", {"form": form})


@require_POST
@login_required
def invite_create(request):
    """Mint a single-use invite link for the signed-in user (R82).

    POST-only because it is not idempotent — every click makes a new code,
    and a GET that mints would let a link on a page (or a browser prefetch)
    spend the inviter's allowance by accident.

    The new code comes back on the inviter's *own* profile as a query
    param rather than being rendered here, so the mint follows the same
    redirect-then-render shape the rest of the POST surfaces use. Rendering
    it is gated again in ``_invite_context`` on the code belonging to the
    requester, so a bookmarked or forwarded ``?invite=`` on someone else's
    profile shows nothing at all.
    """
    if not SiteSettings.get_instance().may_send_invites(request.user):
        messages.error(
            request,
            "This instance only lets its admins send invites — ask one to "
            "send you a link.",
        )
        return redirect("user-profile", localname=request.user.localname)
    invite = Invite.mint(request.user)
    profile = reverse("user-profile", kwargs={"localname": request.user.localname})
    return redirect(f"{profile}?invite={invite.code}")


def invite_accept(request, code):
    """The invitee's landing page: ``/invite/<code>/`` → sign up (R82).

    Public by necessity — the person at the other end of the link does not
    have an account yet, that being the entire point of the link. The code
    is therefore the only credential in this flow, which is why it is long
    and random (``Invite.mint``) and why the redemption below holds a row
    lock across creating the account: without the lock, two people opening
    one link at the same instant would both pass the liveness check before
    either marked it spent, and an invite-only instance would hand out a
    second seat the owner never approved.

    Deliberately independent of ``signup_policy``. A valid link works whether
    the instance is open or invite-only — the link is a stronger statement
    than the setting, and making it depend on the setting would mean an owner
    flipping signup open silently invalidated every link already sent.
    """
    if request.user.is_authenticated:
        messages.info(request, "You already have an account on this instance.")
        return redirect("index")
    invite = Invite.objects.filter(code=code).first()
    if request.method != "POST":
        if invite is None:
            return render(
                request,
                "signup.html",
                {"invite_error": "That invite link is not valid."},
            )
        reason = invite.unusable_reason()
        if reason is not None:
            return render(request, "signup.html", {"invite_error": reason})
        return render(request, "signup.html", {"form": SignupForm(), "invite": invite})

    if invite is None:
        return render(
            request, "signup.html", {"invite_error": "That invite link is not valid."}
        )
    form = SignupForm(request.POST)
    if not form.is_valid():
        # Re-render with the invite still shown: the link is fine, the form
        # is not, and dropping the invite here would read as the link having
        # broken.
        return render(request, "signup.html", {"form": form, "invite": invite})
    data = form.cleaned_data
    with transaction.atomic():
        claimed = Invite.lock_live(code)
        if claimed is None:
            # Lost the race, or the window closed while the form was open.
            return render(
                request, "signup.html", {"invite_error": invite.unusable_reason()}
            )
        user = User.objects.create_user(
            localname=data["localname"],
            email=data.get("email", ""),
            password=data["password1"],
            display_name=data.get("display_name", ""),
        )
        claimed.redeem(user)
        # Inside the transaction on purpose (2F-2): the ``on_commit`` in the
        # helper means the mail is only queued if the account and the
        # invite's redemption both landed. A send scheduled before the
        # commit could go out for an account that never existed.
        send_verification_email(user, request_ip=client_ip(request))
    # No ``login()`` here either (R119). A redeemed invite is a seat, not a
    # proven mailbox — the invite proved that *someone* was sent the link, and
    # this account's address still has to be proved by its own click. An
    # invite-only instance is exactly where this matters most: without the
    # gate the seat is live on the inviter's word alone.
    messages.success(
        request,
        "Your invite is used and your account is created. Check your email "
        "and click the verification link to sign in.",
    )
    return redirect(RESEND_PATH)


def verify_link(request, code):
    """Spend a verification link: ``GET /account/verify/<code>/``.

    The whole point of the feature is the click, so this view is the moment
    R123's provenance is actually created — every verified flag on this
    instance traces to a request that landed here with a real token.

    **Reachable while logged out, by necessity** — see ``GATED_EXEMPT_URLS``
    below for the exemption this depends on and why naming it matters.

    **The copy here is specific, and the resend page's is not.** That
    asymmetry is deliberate (peer finding 8): enumeration risk lives where an
    unauthenticated caller submits an *address*. Reaching this page at all
    requires a ~192-bit token nobody can guess, so whoever reads "your link
    expired on such-and-such a date" already holds a real credential and is
    entitled to know what is wrong with it. Telling them that, with a link
    to get a new one, is the difference between a recoverable annoyance and
    a dead end.

    **Single-use is enforced by the row lock, not by a check.** ``lock_live``
    takes a write lock inside the transaction across the liveness test and the
    spend, because two things click verification links at once all the time
    in practice — a person and their mail client's link scanner.
    """
    if request.method != "GET":
        return HttpResponseNotAllowed(permitted_methods=["GET"])

    verified_localname = None
    problem = None
    # A machine-readable outcome so the template can give the *right* help
    # rather than pattern-matching on sentence text. "already used" in
    # particular needs different advice from "invalid": a mail client's link
    # scanner spends a single-use token without a human ever seeing it, and a
    # member who is told they used their own link when they did not will not
    # believe it.
    outcome = "verified"
    with transaction.atomic():
        token = EmailVerificationToken.lock_live(code)
        if token is None:
            # ``lock_live`` is deliberately silent about *why*. Look the row
            # up to report it; an unknown code and an unusable one get
            # different sentences, and both are safe to say because both
            # required a token to ask.
            looked_up = EmailVerificationToken.objects.filter(code=code).first()
            if looked_up is None:
                outcome = "invalid"
                problem = "That verification link is not valid."
            else:
                reason = looked_up.unusable_reason()
                problem = reason or "That verification link is not valid."
                if looked_up.used_at is not None:
                    outcome = "used"
                elif looked_up.superseded_at is not None:
                    outcome = "superseded"
                elif not looked_up.is_live:
                    outcome = "expired"
                else:
                    outcome = "invalid"
        else:
            try:
                token.consume()
            except AddressMismatchError:
                # The address moved after this link was sent (R120's binding).
                # The member is not stranded — the resend page mints against
                # whatever the address is now.
                outcome = "mismatch"
                problem = (
                    "That link was sent to a different address than the one "
                    "on this account now. Ask for a new link and it will go "
                    "to the current one."
                )
            else:
                verified_localname = token.user.localname
                # Anti-session-fixation across the boundary the peer applies
                # (finding 4): verification changes what the account *is*,
                # so the session that arrived with it should not survive
                # unchanged. In our shape the clicker is usually logged out
                # — R119 makes that so — but the policy is applied rather
                # than skipped because "usually" is not "always": a signed-in
                # visitor can open one of these too, and a key that predates
                # a privilege change is a key that should not be reused.
                request.session.cycle_key()

    return render(
        request,
        "social/verify_result.html",
        {
            "ok": verified_localname is not None,
            "reason": problem,
            "outcome": outcome,
        },
    )


def verify_resend(request):
    """The logged-out recovery page: ``/account/verify/resend/`` (R122).

    Two jobs in one page. Its **GET** half is where signup, the setup wizard
    and invite acceptance now send a person — "check your email" — and its
    **POST** half is the rate-limited resend that rescues a lost or expired
    link. They are the same page because they are the same moment: someone who
    does not have a session and needs a link.

    **Every POST outcome renders the same sentence.** Not "similar" copy —
    the same page with the same words, whether the address exists, was just
    sent a link, is inside its cooldown, or tripped the per-source limit.
    This is the one surface in the increment where an anonymous caller
    submits an address, so anything that varies here becomes an oracle for
    whether an address is registered. The cost is real and accepted: someone
    who mistypes their address here gets no signal that they did. Where that
    information *does* live is the admin's token view, which is the point of
    recording the reason in the log rather than showing it.
    """
    if request.method == "POST":
        form = ResendVerificationForm(request.POST)
        if form.is_valid():
            request_resend(form.cleaned_data["email"], client_ip(request))
            # Redirect rather than render, so refreshing the page cannot fire
            # a second send. The uniform sentence rides in the flash message.
            messages.success(
                request,
                "If that address has an account here, a verification link "
                "is on its way. If it does not arrive within a few minutes, "
                "try again.",
            )
            return redirect(RESEND_PATH)
    else:
        form = ResendVerificationForm()
    return render(request, "social/verify_form.html", {"form": form})


# The one sentence every reset request gets, whoever it was about. It does
# not promise a link, because for some of the addresses submitted here no
# link is coming and this page must not say otherwise. It points at the
# note above the form — which says the verification precondition to every
# visitor in the same words, so it discloses nothing about any particular
# address — rather than repeating it, and it hands the leftovers to the
# human who can actually do something.
#
# The alternative that looks tidier and is worse: vary the answer by what
# was found. "That account is not verified" is a three-way enumeration
# oracle over every address on the instance, handed out for free.
UNIFORM_RESET_MESSAGE = (
    "If that address can reset its password, a reset link is on its way. "
    "Check the inbox and the spam folder. If nothing arrives, the note "
    "above this form explains the other reasons a reset is not possible, "
    "and the site administrator can help."
)


def password_reset_request(request):
    """The logged-out "I forgot my password" page (2G).

    Mirrors :func:`verify_resend` in shape because it is the same kind of
    page: logged out, takes an address, must not answer a question about it.

    **The page says the verification precondition out loud, to everyone,
    before anyone types.** That is what keeps the uniform answer honest.
    An unverified account gets no reset link — a new password would not
    open it, since R119 refuses sign-in until the address is proven — and
    the only way to tell that without building an enumeration oracle is to
    say it in copy that is identical whoever is reading it. The cost is
    that a verified member reads a sentence they do not need. That is a
    much smaller price than the alternative.
    """
    if request.method == "POST":
        form = PasswordResetRequestForm(request.POST)
        if form.is_valid():
            request_reset(form.cleaned_data["email"], client_ip(request))
            # Redirect rather than render, so a refresh cannot fire a second
            # send. The uniform sentence rides in the flash message.
            messages.success(request, UNIFORM_RESET_MESSAGE)
            return redirect(RESET_PATH)
    else:
        form = PasswordResetRequestForm()
    return render(request, "social/password_reset_form.html", {"form": form})


def password_reset_confirm(request, code):
    """Open or spend a reset link: ``/account/password-reset/<code>/`` (2G).

    **GET opens the form; only the POST spends the link.** The link in the
    mail is a credential that has already been copied through a mail
    server, a mail client, and possibly a link scanner. Letting the GET
    *be* the change would mean that credential alone was enough to set a
    password. Here it only unlocks a form, and the thing that actually
    writes is a submission carrying a password nobody else has seen.

    **The copy on this page is specific, and the request page's is not** —
    the same asymmetry R122 draws. Reaching this page at all takes an
    unguessable ~256-bit code, so whoever reads "your link expired on such
    a date" already holds a real credential and is entitled to know what
    is wrong with it.

    **A successful reset does not sign anybody in.** The flow stays
    logged-out end to end so that a session on this instance is only ever
    created by the login form, and the rule that a password change kills
    every session needs no carve-out for "except the one that just did the
    resetting". The member types their new password once more. That is the
    whole cost, and it buys the simpler invariant.
    """
    outcome, problem = classify_reset_code(code)
    if outcome != "live":
        return render(
            request,
            "social/password_reset_result.html",
            {"ok": False, "outcome": outcome, "reason": problem},
        )

    if request.method == "POST":
        form = PasswordResetConfirmForm(request.POST)
        if form.is_valid():
            result = complete_reset(code, form.cleaned_data["password1"])
            if result["changed"]:
                return render(
                    request,
                    "social/password_reset_result.html",
                    {"ok": True, "outcome": "done", "localname": result["localname"]},
                )
            # The link died between the GET that showed this form and the
            # POST that spent it — someone else clicked first, or it aged
            # out mid-form. Re-classify so the sentence matches the truth.
            outcome, problem = classify_reset_code(code)
            return render(
                request,
                "social/password_reset_result.html",
                {"ok": False, "outcome": outcome, "reason": problem},
            )
    else:
        form = PasswordResetConfirmForm()

    return render(
        request,
        "social/password_reset_confirm.html",
        {"form": form, "code": code},
    )


def _invite_context(request, profile_user) -> dict:
    """The invite block for a profile the visitor owns (R82).

    ``invite_link`` is only ever built for a code the requesting user
    minted themselves. That scoping is not cosmetic: without it this becomes
    an oracle that renders any code handed to it, and an attacker could use
    the rendered link to confirm a code they guessed actually exists.
    """
    can = SiteSettings.get_instance().may_send_invites(request.user)
    out = {"can_invite": can}
    code = request.GET.get("invite", "").strip()
    if not can or not code:
        return out
    invite = Invite.objects.filter(code=code, created_by=profile_user).first()
    if invite is not None:
        out["invite"] = invite
        out["invite_link"] = absolute_uri(f"/invite/{invite.code}/")
    return out


def _resolve_profile_user(localname):
    """The user behind a profile/films URL localname.

    Local accounts match case-insensitively (R40: case variants are one
    identity; signup rejects insensitive duplicates). Remote mirrors are
    stored exactly as <preferredUsername>@<netloc> and match verbatim — the
    two namespaces are disjoint ('@' is not in R12's local charset).
    """
    if "@" not in localname:
        return User.objects.filter(local=True, localname__iexact=localname).first()
    return User.objects.filter(local=False, localname=localname).first()


def user_profile(request, localname):
    """The human profile page at the actor URL (M5; supersedes R40's 302).

    ActivityPub clients keep the Person document (R40 content negotiation) —
    for local users only: a mirror's canonical document lives on its home
    instance (R42), so AP clients get 404 here. Browsers get the profile:
    avatar, display name + handle, bio, and the films link. A remote mirror
    missing an avatar or bio triggers one throttled best-effort fetch of the
    home Person document (``refresh_mirror_profile``) before rendering.
    """
    user = _resolve_profile_user(localname)
    if user is None:
        raise Http404("No such user")
    # A banned actor is **gone**, not hidden — and this sits above the
    # content-negotiation split so it answers for peers as well as browsers.
    # R102 asks for ``410 Gone`` by name, and the status code is the point:
    # it is the ActivityPub tombstone convention, so a peer that re-fetches
    # the actor id we just sent a ``Delete(Person)`` for gets the same answer
    # the delete told it. Serving the Person document here would contradict
    # the delete; serving a 404 would leave a gap between "never existed" and
    # "was removed", which is exactly the distinction the ban is making.
    #
    # This is why the lift does not live here. A suspended account still has a
    # page that shows it, so 4b put the unsuspend control on the profile. A
    # banned account deliberately has no public page at all, so the unban
    # control lives on ``/moderate/`` — the rule is that the lift lives on
    # the surface that still shows the account, and for a ban that surface is
    # the moderator's, not the public's.
    if user.banned_at is not None:
        return HttpResponseGone("This account has been removed.")
    if accepts_activitypub(request):
        if not user.local:
            return HttpResponse(status=404)
        return JsonResponse(
            person_document(user, request),
            content_type="application/activity+json",
        )
    # A suspended account's profile renders a **suspended state** rather
    # than 404ing, because R102 says so in as many words: "profile shows a
    # suspended state". That is the one place the plan asks for the account
    # to be *visible and explained* rather than hidden, and it is load-
    # bearing for two reasons beyond the visitor's benefit. A person who
    # cannot tell they were suspended cannot ask about it, and a moderator
    # needs a surface that still shows the account in order to lift it —
    # the queue has already drained.
    #
    # The AP arm above is deliberately reached first and still serves the
    # full Person document, now carrying ``suspended: true``. A peer has
    # to be able to fetch the actor id our ``Update(Person)`` points at;
    # 404ing it would make the broadcast unresolvable.
    if user.suspended_at is not None:
        return render(
            request,
            "social/profile_suspended.html",
            {
                "profile_user": user,
                "can_unsuspend": request.user.is_authenticated
                and can_act_on(request.user, user),
            },
        )
    if not user.local:
        refresh_mirror_profile(user)
        user.refresh_from_db()
    is_self = request.user.is_authenticated and request.user.pk == user.pk
    data = {"profile_user": user, "is_self": is_self}
    if is_self:
        # The invite box only lives on one's own profile (R82).
        data.update(_invite_context(request, user))
    if request.user.is_authenticated and not is_self:
        # The follow + block buttons' state (the template hides the controls
        # on one's own profile and for anonymous visitors).
        data["is_following"] = request.user.follows.filter(pk=user.pk).exists()
        data["is_blocked"] = request.user.blocks.filter(pk=user.pk).exists()
        # The report control (moderation increment 2) rides the same gate as
        # those two buttons. ``report_state`` is shared with the post page so
        # the two host pages cannot drift on who may report what, and it
        # returns R107's dedup state so the profile says "already reported"
        # rather than offering a button that would quietly do nothing.
        can_report, already_reported = report_state(request.user, target_user=user)
        data["can_report"] = can_report
        data["already_reported"] = already_reported
    if not user.local:
        # The home instance the mirror came from (the actor URL's netloc).
        data["home_instance"] = urlparse(user.actor_url).netloc
    return render(request, "social/profile.html", data)


def _change_follow(request, localname: str, *, undo: bool):
    """The follow / unfollow POST routes' shared body (M5 increment 2).

    Operates on a resolvable profile — the button only renders there, so an
    unresolvable handle is a 404 (you can't follow someone we don't know;
    discovery at /find/ is what creates an unknown remote's mirror first). A
    local target is a plain M2M change — same instance, no delivery. A remote
    target goes through the signed Follow / Undo(Follow) delivery (M4); the
    library finds the already-resolved mirror by actor_url, so the only failure
    mode is the delivery itself hitting a down home instance — a dead remote
    must not 500 the user's request (the same posture as status broadcasts):
    the local state stands and a warning says the remote's copy lags.
    """
    target = _resolve_profile_user(localname)
    if target is None:
        raise Http404("No such user")
    follower = request.user
    if follower.pk == target.pk:
        messages.error(request, "You can't follow yourself.")
    elif target.banned_at is not None and not undo:
        # Same rule as the suspension branch above: the route refuses what
        # the page withholds, and only the *follow* half. An unfollow stays
        # always-allowed because removing a subscription surfaces nothing and
        # must never be blocked by somebody else's moderation state.
        #
        # It matters more here than for a suspend, because a banned profile
        # 410s rather than rendering anything: without this clause the only
        # way to reach the follow route on a banned account is a hand-built
        # POST, and a hand-built POST that succeeds is the whole reason the
        # check exists. Following a banned account would also survive the
        # ban in a way nothing else does — the M2M row would sit there, and
        # if the ban were ever lifted the follower would start receiving a
        # feed they subscribed to against a 410.
        messages.error(request, "That account has been removed and cannot be followed.")
    elif target.suspended_at is not None and not undo:
        # R85's rule — the route refuses what the page withholds. The
        # suspended profile renders no follow control, so this is not a
        # button with no route; it is the route refusing a hand-built POST.
        # Only the **follow** half is refused: an unfollow is the removal of
        # an existing subscription and must always work, and a block is
        # defensive personal state that surfaces nothing. A new follow is
        # the one action here that would subscribe somebody to a hidden
        # account's future output — and on unsuspend they would wake up to
        # a feed from someone they never actually chose to follow.
        messages.error(request, "That account is suspended and cannot be followed.")
    elif target.local:
        if undo:
            follower.follows.remove(target)
            messages.success(request, f"You no longer follow {target.get_full_name()}.")
        else:
            follower.follows.add(target)
            # The followed member's notification (notifications increment 2,
            # R92). The local half of the follow pair — the federated half is
            # written by ``handle_follow`` on the receiving side. An unfollow
            # is not an event to announce, so only the add branch reaches it.
            notify(target, follower, Notification.Kind.FOLLOW)
            messages.success(request, f"You now follow {target.get_full_name()}.")
    elif undo:
        try:
            undone = unfollow_user(request, follower, target.actor_url)
        except requests.RequestException:
            # The M2M row is already removed locally; the Undo just couldn't
            # be delivered (v0.1 has no retry queue).
            messages.warning(
                request,
                f"You no longer follow {target.get_full_name()}, but their "
                "instance couldn't be reached — they may still see you as a "
                "follower.",
            )
        else:
            if undone is None:
                # Not following (no M2M row): nothing to undo, and the
                # library sends no spurious Undo.
                messages.info(
                    request, f"You were not following {target.get_full_name()}."
                )
            else:
                messages.success(
                    request, f"You no longer follow {target.get_full_name()}."
                )
    else:
        try:
            follow_user(request, follower, target.actor_url)
        except requests.RequestException:
            # The follow is recorded locally; the signed delivery to a down
            # home instance failed (v0.1 has no retry queue).
            messages.warning(
                request,
                f"You now follow {target.get_full_name()}, but their instance "
                "couldn't be reached — they may not have received the follow yet.",
            )
        else:
            messages.success(request, f"You now follow {target.get_full_name()}.")
    return redirect("user-profile", localname=target.localname)


@require_POST
@login_required
def user_follow(request, localname):
    """POST: follow the profile's user (M5 increment 2)."""
    return _change_follow(request, localname, undo=False)


@require_POST
@login_required
def user_unfollow(request, localname):
    """POST: unfollow the profile's user (M5 increment 2)."""
    return _change_follow(request, localname, undo=True)


def _change_block(request, localname: str, *, block: bool):
    """The block / unblock POST routes' shared body (M5 increment 3).

    Blocking is read-side state (R53/R54): it only changes the local
    ``User.blocks`` M2M — a blocked user's statuses and shelf events drop out
    of the blocker's feed, and their reviews disappear from film pages. There
    is no Block activity delivered over federation in v0.1 (accepted limit),
    so a remote target is handled exactly like a local one: a plain M2M change
    on this instance, nothing sent to the home instance. As with follow, only
    resolvable profiles can be blocked — an unknown handle 404s.
    """
    target = _resolve_profile_user(localname)
    if target is None:
        raise Http404("No such user")
    blocker = request.user
    if blocker.pk == target.pk:
        messages.error(request, "You can't block yourself.")
    elif block:
        blocker.blocks.add(target)
        messages.success(request, f"You have blocked {target.get_full_name()}.")
    else:
        blocker.blocks.remove(target)
        messages.success(request, f"You have unblocked {target.get_full_name()}.")
    return redirect("user-profile", localname=target.localname)


@require_POST
@login_required
def user_block(request, localname):
    """POST: block the profile's user (M5 increment 3)."""
    return _change_block(request, localname, block=True)


@require_POST
@login_required
def user_unblock(request, localname):
    """POST: unblock the profile's user (M5 increment 3)."""
    return _change_block(request, localname, block=False)


@login_required
def find_user(request):
    """Remote-user discovery (M5 increment 2): ``user@domain`` → their profile.

    A same-domain handle resolves the local account directly; any other
    domain goes through webfinger (RFC 6454) over real HTTP — the self
    link's actor URL becomes a mirror (created on first contact) and the
    user is redirected to its profile. Resolve-and-redirect only: submitting
    never follows anyone (the follow action lives on the profile page).
    """
    value = request.POST.get("q", "").strip() if request.method == "POST" else ""
    error = None
    if request.method == "POST":
        if value.count("@") != 1:
            error = "Enter a full handle: user@domain."
        else:
            localname, _, domain = value.partition("@")
            localname, domain = localname.strip(), domain.strip()
            if not localname or not domain:
                error = "Enter a full handle: user@domain."
            elif domain.lower() == settings.DOMAIN.lower():
                # Routed through the profile resolver rather than a
                # second local lookup of our own. This was a duplicate of
                # ``_resolve_profile_user``'s local branch, and a duplicate
                # is two places for a rule to drift — the classic case being
                # one of them gaining a suspension filter while the other
                # quietly keeps resolving. Now there is one answer, and a
                # suspended account found here lands on the profile that
                # says so rather than on a page that 404s unexpectedly.
                user = _resolve_profile_user(localname)
                if user is None:
                    error = f"No user named {localname!r} on this instance."
                else:
                    return redirect("user-profile", localname=user.localname)
            else:
                try:
                    actor_url = webfinger_actor_url(localname, domain)
                    mirror = ensure_mirror(actor_url)
                except RemoteFetchError:
                    error = (
                        f"Could not find {value} — check the handle and that "
                        "the instance is reachable."
                    )
                else:
                    return redirect("user-profile", localname=mirror.localname)
    return render(request, "social/find.html", {"q": value, "error": error})


@login_required
def profile_edit(request):
    """Profile editing for local users (M5): display name, bio, avatar."""
    user = request.user
    if not user.local:
        # Mirrors are edited on their home instance, never here.
        messages.error(request, "Your profile is managed on your home instance.")
        return redirect("index")
    if request.method == "POST":
        form = ProfileForm(request.POST, request.FILES)
        if form.is_valid():
            data = form.cleaned_data
            user.display_name = data["display_name"]
            raw_summary = data["summary"]
            user.raw_summary = raw_summary
            user.summary = render_markdown(raw_summary)
            if data["avatar"]:
                user.avatar = data["avatar"]
            user.save()
            messages.success(request, "Profile updated.")
            return redirect("user-profile", localname=user.localname)
    else:
        form = ProfileForm(
            initial={
                "display_name": user.display_name,
                "summary": user.raw_summary,
            }
        )
    return render(request, "social/profile_edit.html", {"form": form})


# The owner's 1,378-film watchlist crashed the browser rendered in one page
# (2026-09-10); ~50 rows keeps every tab light.
USER_FILMS_PAGE_SIZE = 50


def user_films(request, localname):
    """A user's films page: All / Watchlist / Watched (§3.3 rule 1, D1).

    Exactly three tabs — D1's binary model leaves no room for more. The view
    picks the tab's queryset and paginates it (~50/page); query/shelf logic
    lives on the User model. Works for local users and remote mirrors alike
    (mirrors receive their shelves from federation, R15).
    """
    profile = _resolve_profile_user(localname)
    if profile is None:
        raise Http404("No such user")
    if profile.banned_at is not None:
        # Answered here rather than redirected to the profile. A suspension
        # redirects because the profile has something to say and the tab
        # should show it; a ban has nothing to say anywhere, and a 302 into
        # a 410 would make the tab's own status code wrong.
        return HttpResponseGone("This account has been removed.")
    if profile.suspended_at is not None:
        # Redirect to the profile rather than 404 the tab. The profile is
        # where R102 says a suspended account explains itself, and a films
        # tab that 404s while the profile two segments up says "suspended"
        # is two answers where one would do. This also means the tab never
        # needs its own suspension template or its own rule — it inherits
        # the profile's.
        return redirect("user-profile", localname=profile.localname)
    tab = request.GET.get("tab", "all")
    if tab not in ("watchlist", "watched"):
        tab = "all"
    if tab == "watchlist":
        films = profile.films_on_shelf(Shelf.TO_READ)
    elif tab == "watched":
        films = profile.films_on_shelf(Shelf.READ)
    else:
        films = profile.all_films()
    paginator = Paginator(films, USER_FILMS_PAGE_SIZE)
    try:
        page_obj = paginator.page(request.GET.get("page"))
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        raise Http404("Page not found.") from None
    # Pagination links keep the active tab in the URL (the "all" tab has no
    # query param, matching the tab nav).
    page_query = "?" if tab == "all" else f"?tab={tab}&"
    return render(
        request,
        "social/user_films.html",
        {
            "profile_user": profile,
            "tab": tab,
            "films": page_obj.object_list,
            "page_obj": page_obj,
            "page_query": page_query,
        },
    )
