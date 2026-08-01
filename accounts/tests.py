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