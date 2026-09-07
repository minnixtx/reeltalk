"""Template tags for film display."""

from decimal import Decimal, InvalidOperation

from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def stars(rating) -> str:
    """Render read-only star markup for a 0.5–5 rating.

    Two overlapping rows of five stars: a gray background row and a gold
    foreground row clipped to the rating's width (see ``.stars`` in
    reeltalk.css). Half-star ratings land on a whole percentage because every
    half step is 10% of the five-star span, so no sub-pixel rounding is needed.
    """
    if rating in (None, ""):
        return ""
    try:
        value = Decimal(str(rating))
    except InvalidOperation:
        return ""
    percent = min(max(value / Decimal("5") * 100, 0), 100)
    return mark_safe(
        '<span class="stars">'
        '<span class="stars-bg">★★★★★</span>'
        f'<span class="stars-fg" style="width:{int(percent)}%">★★★★★</span>'
        "</span>"
    )
