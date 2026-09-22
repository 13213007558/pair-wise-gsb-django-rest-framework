"""
Tests for the opt-in JSON Merge Patch (RFC 7396) support:
MergePatchParser, MergePatchMixin and the merge_patch() function.
"""
import json  # noqa

from django.test import TestCase

from rest_framework import generics, serializers, status
from rest_framework.merge_patch import merge_patch
from rest_framework.mixins import MergePatchMixin, UpdateModelMixin
from rest_framework.parsers import JSONParser, MergePatchParser
from rest_framework.test import APIRequestFactory

factory = APIRequestFactory()


class LimitsSerializer(serializers.Serializer):
    lower = serializers.IntegerField()
    upper = serializers.IntegerField()

    def validate(self, attrs):
        if attrs['lower'] > attrs['upper']:
            raise serializers.ValidationError('lower must not exceed upper')
        return attrs


class ConfigSerializer(serializers.Serializer):
    name = serializers.CharField()
    nickname = serializers.CharField(allow_null=True, required=False)
    secret = serializers.CharField(write_only=True)
    created = serializers.CharField(read_only=True)
    tags = serializers.ListField(child=serializers.CharField(), required=False)
    limits = LimitsSerializer()

    def update(self, instance, validated_data):
        view = self.context['view']
        view.save_log.append(validated_data)
        instance.update(validated_data)
        return instance


class BaseConfigView(generics.GenericAPIView):
    serializer_class = ConfigSerializer
    instance = None
    save_log = None

    def get_object(self):
        return self.instance

    def patch(self, request, *args, **kwargs):
        return self.partial_update(request, *args, **kwargs)


class MergePatchConfigView(MergePatchMixin, UpdateModelMixin, BaseConfigView):
    parser_classes = [JSONParser, MergePatchParser]

    def get_merge_patch_server_values(self, instance):
        return {'secret': instance['secret']}


class NoBaselineConfigView(MergePatchMixin, UpdateModelMixin, BaseConfigView):
    parser_classes = [JSONParser, MergePatchParser]


class PlainConfigView(UpdateModelMixin, BaseConfigView):
    parser_classes = [JSONParser]


def make_instance():
    return {
        'name': 'alpha',
        'nickname': 'al',
        'secret': 's3cret',
        'created': '2026-01-01',
        'tags': ['a', 'b'],
        'limits': {'lower': 1, 'upper': 10},
    }


def merge_patch_request(view_cls, payload):
    save_log = []
    view = view_cls.as_view(instance=make_instance(), save_log=save_log)
    request = factory.patch(
        '/config/', json.dumps(payload),
        content_type='application/merge-patch+json',
    )
    response = view(request)
    return response, save_log


class MergePatchFunctionTests(TestCase):
    def test_objects_merge_recursively(self):
        target = {'a': 1, 'nested': {'x': 1, 'y': 2}}
        patch = {'nested': {'y': 3, 'z': 4}}
        assert merge_patch(target, patch) == {
            'a': 1, 'nested': {'x': 1, 'y': 3, 'z': 4},
        }

    def test_null_removes_key(self):
        assert merge_patch({'a': 1, 'b': 2}, {'a': None}) == {'b': 2}

    def test_arrays_replace_wholesale(self):
        assert merge_patch({'a': [1, 2]}, {'a': [3]}) == {'a': [3]}

    def test_scalar_replaces_object(self):
        assert merge_patch({'a': {'b': 1}}, {'a': 5}) == {'a': 5}

    def test_non_object_patch_replaces_target(self):
        assert merge_patch({'a': 1}, 'scalar') == 'scalar'
        assert merge_patch({'a': 1}, None) is None

    def test_target_and_patch_are_not_mutated(self):
        target = {'nested': {'x': 1}}
        patch = {'nested': {'y': 2}}
        merge_patch(target, patch)
        assert target == {'nested': {'x': 1}}
        assert patch == {'nested': {'y': 2}}


class MergePatchViewTests(TestCase):
    def test_merge_is_recursive_and_save_called_once(self):
        response, save_log = merge_patch_request(
            MergePatchConfigView, {'name': 'beta', 'limits': {'upper': 20}},
        )
        assert response.status_code == status.HTTP_200_OK
        assert len(save_log) == 1
        saved = save_log[0]
        assert saved['name'] == 'beta'
        assert saved['limits'] == {'lower': 1, 'upper': 20}

    def test_omitted_fields_are_preserved(self):
        response, save_log = merge_patch_request(MergePatchConfigView, {'name': 'beta'})
        assert response.status_code == status.HTTP_200_OK
        saved = save_log[0]
        assert saved['nickname'] == 'al'
        assert saved['tags'] == ['a', 'b']
        assert saved['limits'] == {'lower': 1, 'upper': 10}

    def test_null_deletes_nullable_field(self):
        response, save_log = merge_patch_request(
            MergePatchConfigView, {'nickname': None},
        )
        assert response.status_code == status.HTTP_200_OK
        assert 'nickname' not in save_log[0]

    def test_explicit_null_in_application_json_is_not_a_deletion(self):
        save_log = []
        view = MergePatchConfigView.as_view(
            instance=make_instance(), save_log=save_log,
        )
        request = factory.patch('/config/', {'nickname': None}, format='json')
        response = view(request)
        assert response.status_code == status.HTTP_200_OK
        assert save_log[0] == {'nickname': None}

    def test_deleting_required_field_fails(self):
        response, save_log = merge_patch_request(MergePatchConfigView, {'name': None})
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert 'name' in response.data
        assert save_log == []

    def test_deleting_nested_required_field_fails(self):
        response, save_log = merge_patch_request(
            MergePatchConfigView, {'limits': {'upper': None}},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert 'limits' in response.data
        assert save_log == []

    def test_read_only_field_cannot_be_deleted(self):
        response, save_log = merge_patch_request(MergePatchConfigView, {'created': None})
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert 'created' in response.data
        assert save_log == []

    def test_arrays_are_replaced_not_merged(self):
        response, save_log = merge_patch_request(MergePatchConfigView, {'tags': ['c']})
        assert response.status_code == status.HTTP_200_OK
        assert save_log[0]['tags'] == ['c']

    def test_scalar_root_is_not_treated_as_empty_object(self):
        for payload in ('42', '"text"', '[1, 2]', 'null'):
            save_log = []
            view = MergePatchConfigView.as_view(
                instance=make_instance(), save_log=save_log,
            )
            request = factory.patch(
                '/config/', payload,
                content_type='application/merge-patch+json',
            )
            response = view(request)
            assert response.status_code == status.HTTP_400_BAD_REQUEST
            assert save_log == []

    def test_merged_invariant_violation_rejects_wholesale(self):
        save_log = []
        instance = make_instance()
        view = MergePatchConfigView.as_view(instance=instance, save_log=save_log)
        request = factory.patch(
            '/config/',
            json.dumps({'name': 'renamed', 'limits': {'lower': 50}}),
            content_type='application/merge-patch+json',
        )
        response = view(request)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert save_log == []
        assert instance['name'] == 'alpha'
        assert instance['limits'] == {'lower': 1, 'upper': 10}

    def test_write_only_field_uses_server_side_baseline(self):
        response, save_log = merge_patch_request(MergePatchConfigView, {'name': 'beta'})
        assert response.status_code == status.HTTP_200_OK
        assert save_log[0]['secret'] == 's3cret'
        assert 'secret' not in response.data

    def test_write_only_field_without_baseline_fails_validation(self):
        response, save_log = merge_patch_request(NoBaselineConfigView, {'name': 'beta'})
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert 'secret' in response.data
        assert save_log == []

    def test_empty_patch_validates_and_saves_current_document(self):
        response, save_log = merge_patch_request(MergePatchConfigView, {})
        assert response.status_code == status.HTTP_200_OK
        assert len(save_log) == 1
        assert save_log[0]['name'] == 'alpha'

    def test_json_patch_on_mixin_view_keeps_partial_semantics(self):
        save_log = []
        view = MergePatchConfigView.as_view(
            instance=make_instance(), save_log=save_log,
        )
        request = factory.patch('/config/', {'name': 'beta'}, format='json')
        response = view(request)
        assert response.status_code == status.HTTP_200_OK
        assert save_log[0] == {'name': 'beta'}


class MergePatchOptInTests(TestCase):
    def test_plain_view_keeps_partial_semantics(self):
        save_log = []
        view = PlainConfigView.as_view(instance=make_instance(), save_log=save_log)
        request = factory.patch('/config/', {'name': 'beta'}, format='json')
        response = view(request)
        assert response.status_code == status.HTTP_200_OK
        assert save_log[0] == {'name': 'beta'}

    def test_plain_view_rejects_merge_patch_media_type(self):
        view = PlainConfigView.as_view(instance=make_instance(), save_log=[])
        request = factory.patch(
            '/config/', json.dumps({'name': 'beta'}),
            content_type='application/merge-patch+json',
        )
        response = view(request)
        assert response.status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
