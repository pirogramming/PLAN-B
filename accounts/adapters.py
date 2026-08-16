from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from accounts.utils import sync_username_with_email


class CustomAccountAdapter(DefaultAccountAdapter):
    def populate_username(self, request, user):
        sync_username_with_email(user)


class CustomSocialAccountAdapter(DefaultSocialAccountAdapter):
    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        sync_username_with_email(user)

        # 구글/네이버 provider의 extract_common_fields()는 email 위주라 "name"을
        # 안 넘겨준다 (naver는 email 하나뿐). 그래서 원본 프로필 응답
        # (sociallogin.account.extra_data)에서 직접 이름을 찾아야, 닉네임이
        # 비어 있는 채로 저장되어 화면에 이메일이 그대로 노출되는 걸 막을 수 있다.
        if not user.nickname:
            extra_data = sociallogin.account.extra_data or {}
            full_name = f"{user.first_name}{user.last_name}".strip()
            user.nickname = (
                extra_data.get("name")
                or extra_data.get("nickname")
                or full_name
            )

        return user