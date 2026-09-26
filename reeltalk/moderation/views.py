"""The moderation surface (increment 1).

Only the empty-state placeholder exists today. The report queue is increment
2, so this view deliberately queries nothing — there is no ``Report`` model
yet, and R99 forbids routing reports through the notification ledger. What
this increment is for is proving the gate around it, which lives in
``decorators.py`` and not here.
"""

from django.shortcuts import render

from reeltalk.moderation.decorators import moderator_required


@moderator_required
def index(request):
    """``/moderate/`` — the moderator's home page (empty until increment 2)."""
    return render(request, "moderation/index.html", {})
