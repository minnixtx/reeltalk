"""The moderation surface: the queue and the report controls (increments 1–2).

Two different audiences share this module and they are gated differently.
``index`` and ``dismiss`` are **moderator-only** (R101: anonymous → 302,
signed-in non-moderator → 403). ``report_status`` and ``report_user`` are
**member-only** — a report is something any member files, and gating it on
moderation would mean only moderators could report anything.

Nothing here federates. R104's ``Flag`` forwarding is increment 6, so a
report about a remote user is recorded and read here but is not yet sent to
their home instance.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from reeltalk.core.models import Status
from reeltalk.moderation.decorators import moderator_required
from reeltalk.moderation.forms import ReportForm
from reeltalk.moderation.models import Report, dismiss_report, file_report
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
    paginator = Paginator(queue_cards(), QUEUE_PAGE_SIZE)
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
        },
    )


def queue_cards():
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
    """
    cards = {}
    for report in Report.unresolved().select_related(
        "reporter", "target_user", "target_status", "target_status__film"
    ):
        # ``Report.unresolved()`` carries Meta.ordering = ["-created"], so
        # first-seen order over it is latest-report-first per target.
        card = cards.get(report.target_key)
        if card is None:
            card = {
                "target_user": report.target_user,
                "target_status": report.target_status,
                "reports": [],
                "latest": report,
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


@login_required
@require_POST
def report_status(request, status_id):
    """Report someone else's post (members-only control, R98).

    The lookup excludes tombstones: a deleted status has nothing left to
    act on, and reporting one would file an accusation against content that
    is already gone.

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
