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
"""

from django import forms


class ListForm(forms.Form):
    """The two fields a list is made of. Films are not part of this form —
    they are added to an existing list through the editor's search (§2K L5,
    one door), so a brand-new list is created empty and filled on the next
    page."""

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
