from django.contrib import admin, messages
from django.contrib.admin.utils import quote
from django.contrib.admin.views.main import ChangeList
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from rest_framework.authtoken.models import REDACTED_TOKEN, TokenProxy
from rest_framework.settings import api_settings

User = get_user_model()


class TokenChangeList(ChangeList):
    """Map to matching User id"""
    def url_for_result(self, result):
        pk = result.user.pk
        return reverse('admin:%s_%s_change' % (self.opts.app_label,
                                               self.opts.model_name),
                       args=(quote(pk),),
                       current_app=self.model_admin.admin_site.name)


@admin.register(TokenProxy)
class TokenAdmin(admin.ModelAdmin):
    list_display = ('key_display', 'user', 'created')
    list_filter = ('created',)
    fields = ('user',)
    search_fields = ('user__%s' % User.USERNAME_FIELD,)
    search_help_text = _('Username')
    ordering = ('user__%s' % User.USERNAME_FIELD,)
    actions = None  # Actions not compatible with mapped IDs.

    @admin.display(description=_('Key'), ordering='key')
    def key_display(self, obj):
        """
        Never render a stored digest where a plaintext key used to be
        shown. Legacy rows still hold plaintext and display as before.
        """
        if obj.is_hashed:
            return REDACTED_TOKEN
        return obj.key

    def get_changelist(self, request, **kwargs):
        return TokenChangeList

    def get_object(self, request, object_id, from_field=None):
        """
        Map from User ID to matching Token.
        """
        queryset = self.get_queryset(request)
        field = User._meta.pk
        try:
            object_id = field.to_python(object_id)
            user = User.objects.get(**{field.name: object_id})
            return queryset.get(user=user)
        except (queryset.model.DoesNotExist, User.DoesNotExist, ValidationError, ValueError):
            return None

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # The plaintext key exists only on the just-created instance.
        if not change and api_settings.AUTHTOKEN_DIGEST_ENABLED:
            self.announce_raw_key(request, obj.raw_key)

    def announce_raw_key(self, request, raw_key):
        """
        Surface the one-time plaintext key in the admin response.
        """
        if not raw_key:
            return
        messages.add_message(
            request,
            messages.WARNING,
            _(
                'The token key is shown only once and cannot be '
                'retrieved later: %(key)s'
            ) % {'key': raw_key},
        )
