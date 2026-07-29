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
        fields = ('email', 'nickname')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # 아이디 
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


class CustomAuthenticationForm(AuthenticationForm):
    """
    로그인
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