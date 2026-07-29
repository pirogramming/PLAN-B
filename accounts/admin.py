from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ('id', 'username', 'email', 'nickname', 'is_staff', 'created_at')
    list_display_links = ('id', 'username', 'email')
    list_filter = ('is_staff', 'is_superuser', 'is_active', 'created_at')
    search_fields = ('username', 'email', 'nickname')
    ordering = ('-created_at',)
    fieldsets = BaseUserAdmin.fieldsets + (
        ('추가 정보', {'fields': ('nickname',)}),
    )

    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ('추가 정보', {'fields': ('email', 'nickname')}),
    )
    readonly_fields = ('created_at', 'updated_at')