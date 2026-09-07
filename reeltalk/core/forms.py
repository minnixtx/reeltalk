"""Film forms: manual create/edit (PLAN.md §3.7).

Only films without a ``tmdb_id`` reach these forms — D4 locks TMDB-sourced
metadata from user editing, enforced in the view before the form is built.
"""

from django import forms

from .models import Film
from .utils import render_markdown


class FilmForm(forms.ModelForm):
    # The model stores rendered HTML plus the markdown source; the form works
    # with the markdown only (one visible field, two model fields).
    description = forms.CharField(
        required=False,
        label="Description",
        widget=forms.Textarea(attrs={"rows": 6}),
        help_text="Markdown is supported.",
    )
    genres = forms.CharField(required=False, help_text="Comma-separated.")
    directors = forms.CharField(required=False, help_text="Comma-separated.")
    cast = forms.CharField(required=False, help_text="Comma-separated.")

    class Meta:
        model = Film
        fields = [
            "title",
            "subtitle",
            "description",
            "year",
            "runtime",
            "genres",
            "directors",
            "cast",
            "poster",
        ]
        widgets = {
            "title": forms.TextInput(attrs={"autofocus": "autofocus"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = self.instance
        if instance.pk:
            # Pre-fill the markdown source (not the stored HTML) and render the
            # array fields as comma-separated text.
            self.initial["description"] = instance.raw_description or ""
            for name in ("genres", "directors", "cast"):
                self.initial[name] = ", ".join(getattr(instance, name) or [])

    def _split_names(self, name: str) -> list[str]:
        raw = self.cleaned_data.get(name) or ""
        return [item.strip() for item in raw.split(",") if item.strip()]

    def clean_genres(self):
        return self._split_names("genres")

    def clean_directors(self):
        return self._split_names("directors")

    def clean_cast(self):
        return self._split_names("cast")

    def clean_description(self):
        raw = self.cleaned_data.get("description") or ""
        # Stash the markdown source; save() applies it to raw_description.
        self._raw_description = raw
        return render_markdown(raw)

    def save(self, commit=True):
        # raw_description is not a form field, so _construct_instance leaves it
        # alone — set it here so one write stores both HTML and markdown.
        self.instance.raw_description = getattr(self, "_raw_description", "")
        return super().save(commit=commit)
