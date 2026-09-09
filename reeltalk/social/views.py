"""Auth views: index, signup, and the first-run setup wizard (PLAN.md §3.7).

First-run flow (R12): a fresh instance with no superuser redirects both the
index and signup to /setup/, where the first account is created as the
instance admin. Once a superuser exists, signup follows the site settings'
policy (§3.7): open, or closed until an admin creates the account.
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.shortcuts import get_object_or_404, redirect, render

import reeltalk
from reeltalk.core.models import Shelf, feed_entries

from .forms import SignupForm
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


def user_films(request, localname):
    """A user's films page: All / Watchlist / Watched (§3.3 rule 1, D1).

    Exactly three tabs — D1's binary model leaves no room for more. The view
    only picks the tab's queryset; query/shelf logic lives on the User model.
    """
    profile = get_object_or_404(User, localname=localname)
    tab = request.GET.get("tab", "all")
    if tab not in ("watchlist", "watched"):
        tab = "all"
    if tab == "watchlist":
        films = profile.films_on_shelf(Shelf.TO_READ)
    elif tab == "watched":
        films = profile.films_on_shelf(Shelf.READ)
    else:
        films = profile.all_films()
    return render(
        request,
        "social/user_films.html",
        {"profile_user": profile, "tab": tab, "films": films},
    )
