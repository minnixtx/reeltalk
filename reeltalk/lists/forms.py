"""The list authoring form (§2K increment 3).

Deliberately a plain ``forms.Form`` and **not** a ``ModelForm``. A ModelForm
comes with a ``save()`` that writes ``FilmList`` directly, and this feature has
exactly one write door — ``reeltalk/lists/services.py``, which owns the post
face, the rank rules and the markdown/HTML pair. A form that could save would
be a second write path with none of those invariants, which is the thing the
service layer exists to prevent. So this form validates and stops there; the
view hands the cleaned values to ``create_list`` / ``rename`` /
``set_description``.

The description field carries **markdown in and nothing else**. Rendering is
the service's job (``create_list`` and ``set_description`` both render and
store both halves). Compare ``FilmForm.clean_description``, which renders into
the cleaned value because ``Film``'s save path expects that: copied here, the
raw markdown would be handed to a renderer that renders it a second time.

The ``visibility`` field is **opt-in per construction**, because the create and
edit pages are not the same question (W-arc D2). The create page offers the
three-way audience choice and passes ``with_visibility=True``; the edit page
constructs the form plain and therefore has no field to render, to validate, or
to write - the audience is fixed at creation, and the immutability is the
absence of a writer rather than a control that would be ignored. A hand-built
POST to the edit route naming ``visibility`` is not a field the form has, so it
is cleaned away with the rest of the noise the view never reads.

The field validates; the create template draws the radios by hand, the way the
finish modal's composer draws them (``core/film/detail.html``), so the two
audience controls on the site are one treatment rather than two that drift.
"""

from django import forms

from reeltalk.core.models import Visibility


class ListForm(forms.Form):
    """The two fields a list is made of, plus the audience the create page asks for.
    Films are not part of this form —
    they are added to an existing list through the editor's search (§2K L5,
    one door), so a brand-new list is created empty and filled on the next
    page."""

    def __init__(self, *args, with_visibility: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        if with_visibility:
            self.fields["visibility"] = forms.ChoiceField(
                choices=Visibility.choices,
                required=False,
                label="Who can see this",
                widget=forms.RadioSelect,
            )

    title = forms.CharField(
        max_length=200,
        label="List name",
        widget=forms.TextInput(
            attrs={
                "autofocus": "autofocus",
                "placeholder": "Best Sci-Fi Films of the 50s",
            }
        ),
    )
    description = forms.CharField(
        required=False,
        label="Description",
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text="Markdown is supported.",
    )
