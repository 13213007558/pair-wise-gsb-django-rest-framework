import pytest

from rest_framework import generics, serializers, status
from rest_framework.parsers import JSONMergePatchParser, JSONParser
from rest_framework.test import APIRequestFactory


factory = APIRequestFactory()
MERGE_PATCH = 'application/merge-patch+json'
UNSET = object()


class Config:
    def __init__(self, **values):
        self.__dict__.update(values)


class BoundsSerializer(serializers.Serializer):
    lower = serializers.IntegerField()
    upper = serializers.IntegerField()

    def validate(self, attrs):
        if attrs['lower'] > attrs['upper']:
            raise serializers.ValidationError('lower must not exceed upper')
        return attrs


class ConfigSerializer(serializers.Serializer):
    name = serializers.CharField()
    notes = serializers.CharField(allow_null=True, required=False)
    bounds = BoundsSerializer()
    labels = serializers.ListField(child=serializers.CharField())
    secret = serializers.CharField(write_only=True)
    version = serializers.IntegerField(read_only=True)

    def get_merge_patch_baseline(self, instance=None):
        baseline = super().get_merge_patch_baseline(instance)
        baseline['secret'] = instance.secret
        return baseline

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        if getattr(instance, 'notes', UNSET) is UNSET:
            representation.pop('notes')
        return representation

    def update(self, instance, validated_data):
        MergePatchView.saved_documents.append(dict(validated_data))
        return Config(
            name=validated_data.get('name', instance.name),
            notes=validated_data.get('notes', UNSET),
            bounds=dict(validated_data.get('bounds', instance.bounds)),
            labels=list(validated_data.get('labels', instance.labels)),
            secret=validated_data.get('secret', instance.secret),
            version=instance.version,
        )


class MergePatchView(generics.UpdateAPIView):
    serializer_class = ConfigSerializer
    parser_classes = [JSONMergePatchParser, JSONParser]
    saved_documents = []
    save_calls = 0
    last_serializer = None

    def get_object(self):
        return current_config

    def get_serializer(self, *args, **kwargs):
        serializer = super().get_serializer(*args, **kwargs)
        self.__class__.last_serializer = serializer
        return serializer

    def perform_update(self, serializer):
        self.__class__.save_calls += 1
        self.__class__.candidate_document = dict(serializer.initial_data)
        return super().perform_update(serializer)


class LegacyPatchView(MergePatchView):
    parser_classes = generics.GenericAPIView.parser_classes


@pytest.fixture(autouse=True)
def config():
    global current_config
    current_config = Config(
        name='service',
        notes='kept',
        bounds={'lower': 1, 'upper': 10},
        labels=['a', 'b'],
        secret='server-secret',
        version=7,
    )
    MergePatchView.save_calls = 0
    MergePatchView.saved_documents = []
    MergePatchView.last_serializer = None
    yield current_config


def patch(view, data, content_type=MERGE_PATCH):
    request = factory.patch('/', data, content_type=content_type)
    return view.as_view()(request).render()


def test_merge_patch_recursively_updates_and_replaces_arrays():
    response = patch(MergePatchView, {
        'bounds': {'upper': 20},
        'labels': ['replaced'],
    })

    assert response.status_code == status.HTTP_200_OK
    assert MergePatchView.save_calls == 1
    assert MergePatchView.saved_documents == [{
        'name': 'service',
        'notes': 'kept',
        'bounds': {'lower': 1, 'upper': 20},
        'labels': ['replaced'],
        'secret': 'server-secret',
    }]
    assert response.data == {
        'name': 'service',
        'notes': 'kept',
        'bounds': {'lower': 1, 'upper': 20},
        'labels': ['replaced'],
        'version': 7,
    }
    assert 'secret' not in response.data
    assert MergePatchView.candidate_document['version'] == 7


def test_merge_patch_null_removes_optional_key():
    response = patch(MergePatchView, {'notes': None})

    assert response.status_code == status.HTTP_200_OK
    assert 'notes' not in MergePatchView.saved_documents[0]
    assert 'notes' not in response.data


def test_json_null_still_sets_nullable_key():
    response = patch(
        MergePatchView,
        {'notes': None},
        content_type='application/json'
    )

    assert response.status_code == status.HTTP_200_OK
    assert MergePatchView.saved_documents[0]['notes'] is None
    assert response.data['notes'] is None


def test_disabled_view_keeps_partial_semantics_for_json_patch():
    response = patch(
        LegacyPatchView,
        {'name': 'changed'},
        content_type='application/json'
    )

    assert response.status_code == status.HTTP_200_OK
    assert MergePatchView.saved_documents == [{'name': 'changed'}]


def test_deleting_required_field_fails_full_validation_without_saving():
    response = patch(MergePatchView, {'name': None})

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert 'name' in response.data
    assert MergePatchView.save_calls == 0
    assert MergePatchView.saved_documents == []


def test_separately_valid_change_rejected_after_merging():
    response = patch(MergePatchView, {'bounds': {'lower': 30}})

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.data['bounds'] == {
        'non_field_errors': ['lower must not exceed upper']
    }
    assert MergePatchView.last_serializer.initial_data == {
        'name': 'service',
        'notes': 'kept',
        'bounds': {'lower': 30, 'upper': 10},
        'labels': ['a', 'b'],
        'secret': 'server-secret',
        'version': 7,
    }
    assert MergePatchView.save_calls == 0


def test_read_only_fields_cannot_be_removed_or_replaced():
    response = patch(MergePatchView, {'version': None})

    assert response.status_code == status.HTTP_200_OK
    assert response.data['version'] == 7
    assert MergePatchView.saved_documents[0] == {
        'name': 'service',
        'notes': 'kept',
        'bounds': {'lower': 1, 'upper': 10},
        'labels': ['a', 'b'],
        'secret': 'server-secret',
    }


@pytest.mark.parametrize('document', [5, 'scalar', True, ['replacement']])
def test_non_object_root_is_not_treated_as_empty_object(document):
    response = patch(MergePatchView, document)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert MergePatchView.save_calls == 0


def test_null_root_is_not_treated_as_empty_object():
    request = factory.patch('/', b'null', content_type=MERGE_PATCH)
    response = MergePatchView.as_view()(request).render()

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert MergePatchView.save_calls == 0


def test_merge_patch_must_be_explicitly_enabled():
    response = patch(LegacyPatchView, {'name': 'changed'})

    assert response.status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    assert MergePatchView.save_calls == 0
