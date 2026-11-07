from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from ...models import get_tenant_model, normalize_slug


class NewTenantForm(forms.Form):
    """The workspace alone, for someone already signed in."""

    tenant_name = forms.CharField(label="Workspace name", max_length=200)
    slug = forms.CharField(label="Workspace address", max_length=200)

    def clean_tenant_name(self):
        # One line, as it goes into an email subject.
        name = " ".join(self.cleaned_data["tenant_name"].split())
        return get_tenant_model()._meta.get_field("name").clean(name, None)

    def clean_slug(self):
        model = get_tenant_model()
        slug = normalize_slug(self.cleaned_data["slug"])
        slug = model._meta.get_field("slug").clean(slug, None)
        if model.objects.filter(slug=slug).exists():
            raise ValidationError("That address is taken.")
        return slug


class SignupForm(NewTenantForm):
    email = forms.EmailField(label="Your email")
    password = forms.CharField(label="Password", strip=False, widget=forms.PasswordInput)

    def clean_email(self):
        email = self.cleaned_data["email"].casefold()
        model = get_user_model()
        limit = model._meta.get_field(model.USERNAME_FIELD).max_length
        if limit is not None and len(email) > limit:
            raise ValidationError(f"Use an email of at most {limit} characters.")
        return email

    def clean(self):
        cleaned = super().clean()
        password, email = cleaned.get("password"), cleaned.get("email")
        if password:
            model = get_user_model()
            # A prospective user, so rules that compare the password with the account can run.
            fields = {model.USERNAME_FIELD: email or "", "email": email or ""}
            try:
                validate_password(password, model(**fields))
            except ValidationError as error:
                self.add_error("password", error)
        return cleaned

    def password_hash(self) -> str:
        return make_password(self.cleaned_data["password"])
