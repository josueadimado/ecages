from django import forms
from django.contrib.auth import authenticate
from .models import User


class LoginForm(forms.Form):
    role = forms.ChoiceField(
        label="Sélectionner votre rôle d'utilisateur",
        choices=User.ROLE_CHOICES,
        widget=forms.Select(attrs={"class": "input"})
    )
    username = forms.CharField(
        label="Sélectionner votre nom d'utilisateur",
        widget=forms.Select(attrs={"class": "input"}),  # replaced by JS with real options
        required=True,
    )
    password = forms.CharField(
        label="Mot de passe",
        widget=forms.PasswordInput(attrs={"class": "input", "placeholder": "Saisissez votre Mot de Passe"}),
        required=True,
    )

    # Will hold the authenticated user for the view
    user_obj = None

    def clean(self):
        cleaned = super().clean()
        role = cleaned.get("role")
        username = cleaned.get("username")
        password = cleaned.get("password")

        if not (role and username and password):
            return cleaned

        # verify user exists
        try:
            user = User.objects.get(username=username, is_active=True)
        except User.DoesNotExist:
            raise forms.ValidationError("Utilisateur introuvable ou inactif.")

        # verify role matches selection
        if user.role != role:
            raise forms.ValidationError("Le rôle sélectionné ne correspond pas à l'utilisateur.")

        # authenticate password
        authed = authenticate(username=username, password=password)
        if not authed:
            raise forms.ValidationError("Nom d'utilisateur ou mot de passe incorrect.")

        # everything OK
        self.user_obj = authed
        return cleaned


class SimpleUserCreateForm(forms.ModelForm):
    password1 = forms.CharField(label="Mot de passe", widget=forms.PasswordInput)
    password2 = forms.CharField(label="Confirmer le mot de passe", widget=forms.PasswordInput)

    class Meta:
        model = User
        fields = ("username", "first_name", "last_name", "email", "role", "salespoint", "is_active", "is_staff")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password1") != cleaned.get("password2"):
            self.add_error("password2", "Les mots de passe ne correspondent pas.")
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        if commit:
            user.save()
        return user

class SimpleUserAssignForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "role", "salespoint", "is_active", "is_staff")