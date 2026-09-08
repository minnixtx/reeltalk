from django.contrib import admin

from .models import LinkDomain, SiteSettings, User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ["localname", "display_name", "email", "local", "is_staff"]
    list_filter = ["local", "is_staff", "is_superuser"]
    search_fields = ["localname", "display_name", "email"]


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    list_display = ["name", "signup_policy"]


@admin.register(LinkDomain)
class LinkDomainAdmin(admin.ModelAdmin):
    list_display = ["domain"]
    search_fields = ["domain"]
