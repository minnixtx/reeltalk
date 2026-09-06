"""Account forms: signup and the first-run setup wizard share one form."""

import re

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from .models import User

# Letters/digits plus '.', '_', '-'; must start with a letter or digit.
LOCALNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


class SignupForm(forms.Form):
    localname = forms.CharField(
        max_length=30,
        widget=forms.TextInput(attrs={"autofocus": "autofocus"}),
    )
    display_name = forms.CharField(max_length=255, required=False)
    email = forms.EmailField(required=False)
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput)
    password2 = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput,
    )

    def clean_localname(self):
        localname = self.cleaned_data["localname"].strip()
        if not LOCALNAME_RE.match(localname):
            raise ValidationError(
                "Use 1-30 characters: letters, numbers, '.', '_' or '-', "
                "starting with a letter or number."
            )
        # Case-insensitive uniqueness: "Alice" and "alice" would be the same
        # federated identity to other instances.
        if User.objects.filter(localname__iexact=localname).exists():
            raise ValidationError("That name is already taken.")
        return localname

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            self.add_error("password2", "The two password fields didn't match.")
        if password1:
            try:
                validate_password(password1)
            except ValidationError as exc:
                self.add_error("password1", exc)
        return cleaned_data
