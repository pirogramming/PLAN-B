from django.shortcuts import render, redirect
from django.contrib.auth import login, logout
from django.views.decorators.http import require_http_methods

from .forms import CustomUserCreationForm, CustomAuthenticationForm


# ==========================================
# 1. 회원가입 뷰 (Signup)
# ==========================================
@require_http_methods(["GET", "POST"])
def signup_view(request):
    """
    URL: /accounts/signup/
    템플릿: accounts/signup.html
    """
    if request.user.is_authenticated:
        return redirect('/')

    if request.method == 'POST':
        form = CustomUserCreationForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('accounts:login')
        # form.is_valid()가 False면 에러가 담긴 form을 내려보냄
    else:
        form = CustomUserCreationForm()

    context = {'form': form}
    return render(request, 'accounts/signup.html', context)


# ==========================================
# 2. 로그인 뷰 (Login)
# ==========================================
@require_http_methods(["GET", "POST"])
def login_view(request):
    """
    URL: /accounts/login/
    템플릿: accounts/login.html
    """
    if request.user.is_authenticated:
        return redirect('/')

    if request.method == 'POST':
        form = CustomAuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            next_url = request.POST.get('next') or request.GET.get('next') or '/'
            return redirect(next_url)
    else:
        form = CustomAuthenticationForm()

    context = {'form': form}
    return render(request, 'accounts/login.html', context)


# ==========================================
# 3. 로그아웃 뷰 (Logout)
# ==========================================
@require_http_methods(["POST"])
def logout_view(request):
    """
    URL: /accounts/logout/
    """
    logout(request)
    return redirect('accounts:login')