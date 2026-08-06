# accounts/utils.py

def sync_username_with_email(user):
    """
    User 객체의 username을 email 값과 동일하게 맞춰주는 공통 유틸리티 함수
    """
    if user.email:
        user.username = user.email
    return user