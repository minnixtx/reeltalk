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
"""

from urllib.parse import urlparse

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render

import reeltalk
from reeltalk.activitypub.identity import accepts_activitypub, person_document
from reeltalk.activitypub.mirrors import refresh_mirror_profile
from reeltalk.core.models import Shelf, feed_entries
from reeltalk.core.utils import render_markdown

from .forms import ProfileForm, SignupForm
from .models import SiteSettings, User


def has_admin() -> bool:
    return User.objects.filter(is_superuser=True).exists()


def index(request):
    if not has_admin():
        return redirect("setup")
    data = {"site": SiteSettings.get_instance()}
    if request.user.is_authenticated:
        # The v0.1 timeline (§3.6/§3.7): shelf events + statuses (R33/R35);
        # anonymous visitors get the landing page only.
        data["feed"] = feed_entries(request.user)
    return render(request, "home.html", data)


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


def signup(request):
    if not has_admin():
        return redirect("setup")
    if request.user.is_authenticated:
        return redirect("index")
    site = SiteSettings.get_instance()
    if site.signup_policy != SiteSettings.OPEN:
        # Invite-only in v0.1 means closed: an admin creates the account.
        return render(request, "signup.html", {"closed": True})
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        user = User.objects.create_user(
            localname=data["localname"],
            email=data.get("email", ""),
            password=data["password1"],
            display_name=data.get("display_name", ""),
        )
        login(request, user)
        messages.success(request, f"Welcome, {user.localname}!")
        return redirect("index")
    return render(request, "signup.html", {"form": form})


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
        login(request, user)
        messages.success(request, "Instance set up — you are now the admin.")
        return redirect("index")
    return render(request, "social/setup.html", {"form": form})


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
    if accepts_activitypub(request):
        if not user.local:
            return HttpResponse(status=404)
        return JsonResponse(
            person_document(user, request),
            content_type="application/activity+json",
        )
    if not user.local:
        refresh_mirror_profile(user)
        user.refresh_from_db()
    data = {
        "profile_user": user,
        "is_self": request.user.is_authenticated and request.user.pk == user.pk,
    }
    if not user.local:
        # The home instance the mirror came from (the actor URL's netloc).
        data["home_instance"] = urlparse(user.actor_url).netloc
    return render(request, "social/profile.html", data)


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
