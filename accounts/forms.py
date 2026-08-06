# accounts/forms.py

from django import forms
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from django.contrib.auth import get_user_model
from accounts.utils import sync_username_with_email

User = get_user_model()


class CustomUserCreationForm(UserCreationForm):
    """
    회원가입 폼
    """
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('email', 'nickname')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # 이메일 (아이디 역할)
        if 'email' in self.fields:
            self.fields['email'].required = True
            self.fields['email'].widget.attrs.update({
                'class': 'form-control',
                'placeholder': 'example@email.com',
                'autofocus': True,
            })
            
        # 닉네임 
        if 'nickname' in self.fields:
            self.fields['nickname'].required = True
            self.fields['nickname'].widget.attrs.update({
                'class': 'form-control',
                'placeholder': '닉네임을 입력하세요',
            })
            
        # 비밀번호 
        if 'password1' in self.fields:
            self.fields['password1'].widget.attrs.update({
                'class': 'form-control',
                'placeholder': '비밀번호를 입력하세요',
            })
        if 'password2' in self.fields:
            self.fields['password2'].widget.attrs.update({
                'class': 'form-control',
                'placeholder': '비밀번호를 한번 더 입력하세요',
            })

    def clean_email(self):
        email = self.cleaned_data.get('email')
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError('이미 가입된 이메일입니다.')
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data['email']
        
        # 공통 유틸리티 함수를 사용해 username = email 설정
        sync_username_with_email(user)
        
        if commit:
            user.save()
        return user


class CustomAuthenticationForm(AuthenticationForm):
    """
    로그인 폼
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        if 'username' in self.fields:
            self.fields['username'].label = '이메일'
            self.fields['username'].widget.attrs.update({
                'class': 'form-control',
                'placeholder': '이메일을 입력하세요',
                'autofocus': True,
            })
            
        if 'password' in self.fields:
            self.fields['password'].widget.attrs.update({
                'class': 'form-control',
                'placeholder': '비밀번호',
            })