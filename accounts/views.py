from django.shortcuts import render, redirect
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
        return redirect('exams:period_list')

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
        return redirect('exams:period_list')

    if request.method == 'POST':
        form = CustomAuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            
            # 2. next 파라미터 가져오기
            next_url = request.POST.get('next') or request.GET.get('next')
            
            # 3. 안전한 내 내부 URL인지 검증 후 리다이렉트
            if next_url and url_has_allowed_host_and_scheme(
                url=next_url,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure()
            ):
                return redirect(next_url)
            
            # 검증 실패 시 또는 next가 없을 시 기본 메인 페이지로 이동
            return redirect('exams:period_list')
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