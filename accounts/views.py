from django.conf import settings
from django.shortcuts import render, redirect, resolve_url
from django.contrib.auth import login, logout
from django.views.decorators.http import require_http_methods
from django.utils.http import url_has_allowed_host_and_scheme

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
        return redirect(settings.LOGIN_REDIRECT_URL)

    if request.method == 'POST':
        form = CustomUserCreationForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('accounts:login')
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
        return redirect(settings.LOGIN_REDIRECT_URL)

    if request.method == 'POST':
        form = CustomAuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)

            next_url = request.POST.get('next') or request.GET.get('next')
            if next_url and url_has_allowed_host_and_scheme(
                url=next_url,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                return redirect(next_url)
            return redirect(settings.LOGIN_REDIRECT_URL)
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
    return redirect(settings.LOGOUT_REDIRECT_URL)