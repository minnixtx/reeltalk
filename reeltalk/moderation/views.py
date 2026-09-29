"""The moderation surface: the queue, the report controls, the delete action.

Three different audiences share this module and they are gated differently.
``index``, ``dismiss`` and ``delete_status`` are **moderator-only** (R101:
anonymous → 302, signed-in non-moderator → 403). ``report_status`` and
``report_user`` are **member-only** — a report is something any member
files, and gating it on moderation would mean only moderators could report
anything.

``delete_status`` carries a second gate on top of the moderator one.
``moderator_required`` says you may open the queue; ``can_act_on`` (R103)
says the thing in front of you is yours to destroy, and a moderator who can
see a superuser's post must not be able to delete it. Both are checked in
the view, because a control hidden in a template is not an enforced rule.

**This module federates as of increment 3.** A moderator delete sends a
real ``Delete`` to the reported post author's remote followers. Increment
6 adds the outward half: ``forward`` sends a ``Flag`` about a remote
account to its home instance, signed by the instance representative so the
member who reported never appears on the wire (R104).
"""

from urllib.parse import urlparse

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from reeltalk.activitypub.broadcast import (
    broadcast_actor_update,
    broadcast_person_delete,
    broadcast_report_flag,
    broadcast_status_delete,
)
from reeltalk.core.models import Status
from reeltalk.moderation.decorators import (
    can_act_on,
    can_impose_severity,
    moderator_required,
)
from reeltalk.moderation.forms import ReportForm
from reeltalk.moderation.models import (
    DomainBlock,
    Report,
    ban_reported_member,
    delete_reported_status,
    dismiss_report,
    file_report,
    normalize_domain,
    record_federation_outcome,
    record_forward_outcome,
    suspend_reported_member,
)
from reeltalk.moderation.models import block_domain as apply_domain_block
from reeltalk.moderation.models import unblock_domain as lift_domain_block
from reeltalk.moderation.representative import (
    INSTANCE_ACTOR_LOCALNAME,
    instance_representative,
)
from reeltalk.social.models import User
from reeltalk.social.views import _resolve_profile_user

# Cards, not rows (R107: staff are told once per unresolved *target*).
# A 25-card page is 25 things to decide on; the reports under each card are
# a handful of short lines, so the page stays readable even when a target
# is piled on.
QUEUE_PAGE_SIZE = 25


@moderator_required
def index(request):
    """``/moderate/`` — the unresolved queue, one card per reported target.

    Grouped by target rather than listed per report, because R107 says staff
    are told **once per unresolved target, not once per report**. Three
    members reporting the same post is one decision with three pieces of
    evidence, not three decisions; rendering it as three rows would put the
    same content on screen three times and ask the moderator to make the
    same call three times.

    Only unresolved reports appear. There is no "resolved" tab in this
    increment — the resolved rows are the audit trail (R106) and stay
    queryable, but the queue is work, and work that is done is not on it.
    """
    paginator = Paginator(queue_cards(request.user), QUEUE_PAGE_SIZE)
    try:
        page_obj = paginator.page(request.GET.get("page"))
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        raise Http404("Page not found.") from None
    return render(
        request,
        "moderation/index.html",
        {
            "cards": page_obj.object_list,
            "page_obj": page_obj,
            "page_query": "?",
            "resolved_count": Report.objects.filter(resolved_at__isnull=False).count(),
            "banned_accounts": banned_accounts(request.user),
            # The constant, not the row. The forward drawer names the
            # instance actor on every render of the queue, and resolving
            # that row means ``get_or_create`` — a write on a GET, on a
            # page a moderator may reload repeatedly, for an identity that
            # is fixed by a module constant anyway. The row is created by
            # the forward POST, which is the first moment it is actually
            # needed and the only moment it is actually used.
            "instance_localname": INSTANCE_ACTOR_LOCALNAME,
            "blocked_domains": DomainBlock.objects.select_related("created_by"),
        },
    )


def banned_accounts(actor):
    """Every banned local account this actor may lift, most recent first.

    The lift surface, and it exists because the ban took the other one away.
    A suspension keeps a public profile that explains itself, so 4b put the
    unsuspend control there. A ban deliberately leaves **no** public page —
    the profile is ``410 Gone``, which is the whole point of the action,
    not an oversight. That means the only surface still showing a banned
    account is a moderator's, and without a surface there is no lift: an
    action nobody can see is an action nobody can undo.

    Filtered by ``can_act_on`` here rather than in the template, so a
    moderator sees exactly the bans they may lift and no others, and the
    route re-checks the same function independently. A list that showed a
    ban the viewer cannot lift would be a control that 403s, which is the
    pattern this arc keeps refusing.

    Local accounts only. A remote mirror cannot be banned by us at all, so
    there is never a remote row here to wonder about.
    """
    return [
        user
        for user in User.objects.filter(banned_at__isnull=False, local=True).order_by(
            "-banned_at"
        )
        if can_act_on(actor, user)
    ]


def queue_cards(actor):
    """One card per unresolved target, most recently reported first.

    Built in Python over the unresolved set rather than with a grouped SQL
    query. The grouping key is ``target_key`` — a ``(user, status)`` pair —
    and no aggregate in the ORM returns that as a usable identity without
    either a ``values()`` row of bare ids (which the template then cannot
    render) or a second query per card. The unresolved set is the honest
    bound: it is drained by acting on it, and R107's dedup keeps it from
    growing by reporter alone.

    Card order is by **latest** report, not first: a target that just drew
    a third reporter is more urgent than one that has sat untouched for a
    week, and the newest report is the one that changed the picture.

    Each card carries ``can_delete`` and ``can_suspend`` — whether *this*
    actor may take that action on this card — computed here so R103 has
    exactly one implementation. The template reads booleans and does not
    get to decide a permission; a moderator and the site admin looking at
    the same card get different buttons out of the same function, which is
    what "the guard reads the actor, not the surface" looks like on a page.

    ``can_suspend`` adds two conditions on top of ``can_act_on``, and both
    are about what suspend *is* rather than who is asking.
    ``target_user.local`` because R102 defines suspend and ban as actions
    on an account we own — we cannot suspend a remote account at its home
    instance, and we could not even sign the actor update for one. And
    ``suspended_at is None`` because suspending an already-suspended
    account is not a decision; the control that lifts one lives on the
    profile, not on a queue card that this action drains away.
    """
    cards = {}
    for report in Report.unresolved().select_related(
        "reporter", "target_user", "target_status", "target_status__film"
    ):
        # ``Report.unresolved()`` carries Meta.ordering = ["-created"], so
        # first-seen order over it is latest-report-first per target.
        card = cards.get(report.target_key)
        if card is None:
            status = report.target_status
            target = report.target_user
            card = {
                "target_user": target,
                "target_status": status,
                "reports": [],
                "latest": report,
                # A tombstone has nothing left to remove, and R103 decides
                # everything else. Checked against ``target_user`` rather
                # than ``status.user`` because ``Report.clean()`` already
                # pins a status report's target to the status's author —
                # asking the same question of either half is the same
                # question, and the target is the one the card is about.
                "can_delete": bool(status)
                and not status.deleted
                and can_act_on(actor, target),
                "can_suspend": target.local
                and target.suspended_at is None
                and can_impose_severity(actor, target),
                # Ban sits beside suspend rather than above it, and the two
                # conditions differ in exactly one column on purpose. Ban
                # requires the same thing suspend requires — a local account
                # this actor may act on, not already under that state — and
                # reads ``banned_at`` for the last part rather than
                # ``suspended_at``. A banned account is not "more suspended",
                # and a control keyed on the wrong column would offer to ban
                # someone already banned while hiding the ban itself.
                "can_ban": target.local
                and target.banned_at is None
                and can_impose_severity(actor, target),
                # Forward is the mirror image of the other four: it is the
                # only card action aimed *outward* at a remote account, and
                # the only one that decides nothing. The three conditions
                # are each a different reason the button would be a lie.
                # ``not target.local`` because a local account has no home
                # instance to tell. ``actor_url`` because without a home
                # actor URL there is no inbox to address — a remote mirror
                # always has one, so this catches only a half-written row.
                # And ``suspended_at is None`` because ``_deliver_signed``
                # skips a suspended recipient without recording a failure:
                # drawing the control there would mean a click that returns
                # "forwarded" having sent nothing at all, which is R88's
                # blindness wearing a button.
                "can_forward": (not target.local)
                and bool(target.actor_url)
                and target.suspended_at is None
                and can_act_on(actor, target),
                # Refuse is the per-account half of the generalisation the
                # owner chose for §2D's local/remote question: we cannot
                # suspend a remote account at its home instance, but we
                # can refuse it on *this* one, and refusing it is the same
                # suspension the domain block applies — just aimed at one
                # mirror instead of a whole host. The condition is
                # ``can_suspend`` with ``local`` inverted, and the two must
                # stay exact mirrors of each other: exactly one of the two
                # controls is ever offered for any target, and a card that
                # offered both would be a card where the moderator has to
                # work out which verb they meant.
                "can_refuse": (not target.local)
                and target.suspended_at is None
                and can_act_on(actor, target),
            }
            cards[report.target_key] = card
        card["reports"].append(report)
    return list(cards.values())


@moderator_required
@require_POST
def dismiss(request, report_id):
    """Dismiss a card: resolve its whole unresolved pile (R106/R107).

    The route takes one report id because that is the handle the card was
    built from, but it acts on the **target**, not the row. Dismissing one
    of three reports about the same post and leaving two open would put the
    same card back on the queue on the next page load, which is the
    re-notification R107 exists to prevent.

    Records who dismissed, when, and the note (R106) on every row in the
    pile. There is no separate audit table to write — the report row is the
    record.
    """
    report = get_object_or_404(Report, id=report_id)
    note = request.POST.get("note", "").strip()
    count = dismiss_report(report, by_user=request.user, note=note)
    subject = "post" if report.target_status_id else "member"
    if count > 1:
        messages.success(request, f"Dismissed {count} reports about this {subject}.")
    else:
        messages.success(request, "Report dismissed.")
    return redirect("moderation")


@moderator_required
@require_POST
def delete_status(request, report_id):
    """Delete the reported post from the queue (increment 3, R103/R106).

    Two gates run before anything is written, and they answer different
    questions. ``moderator_required`` says the visitor may be here at all;
    ``can_act_on`` (R103, as amended R103b) says this particular post is
    theirs to destroy. A moderator may delete only a **regular user's**
    post: never the site admin's, never a peer moderator's, never an
    account holding the ``/admin/`` door, never their own. The site admin
    may delete anyone's. Both are enforced here rather than only reflected
    in which button the template drew, because a hidden control is a
    suggestion and this route is the one that deletes things.

    **A tombstone is not an actionable object.** A report whose post is
    already gone 404s rather than re-deleting it, matching ``report_status``
    excluding deleted statuses: the author's own delete already tombstoned
    it and already broadcast the ``Delete``, so a second one here would
    claim work that was already done. The card is still dismissible — a
    moderator can still close the report — so nothing dead-ends.

    **A resolved report is not a live delete handle.** The route acts on
    open work only. Without this, a report id stays a way to destroy a post
    forever after the decision about it was made — a bookmark from last
    quarter, a second moderator who already dismissed the pile, a replayed
    form. Dismiss can afford to be idempotent because resolving an already
    resolved row changes nothing; this cannot, so it refuses instead.

    **The broadcast only fires for a local author, and that is not a
    convenience.** ``broadcast_status_delete`` signs as ``status.user`` with
    that user's ``private_key``, and a mirrored remote account's key is
    empty by construction (we fetch their *public* key and never hold their
    private one). Signing would raise inside the delivery loop — a
    ``ValueError`` from the key loader, which the loop's
    ``except requests.RequestException`` does not catch — so an
    unconditional broadcast would turn a moderator's click into a 500 with
    the post already deleted and the report already closed. The principle is
    the sturdier reason: we cannot sign a ``Delete`` as an identity we do
    not control, and peers would rightly refuse it. Removing a remote
    account's content *here* is exactly the effect §2D allows against a
    remote target; their home instance keeps its own copy, because we do not
    own it and cannot un-send it.
    """
    report = get_object_or_404(
        Report.objects.select_related("target_status", "target_user"), id=report_id
    )
    if report.resolved_at is not None:
        raise Http404("This report has already been resolved.")
    status = report.target_status
    if status is None:
        raise Http404("This report is about a member, not a post.")
    if status.deleted:
        raise Http404("This post has already been deleted.")
    if not can_act_on(request.user, status.user):
        raise PermissionDenied

    note = request.POST.get("note", "").strip()
    # The local write commits inside the helper, before any network call, so
    # a dead follower can never lose the deletion or the audit record.
    _, count = delete_reported_status(report, by_user=request.user, note=note)
    failures = broadcast_status_delete(request, status) if status.user.local else []
    if failures:
        hosts = ", ".join(
            sorted({urlparse(f.inbox).netloc or f.inbox for f in failures})
        )
        record_federation_outcome(
            report,
            f"[federation] {len(failures)} remote recipient(s) not notified: {hosts}",
        )
        messages.warning(
            request,
            f"Post deleted here, but {len(failures)} remote "
            f"{'server was' if len(failures) == 1 else 'servers were'} "
            f"not notified ({hosts}). Their copies may still be up.",
        )
    elif count > 1:
        messages.success(request, f"Post deleted; {count} reports about it resolved.")
    else:
        messages.success(request, "Post deleted.")
    return redirect("moderation")


@moderator_required
@require_POST
def suspend(request, report_id):
    """Suspend the reported member from the queue (increment 4, R102/R103b).

    The heaviest action the queue offers, and it carries the same two gates
    as the delete rather than a third. ``moderator_required`` says the
    visitor may be here at all; ``can_act_on`` (R103 as amended by R103b)
    says this account is theirs to act on — a moderator may suspend only a
    **regular user**, never the site admin, never a peer moderator, never
    an account holding the ``/admin/`` door, never themselves. The owner
    settled the reach once for every destructive action, so this guard is
    reused unchanged rather than widened for the heavier verb.

    **A resolved report is not a live suspend handle**, exactly as with the
    delete. Dismiss can afford idempotence because re-resolving changes
    nothing; a suspend cannot, so the route refuses instead of leaving a
    bookmark from last quarter able to re-fire a decision that was already
    made.

    **Remote targets are refused, and not only by the hidden control.**
    R102 defines suspend as an action on an account we own. We cannot
    suspend a remote account at its home instance, and we could not sign
    the actor update even if we tried — a mirror holds no private key. The
    effects available against a remote target are *remove their content
    here* and *refuse them here*, which is increment 6's domain block.

    **The broadcast is loud and non-blocking (R108).** The suspend itself
    is committed before any network call, so a dead peer cannot undo the
    decision; but a suspend that did not federate is written into the
    report's audit line and shown to the moderator, because "this account
    is suspended here and nobody else knows" is exactly the fact that must
    not be quiet.
    """
    report = get_object_or_404(
        Report.objects.select_related("target_user", "target_status"), id=report_id
    )
    if report.resolved_at is not None:
        raise Http404("This report has already been resolved.")
    target = report.target_user
    if not can_impose_severity(request.user, target):
        raise PermissionDenied
    if not target.local:
        raise Http404(
            "Suspend applies to accounts on this instance; remote accounts "
            "are handled by the domain block."
        )

    note = request.POST.get("note", "").strip()
    # The local write commits inside the helper, before any network call,
    # so a dead follower can never lose the suspension or the audit record.
    _, suspended, count = suspend_reported_member(
        report, by_user=request.user, note=note
    )
    if not suspended:
        messages.info(request, f"@{target.localname} was already suspended.")
        return redirect("moderation")

    failures = broadcast_actor_update(request, target)
    if failures:
        hosts = ", ".join(
            sorted({urlparse(f.inbox).netloc or f.inbox for f in failures})
        )
        record_federation_outcome(
            report,
            f"[federation] {len(failures)} remote recipient(s) not told about "
            f"the suspension: {hosts}",
        )
        messages.warning(
            request,
            f"@{target.localname} is suspended here, but {len(failures)} "
            f"remote {'server was' if len(failures) == 1 else 'servers were'} "
            f"not told ({hosts}).",
        )
    elif count > 1:
        messages.success(
            request,
            f"@{target.localname} suspended; {count} reports about them resolved.",
        )
    else:
        messages.success(request, f"@{target.localname} suspended.")
    return redirect("moderation")


@moderator_required
@require_POST
def unsuspend(request, localname):
    """Lift a suspension from the suspended account's profile (R102).

    Not a queue route, and that placement is forced rather than chosen. A
    suspend resolves the pile that raised it, so the card is gone by the
    time anyone wants to undo it — and a suspension with no surface that
    still shows the account is a suspension nobody can lift. The profile is
    the one place a suspended account remains visible (R102 requires
    exactly that), so the lift lives there.

    Same guard as the suspend, unchanged: ``moderator_required`` for the
    surface, ``can_act_on`` for the target. R103b's reach does not get
    looser just because this direction is the friendly one — a moderator
    still cannot lift a suspension on an account they could never have
    imposed, which keeps the two verbs from becoming an asymmetric way to
    act on a peer.

    **No note field, and deliberately so.** R106 wants a note on every
    action, but the record this arc keeps is the ``Report`` row, and by now
    that row is closed — appending to a resolved decision would rewrite it
    rather than add to it. Rather than claim to record something this
    surface cannot, the control asks for no note. The gap is stated in the
    increment's execution record instead of being papered over with a
    second audit table the plan declined.
    """
    target = _resolve_profile_user(localname)
    if target is None:
        raise Http404("No such user")
    if not can_act_on(request.user, target):
        raise PermissionDenied
    if target.suspended_at is None:
        messages.info(request, f"@{target.localname} is not suspended.")
        return redirect("user-profile", localname=target.localname)

    target.unsuspend()
    failures = broadcast_actor_update(request, target)
    if failures:
        hosts = ", ".join(
            sorted({urlparse(f.inbox).netloc or f.inbox for f in failures})
        )
        messages.warning(
            request,
            f"@{target.localname} is unsuspended, but {len(failures)} "
            f"remote {'server was' if len(failures) == 1 else 'servers were'} "
            f"not told ({hosts}).",
        )
    else:
        messages.success(request, f"@{target.localname} is no longer suspended.")
    return redirect("user-profile", localname=target.localname)


@moderator_required
@require_POST
def ban(request, report_id):
    """Ban the reported member from the queue (increment 5, R102/R103b).

    The heaviest thing this instance does to a person, and it reaches it
    through the *same two gates* as the delete and the suspend rather than a
    third. ``moderator_required`` for the surface; ``can_act_on`` for the
    target. R103b's reach — a moderator acts only on a regular user who is
    not them — does not widen for the heavier verb, and does not narrow either:
    on the owner's 2026-09-28 call a moderator may both impose and lift a
    ban, so the guard that permits the click is the same one that permits the
    undo, and neither verb gets a private rule.

    **What the moderator is told before clicking, and why it is not scare
    text.** A ban removes content, and the removal is real: ``Status.delete()``
    is R17 soft-delete, which clears ``content`` and ``raw_content`` on the
    way to becoming a tombstone. Lifting the ban restores the account, the
    localname and the actor document — **it does not restore the writing.**
    That is not this increment being pessimistic; it is what the instance's
    delete semantics are, and the author's own delete behaves identically.
    The alternative would be a private content backup attached to the ban,
    which is a different product. So the disclosure says it plainly, in the
    one place it can be read before the click rather than after.

    **A resolved report is not a live ban handle**, exactly as with the delete
    and the suspend. A bookmark from last quarter must not be able to re-fire
    a decision that was already made about it.

    **Remote targets are refused.** R102 defines ban as an action on an
    account we own. We cannot ban a remote account at its home instance, and
    we could not sign a ``Delete(Person)`` for one even if we tried — a
    mirror holds no private key, and a peer would rightly refuse a statement
    about their user signed by somebody else. The effects available against a
    remote target are *remove their content here* and *refuse them here*,
    which is increment 6's domain block.

    **The broadcast is loud and non-blocking (R108).** The ban commits before
    any network call, so a dead peer cannot undo the decision; but a ban that
    did not federate is written into the report's audit line and shown to the
    moderator, because "this account is gone here and their followers still
    have every word of it" is the fact that must not be quiet.
    """
    report = get_object_or_404(
        Report.objects.select_related("target_user", "target_status"), id=report_id
    )
    if report.resolved_at is not None:
        raise Http404("This report has already been resolved.")
    target = report.target_user
    if not can_impose_severity(request.user, target):
        raise PermissionDenied
    if not target.local:
        raise Http404(
            "Ban applies to accounts on this instance; remote accounts "
            "are handled by the domain block."
        )

    note = request.POST.get("note", "").strip()
    # The local write — the report pile, the content removal, the account
    # state — commits inside the helper, before any network call runs, so a
    # dead follower can never lose the ban or the audit record.
    _, banned, count, removed = ban_reported_member(
        report, by_user=request.user, note=note
    )
    if not banned:
        messages.info(request, f"@{target.localname} was already banned.")
        return redirect("moderation")

    failures = broadcast_person_delete(request, target)
    if failures:
        hosts = ", ".join(
            sorted({urlparse(f.inbox).netloc or f.inbox for f in failures})
        )
        record_federation_outcome(
            report,
            f"[federation] {len(failures)} remote recipient(s) not told about "
            f"the ban: {hosts}",
        )
        messages.warning(
            request,
            f"@{target.localname} is banned here, but {len(failures)} "
            f"remote {'server was' if len(failures) == 1 else 'servers were'} "
            f"not told ({hosts}). Their content may still be live there.",
        )
    elif count > 1:
        messages.success(
            request,
            f"@{target.localname} banned; {count} reports about them resolved "
            f"and {len(removed)} post{'' if len(removed) == 1 else 's'} removed.",
        )
    else:
        messages.success(
            request,
            f"@{target.localname} banned; "
            f"{len(removed)} post{'' if len(removed) == 1 else 's'} removed.",
        )
    return redirect("moderation")


@moderator_required
@require_POST
def forward(request, report_id):
    """Tell a remote account's home instance that we have been told about them (R104).

    The only card action that points **outward** rather than inward, and the
    only one that decides nothing. The report stays open: forwarding is
    passing the complaint to the one server that can act on that account,
    not a judgment of our own, and the moderator may still delete the
    local copy or dismiss the pile afterwards.

    **The reporter is masked to the instance representative, and the
    masking reaches the key.** ``broadcast_report_flag`` signs as
    ``@_instance`` with ``@_instance``'s keypair, so neither the reporter's
    identity nor the moderator's appears in the document *or* in the
    ``keyid`` a peer verifies before it reads anything else. Masking only
    the ``actor`` field while signing as the reporter would be worse than
    no masking at all — it would look compliant and leak anyway.

    **Four structural refusals, all ``404``, all the same posture** as the
    delete, suspend and ban routes: a resolved report is not a live handle,
    a local account has no home instance to tell, a mirror with no
    ``actor_url`` has no address to forward to, and a suspended target
    would be skipped by the delivery layer without recording a failure.
    The last one is the important pair to the hidden control: without the
    check, a hand-built POST against a suspended mirror would come back
    "forwarded" having sent nothing, which is R88's blindness wearing a
    success message.

    **A 2xx here is delivery, not agreement.** Increment 5 established
    that a peer can accept our activity and then discard it — their
    ``ProcessAccountService`` shield refuses to undo a suspension their own
    moderators imposed, after answering ``202``. A ``Flag`` is the same
    shape: we are asking another server to look at something. The audit
    line says *delivered*, never *acted on*, and the moderator's message
    says the same.
    """
    report = get_object_or_404(
        Report.objects.select_related("target_user", "target_status"), id=report_id
    )
    if report.resolved_at is not None:
        raise Http404("This report has already been resolved.")
    target = report.target_user
    if target.local:
        raise Http404(
            "A report about a local account has no home server to forward to."
        )
    if not target.actor_url:
        raise Http404("This account has no home server address to forward to.")
    if target.suspended_at is not None:
        raise Http404(
            "This account is suspended here, so a forward would not be delivered."
        )
    if not can_act_on(request.user, target):
        raise PermissionDenied

    representative = instance_representative()
    failures = broadcast_report_flag(request, report, representative=representative)
    if failures:
        hosts = ", ".join(
            sorted({urlparse(f.inbox).netloc or f.inbox for f in failures})
        )
        reasons = ", ".join(sorted({f.reason for f in failures}))
        record_forward_outcome(
            report,
            f"[federation] forward not delivered to {hosts} ({reasons})",
        )
        messages.error(
            request,
            f"The report was not delivered to {hosts}. "
            f"It is still open here and can be retried.",
        )
    else:
        record_forward_outcome(
            report,
            f"[federation] delivered to {target.actor_url} as "
            f"@{representative.localname} (their server accepted it; whether it "
            f"acted is not knowable from here)",
        )
        messages.success(
            request,
            f"Forwarded to the server that holds @{target.localname}. "
            "They decide what to do about it — this does not resolve the report here.",
        )
    return redirect("moderation")


@moderator_required
@require_POST
def refuse_remote(request, report_id):
    """Refuse a remote account on this instance (R105, the generalised block).

    The per-account answer to the question §2D left open: a remote user
    cannot be suspended or banned at their home instance, so what is a
    moderator to do when one report is not enough to send them away? The
    answer the owner chose is that refusing them **is** a suspension — the
    same state the domain block applies to a whole host's mirrors, aimed
    here at one mirror of one account.

    That is not a shortcut and it is not a redefinition. Suspension is the
    only state on this instance that already means "this account is not
    welcome here and its content must not appear", and it is enforced at
    every one of the eight-odd dozen read sites increment 4 mapped. A
    second, refusal-shaped state would have needed its own clause at each of
    those sites, and the failure mode of that design is not a crash — it is
    the blocked account still showing up on the one page nobody thought to
    filter.

    **Nothing is broadcast, and that is not an omission.** A suspension of
    a mirror is a statement about what *we* will accept, not about the
    account. Broadcasting an ``Update(Person)`` for it would mean signing
    someone else's actor document with a key we do not hold — a mirror's
    ``private_key`` is empty by construction, so the attempt would raise
    in the delivery layer — and even if we could, telling the network that
    another instance's user is suspended is a claim about their account we
    are not entitled to make. The refusal is local, and it stays local.

    **The lift lives on the mirror's profile**, same rule as every other
    lift in this arc: the control sits on the surface that still shows the
    account. A refused mirror keeps the suspended-state page increment 4b
    built, and its unsuspend control lifts this the same way.
    """
    report = get_object_or_404(
        Report.objects.select_related("target_user", "target_status"), id=report_id
    )
    if report.resolved_at is not None:
        raise Http404("This report has already been resolved.")
    target = report.target_user
    if target.local:
        raise Http404(
            "This is an account on this instance — suspend or ban it, do not refuse it."
        )
    if not can_impose_severity(request.user, target):
        raise PermissionDenied

    note = request.POST.get("note", "").strip()
    refused = target.suspend(reason=note)
    if not refused:
        messages.info(request, f"@{target.localname} was already refused here.")
        return redirect("moderation")
    messages.success(
        request,
        f"@{target.localname} is refused here. Their posts are hidden on this "
        "instance and nothing they send will be accepted. Their home server is "
        "unchanged — it does not hear about this and cannot.",
    )
    return redirect("moderation")


@moderator_required
@require_POST
def block_domain_view(request):
    """Block a whole remote server (R105).

    Two effects, and the second is why this is cheap. The door closes: no
    activity from that host resolves as a sender, and no new mirror from it
    gets created. And every mirror we already hold from it is suspended,
    which means the hiding is done by the suspension filters that already
    exist rather than by a new host-keyed clause at each read site.

    The moderator is told how many accounts the block swept, because a
    block that silently hid forty profiles and a block that found none look
    identical otherwise — and those are very different things to have just
    done.
    """
    raw = request.POST.get("domain", "")
    reason = request.POST.get("note", "").strip()
    normalized = normalize_domain(raw)
    if not normalized:
        messages.error(request, "Enter the server to block, for example bad.example.")
        return redirect("moderation")
    if DomainBlock.objects.filter(domain=normalized).exists():
        messages.error(request, f"{normalized} is already blocked.")
        return redirect("moderation")

    block, suspended = apply_domain_block(
        normalized, by_user=request.user, reason=reason
    )
    messages.success(
        request,
        f"{block.domain} is blocked. Nothing from that server will be accepted "
        f"here, and {len(suspended)} account{'' if len(suspended) == 1 else 's'} "
        f"we already held from it {'is' if len(suspended) == 1 else 'are'} now hidden.",
    )
    return redirect("moderation")


@moderator_required
@require_POST
def unblock_domain_view(request, block_id):
    """Remove a domain block and lift the suspensions that block imposed.

    Only that block's own suspensions. Accounts somebody suspended on
    their own merits stay suspended, and accounts sitting under a *different*
    block stay suspended too — the lift is scoped to what this action is
    responsible for, which is the same principle that keeps an unban from
    clearing a separate suspension.
    """
    block = get_object_or_404(DomainBlock, id=block_id)
    domain = block.domain
    _block, lifted = lift_domain_block(block, by_user=request.user)
    if lifted:
        messages.success(
            request,
            f"{domain} is no longer blocked, and the {len(lifted)} account(s) "
            "this block hid are restored. Accounts suspended for any other "
            "reason stay suspended.",
        )
    else:
        messages.success(
            request,
            f"{domain} is no longer blocked. No accounts were hidden by this "
            "block, so nothing was restored.",
        )
    return redirect("moderation")


@moderator_required
@require_POST
def unban(request, localname):
    """Lift a ban from the moderation queue (R102 as amended 2026-09-28).

    Not on the profile, and the reason is the ban's own design. 4b put the
    unsuspend control on the profile because R102 requires a suspended
    account to keep a visible page that explains itself — the lift lives on
    the surface that still shows the account. A banned account deliberately
    has **no** public page: the profile is ``410 Gone``, which is the point
    of the ban and not an oversight to route around. So the surface that
    still shows a banned account is the moderator's, and the control lives
    on ``/moderate/``.

    Same guard as the ban, unchanged, which is the whole shape of the
    owner's call: whoever may impose it may lift it, and nobody else may do
    either. A moderator cannot lift a ban on an account they could never
    have banned.

    **No note field, and it is the same gap unsuspend left.** R106 wants a
    note on every action, but the record this arc keeps is the ``Report``
    row, and by now that row is closed and drained. Appending to it would
    rewrite a closed decision rather than add a new one. The honest fix is a
    separate audit surface, which R106 declined.

    **Nothing is broadcast, and there is nothing to broadcast.** A
    ``Delete(Person)`` has no inverse. Re-announcing a previously-deleted
    actor with an ``Update`` is not something any peer is obliged to handle
    sanely, and a wrong guess here tells the network something about an
    identity we already told it was dead. What restores federation is
    ordinary discovery: the actor URL answers ``200`` again, so a peer that
    is asked for it — by a search, or by someone re-following — gets the
    live document and rebuilds its mirror. The follow graph on their side is
    not restored, because that was destroyed by the delete and we cannot
    reach across to re-create it.
    """
    target = _resolve_profile_user(localname)
    if target is None:
        raise Http404("No such user")
    if not can_act_on(request.user, target):
        raise PermissionDenied
    if target.banned_at is None:
        messages.info(request, f"@{target.localname} is not banned.")
        return redirect("moderation")

    target.unban()
    messages.success(
        request,
        f"@{target.localname}'s ban is lifted — they can sign in again. "
        "Their removed posts do not come back, and remote servers that "
        "processed the delete will only learn they exist again by being "
        "shown: the account is re-followable, not re-followed.",
    )
    return redirect("moderation")


@login_required
@require_POST
def report_status(request, status_id):
    """Report someone else's post (members-only control, R98).

    A member reporting their **own** post is refused, not silently
    swallowed. The refusal is a message plus a redirect rather than a 403,
    matching ``_change_follow``'s self-case on the neighbouring member
    routes — the write is refused either way, and the closer precedent wins
    over inventing a second posture for the same kind of "that one does not
    apply to you". What the test pins is that no row was created.
    """
    status = get_object_or_404(Status, id=status_id, deleted=False)
    if status.user_id == request.user.pk:
        messages.error(request, "You can't report your own post.")
        return redirect("status", status_id=status.id)
    form = ReportForm(request.POST)
    if not form.is_valid():
        _report_form_errors(request, form)
        return redirect("status", status_id=status.id)
    _, created = file_report(
        reporter=request.user,
        target_status=status,
        category=form.cleaned_data["category"],
        comment=form.cleaned_data["comment"],
    )
    if created:
        messages.success(request, "Report filed. A moderator will review it.")
    else:
        messages.info(request, "You have already reported this post.")
    return redirect("status", status_id=status.id)


@login_required
@require_POST
def report_user(request, localname):
    """Report a member from their profile (members-only control, R98).

    Resolved through the same helper the profile route uses, so a mirror
    handle (``<user>@<host>``) reports the mirror rather than a local
    account of the same name. That distinction is the whole reason this
    route does not do its own lookup: two resolution rules on one handle
    shape is how a report ends up against the wrong person.
    """
    target = _resolve_profile_user(localname)
    if target is None:
        raise Http404("No such user")
    if target.pk == request.user.pk:
        messages.error(request, "You can't report yourself.")
        return redirect("user-profile", localname=target.localname)
    form = ReportForm(request.POST)
    if not form.is_valid():
        _report_form_errors(request, form)
        return redirect("user-profile", localname=target.localname)
    _, created = file_report(
        reporter=request.user,
        target_user=target,
        category=form.cleaned_data["category"],
        comment=form.cleaned_data["comment"],
    )
    if created:
        messages.success(request, f"Report filed about @{target.localname}.")
    else:
        messages.info(request, f"You have already reported @{target.localname}.")
    return redirect("user-profile", localname=target.localname)


def _report_form_errors(request, form):
    """Surface a rejected report as messages rather than a re-rendered form.

    The form lives inside pages owned by other apps — the post page and the
    profile — so re-rendering one with an invalid form would mean
    reconstructing that app's whole template context from here. The
    failure modes are narrow (a missing category, a comment over the cap)
    and the host page is the right place to land either way, so this
    follows the shape the rest of the codebase uses for a refused form
    post: say what was wrong, send them back.
    """
    for field, errors in form.errors.items():
        for error in errors:
            messages.error(request, f"Report not filed: {error}")
