from django import forms
from .models import Document
from django.contrib.auth.models import User
from django.contrib.auth.forms import UserCreationForm

class SignUpForm(UserCreationForm):
    email = forms.EmailField(required=True, widget=forms.EmailInput(attrs={
        'placeholder': 'Email', 'class': 'input-field'
    }))
    username = forms.CharField(widget=forms.TextInput(attrs={
        'placeholder': 'Username', 'class': 'input-field'
    }))
    password1 = forms.CharField(widget=forms.PasswordInput(attrs={
        'placeholder': 'Password', 'class': 'input-field'
    }))
    password2 = forms.CharField(widget=forms.PasswordInput(attrs={
        'placeholder': 'Confirm Password', 'class': 'input-field'
    }))

    class Meta:
        model = User
        fields = ('username', 'email', 'password1', 'password2')

class DocumentForm(forms.ModelForm):
    file = forms.FileField(required=True)  # temp file upload (not saved)

    class Meta:
        model = Document
        fields = ('file', 'job_description')
        widgets = {
            'job_description': forms.Textarea(
                attrs={
                    'placeholder': 'Paste the job description here...',
                    'rows': 6,
                    'style': 'width:100%; border:1px solid black; border-radius:5px; padding:10px;'
                }
            )
        }

class CustomUserCreationForm(UserCreationForm):
    email = forms.EmailField(required=True, widget=forms.EmailInput(attrs={
        'placeholder': 'Email',
        'class': 'form-input'
    }))

    class Meta:
        model = User
        fields = ("username", "email", "password1", "password2")