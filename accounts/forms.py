from django import forms
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from django.contrib.auth import get_user_model

User = get_user_model()


class CustomUserCreationForm(UserCreationForm):
    """
    회원가입 
    """
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'nickname', 'email')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # 아이디 
        if 'username' in self.fields:
            self.fields['username'].widget.attrs.update({
                'class': 'form-control',
                'placeholder': '아이디를 입력하세요',
            })
            
        # 닉네임 
        if 'nickname' in self.fields:
            self.fields['nickname'].widget.attrs.update({
                'class': 'form-control',
                'placeholder': '닉네임을 입력하세요 (선택)',
            })
            
        # 이메일 
        if 'email' in self.fields:
            self.fields['email'].widget.attrs.update({
                'class': 'form-control',
                'placeholder': 'example@email.com',
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


class CustomAuthenticationForm(AuthenticationForm):
    """
    로그인
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].widget.attrs.update({
            'class': 'form-control',
            'placeholder': '아이디',
            'autofocus': True,
        })
        self.fields['password'].widget.attrs.update({
            'class': 'form-control',
            'placeholder': '비밀번호',
        })