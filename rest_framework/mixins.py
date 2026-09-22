"""
Basic building blocks for generic class based views.

We don't bind behavior to http method handlers yet,
which allows mixin classes to be composed in interesting ways.
"""
from collections.abc import Mapping

from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.merge_patch import merge_patch
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.utils.mediatypes import media_type_matches


class CreateModelMixin:
    """
    Create a model instance.
    """
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def perform_create(self, serializer):
        serializer.save()

    def get_success_headers(self, data):
        try:
            return {'Location': str(data[api_settings.URL_FIELD_NAME])}
        except (TypeError, KeyError):
            return {}


class ListModelMixin:
    """
    List a queryset.
    """
    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)


class RetrieveModelMixin:
    """
    Retrieve a model instance.
    """
    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        return Response(serializer.data)


class UpdateModelMixin:
    """
    Update a model instance.
    """
    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        if getattr(instance, '_prefetched_objects_cache', None):
            # If 'prefetch_related' has been applied to a queryset, we need to
            # forcibly invalidate the prefetch cache on the instance.
            instance._prefetched_objects_cache = {}

        return Response(serializer.data)

    def perform_update(self, serializer):
        serializer.save()

    def partial_update(self, request, *args, **kwargs):
        kwargs['partial'] = True
        return self.update(request, *args, **kwargs)


class MergePatchMixin:
    """
    Update an instance from a JSON Merge Patch (RFC 7396) document.

    This is an opt-in extension of `UpdateModelMixin`: it only takes over
    `.partial_update()` when the request uses the
    `application/merge-patch+json` content type, which also requires
    `MergePatchParser` to be included in `parser_classes`. Requests using
    any other content type keep the regular `partial=True` behavior.

    The patch is merged into the current JSON representation of the
    instance, and the merged candidate document is then validated as a
    whole (field-level and object-level) before `.save()` is called once.
    """
    merge_patch_media_type = 'application/merge-patch+json'

    def partial_update(self, request, *args, **kwargs):
        if media_type_matches(self.merge_patch_media_type, request.content_type):
            return self.merge_patch_update(request, *args, **kwargs)
        return super().partial_update(request, *args, **kwargs)

    def get_merge_patch_document(self, instance):
        """
        Return the current JSON document that the merge patch is applied
        to. Defaults to the serializer representation of the instance,
        which never includes write-only fields.
        """
        return self.get_serializer(instance).data

    def get_merge_patch_server_values(self, instance):
        """
        Return server-side values that are merged into the candidate
        document before validation.

        Write-only fields are absent from the serializer representation,
        so override this hook to provide a baseline for any of them that
        must take part in validation, instead of exposing their current
        values in the representation sent to clients.
        """
        return {}

    def merge_patch_update(self, request, *args, **kwargs):
        instance = self.get_object()
        patch = request.data

        document = dict(self.get_merge_patch_document(instance))
        document.update(self.get_merge_patch_server_values(instance))
        candidate = merge_patch(document, patch)

        if not isinstance(candidate, Mapping):
            raise ValidationError({
                api_settings.NON_FIELD_ERRORS_KEY: (
                    'Applying the merge patch must result in a JSON object.'
                )
            })

        serializer = self.get_serializer(instance, data=candidate)

        if isinstance(patch, Mapping):
            read_only_deletions = [
                name for name, field in serializer.fields.items()
                if field.read_only and name in patch and patch[name] is None
            ]
            if read_only_deletions:
                raise ValidationError({
                    name: [
                        'Read-only fields cannot be removed by a merge patch.'
                    ]
                    for name in read_only_deletions
                })

        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        if getattr(instance, '_prefetched_objects_cache', None):
            # If 'prefetch_related' has been applied to a queryset, we need to
            # forcibly invalidate the prefetch cache on the instance.
            instance._prefetched_objects_cache = {}

        return Response(serializer.data)

    def perform_update(self, serializer):
        serializer.save()


class DestroyModelMixin:
    """
    Destroy a model instance.
    """
    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def perform_destroy(self, instance):
        instance.delete()
