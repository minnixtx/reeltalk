"""Admin registration for the notification ledger.

Read-only, in the shape of ``InviteAdmin``: a notification is the record of
something that already happened, not a setting. Nothing here is authored by an
admin — every field is written by ``notify()`` — so there is no add form, and
the change page exists to be read rather than edited.
"""

from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ["id", "recipient", "actor", "kind", "status", "created"]
    list_filter = ["kind"]
    search_fields = ["recipient__localname", "actor__localname"]
    date_hierarchy = "created"
    readonly_fields = ["recipient", "actor", "kind", "status", "created"]

    def has_add_permission(self, request):
        return False
