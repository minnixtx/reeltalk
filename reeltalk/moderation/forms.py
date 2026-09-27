"""The report form (moderation arc increment 2).

A plain ``forms.Form`` rather than a ``ModelForm`` on purpose. A
``ModelForm`` would want ``reporter``, ``target_user`` and ``target_status``
as fields, and every one of them is something the **URL** decides, not the
person filing the report. Letting the submitted form name the target would
mean a tampered POST could file a report against someone the reporter never
looked at. So the form carries only the two things the reporter actually
supplies — why, and their note — and the view supplies the trio the
dedup key is built from.
"""

from django import forms

from reeltalk.moderation.models import Report

# Mastodon allows 1,000 chars on a local report comment. Half that is the
# cap here: this is a note about one post on a small instance, and the
# queue renders the comment inline, so an unbounded field would be a
# moderator's reading problem as much as a storage one.
COMMENT_MAX_LENGTH = 500


class ReportForm(forms.Form):
    """The reporter's two inputs: a category and an optional note."""

    category = forms.ChoiceField(
        choices=Report.Category.choices,
        widget=forms.RadioSelect,
        help_text="What is wrong with this?",
    )
    comment = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3, "maxlength": COMMENT_MAX_LENGTH}),
        required=False,
        label="Anything else we should know?",
        # max_length on the field, not only the maxlength attribute on the
        # widget. The attribute is a browser courtesy and stops a real
        # keyboard going past the cap; it does nothing to a POST built by
        # anything else, and the field's own validator is the only thing
        # the server enforces.
        max_length=COMMENT_MAX_LENGTH,
    )

    def clean_category(self):
        category = self.cleaned_data["category"]
        # ChoiceField already rejects a value outside the enum, so this
        # reads as redundant — but only because the choices are bound here
        # rather than taken from the model's blank-allowed column. Stated
        # explicitly because an empty category is the difference between a
        # report a moderator can triage and one they cannot.
        if category not in Report.Category.values:
            raise forms.ValidationError("Choose a reason.")
        return category

    def clean_comment(self):
        return self.cleaned_data["comment"].strip()
