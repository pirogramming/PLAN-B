from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model

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
        self.assertRedirects(response, reverse('exams:period_list'))

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
        self.assertRedirects(response, reverse('exams:period_list'))

    def test_authenticated_user_redirected_from_login_page(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('accounts:login'))
        self.assertRedirects(response, reverse('exams:period_list'))