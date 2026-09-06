from django.contrib import admin

from .models import User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ["localname", "display_name", "email", "local", "is_staff"]
    list_filter = ["local", "is_staff", "is_superuser"]
    search_fields = ["localname", "display_name", "email"]
