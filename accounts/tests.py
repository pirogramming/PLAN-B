from django.test import TestCase, RequestFactory
from django.urls import reverse
from django.contrib.auth import get_user_model
from allauth.socialaccount.models import SocialAccount, SocialLogin
from accounts.adapters import CustomSocialAccountAdapter

User = get_user_model()


class SignupTests(TestCase):
    def test_signup_with_different_emails_succeeds_each_time(self):
        """서로 다른 이메일로 회원가입을 두 번 해도 둘 다 정상 생성되어야 한다."""
        response1 = self.client.post(reverse('accounts:signup'), {
            'email': 'user1@example.com',
            'nickname': '유저1',
            'password1': 'StrongPass123!',
            'password2': 'StrongPass123!',
        })
        response2 = self.client.post(reverse('accounts:signup'), {
            'email': 'user2@example.com',
            'nickname': '유저2',
            'password1': 'StrongPass123!',
            'password2': 'StrongPass123!',
        })

        self.assertEqual(response1.status_code, 302)
        self.assertEqual(response2.status_code, 302)
        self.assertEqual(User.objects.count(), 2)

        user1 = User.objects.get(email='user1@example.com')
        user2 = User.objects.get(email='user2@example.com')
        # username에 email이 채워지고 서로 달라 unique 제약을 통과해야 함
        self.assertEqual(user1.username, 'user1@example.com')
        self.assertEqual(user2.username, 'user2@example.com')

    def test_signup_with_duplicate_email_fails(self):
        User.objects.create_user(
            username='user1@example.com', email='user1@example.com', password='StrongPass123!'
        )
        response = self.client.post(reverse('accounts:signup'), {
            'email': 'user1@example.com',
            'nickname': '중복유저',
            'password1': 'StrongPass123!',
            'password2': 'StrongPass123!',
        })
        self.assertEqual(response.status_code, 200)  # 에러와 함께 폼 재렌더링
        self.assertEqual(User.objects.count(), 1)
        self.assertFormError(
            response.context['form'], 'email', '이미 가입된 이메일입니다.'
        )

class LoginRedirectTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='redirect@example.com', email='redirect@example.com', password='StrongPass123!'
        )

    def test_login_redirects_to_default_when_no_next(self):
        response = self.client.post(reverse('accounts:login'), {
            'username': 'redirect@example.com',
            'password': 'StrongPass123!',
        })
        self.assertRedirects(response, reverse('planner:dashboard'))

    def test_login_redirects_to_safe_next(self):
        next_url = reverse('exams:period_list')
        response = self.client.post(
            f"{reverse('accounts:login')}?next={next_url}",
            {'username': 'redirect@example.com', 'password': 'StrongPass123!', 'next': next_url},
        )
        self.assertRedirects(response, next_url)

    def test_login_rejects_open_redirect(self):
        malicious_next = 'https://evil-phishing-site.com/'
        response = self.client.post(
            reverse('accounts:login'),
            {
                'username': 'redirect@example.com',
                'password': 'StrongPass123!',
                'next': malicious_next,
            },
        )
        # 외부 도메인은 차단되고 기본 리다이렉트로 fallback 되어야 함
        self.assertRedirects(response, reverse('planner:dashboard'))

    def test_authenticated_user_redirected_from_login_page(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('accounts:login'))
        self.assertRedirects(response, reverse('planner:dashboard'))


class SocialAccountAdapterTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.request = self.factory.get('/')

    def test_populate_user_sets_username_to_email(self):
        adapter = CustomSocialAccountAdapter()
        
        # 1. SocialLogin 객체 사전 생성
        sociallogin = SocialLogin(
            account=SocialAccount(provider='google', uid='12345')
        )
        
        # 2. adapter.new_user()에 request와 sociallogin을 함께 전달
        user = adapter.new_user(self.request, sociallogin)
        sociallogin.user = user

        data = {'email': 'googleuser@example.com', 'username': 'RandomGoogleName'}

        # 3. populate_user 호출
        user = adapter.populate_user(self.request, sociallogin, data)

        # 4. username이 email과 동일하게 들어갔는지 확인
        self.assertEqual(user.username, 'googleuser@example.com')

    def test_populate_user_sets_nickname_from_extra_data_name(self):
        """구글처럼 extra_data에 name이 있으면 그걸 nickname으로 채워야 한다."""
        adapter = CustomSocialAccountAdapter()
        sociallogin = SocialLogin(
            account=SocialAccount(
                provider='google', uid='12345',
                extra_data={'name': '홍길동'},
            )
        )
        user = adapter.new_user(self.request, sociallogin)
        sociallogin.user = user

        data = {'email': 'googleuser@example.com'}
        user = adapter.populate_user(self.request, sociallogin, data)

        self.assertEqual(user.nickname, '홍길동')

    def test_populate_user_sets_nickname_from_extra_data_naver_nickname(self):
        """네이버는 extra_data에 name이 없을 수 있어 nickname 필드로 폴백해야 한다."""
        adapter = CustomSocialAccountAdapter()
        sociallogin = SocialLogin(
            account=SocialAccount(
                provider='naver', uid='67890',
                extra_data={'nickname': '네이버유저'},
            )
        )
        user = adapter.new_user(self.request, sociallogin)
        sociallogin.user = user

        data = {'email': 'naveruser@example.com'}
        user = adapter.populate_user(self.request, sociallogin, data)

        self.assertEqual(user.nickname, '네이버유저')