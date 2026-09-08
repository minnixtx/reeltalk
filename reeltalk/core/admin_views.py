"""Admin-only views outside the ModelAdmin framework (PLAN.md §3.7 admin).

The film merge/absorb tool: pick a canonical film and one or more duplicate
rows, run ``Film.merge_into`` over them in one transaction. The model method
owns all the semantics (backfill, re-pointing, MergedFilm row, deletion) —
this view only validates the selection and reports the outcome.
"""

from django import forms
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.shortcuts import redirect, render

from .models import Film


class MergeFilmsForm(forms.Form):
    canonical = forms.ModelChoiceField(
        label="Canonical film",
        queryset=Film.objects.order_by("sort_title", "year"),
        help_text=(
            "The row that survives. Empty metadata is filled from the absorbed rows."
        ),
    )
    absorbed = forms.ModelMultipleChoiceField(
        label="Films to merge in",
        queryset=Film.objects.order_by("sort_title", "year"),
        help_text=(
            "Every shelf and review on these rows moves to the canonical "
            "film, their old URLs keep resolving, and the rows are deleted. "
            "This cannot be undone."
        ),
    )

    def clean(self):
        cleaned = super().clean()
        canonical = cleaned.get("canonical")
        absorbed = cleaned.get("absorbed") or []
        if canonical and any(film.id == canonical.id for film in absorbed):
            self.add_error("absorbed", "The canonical film cannot also be merged in.")
        return cleaned


@staff_member_required
def merge_films(request):
    """Merge duplicate film rows into one (§3.7 v0.1 admin tool)."""
    if request.method == "POST":
        form = MergeFilmsForm(request.POST)
        if form.is_valid():
            canonical = form.cleaned_data["canonical"]
            absorbed = list(form.cleaned_data["absorbed"])
            try:
                # The outer transaction makes the whole batch all-or-nothing:
                # each merge_into is atomic on its own, but a failure partway
                # through must not leave earlier merges committed.
                with transaction.atomic():
                    for film in absorbed:
                        film.merge_into(canonical)
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(
                    request, f"Merged {len(absorbed)} film(s) into {canonical}."
                )
                return redirect("admin-merge-films")
    else:
        form = MergeFilmsForm()
    return render(request, "core/admin/merge_films.html", {"form": form})
