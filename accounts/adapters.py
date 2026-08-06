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
        return user