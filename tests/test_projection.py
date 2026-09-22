import pytest
from django.test import TestCase

from rest_framework import generics, serializers, status
from rest_framework.projection import (
    FieldProjectionMixin, ProjectionError, apply_projection, parse_projection
)
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from tests.models import ProjectionArticle, ProjectionEntry, ProjectionOwner

factory = APIRequestFactory()


class TrackedInstance:
    """
    Plain object that records every attribute access, used to prove that
    fields pruned by a projection are never evaluated.
    """

    def __init__(self, **attrs):
        self.accessed = []
        self._attrs = attrs

    def __getattr__(self, name):
        try:
            value = self._attrs[name]
        except KeyError:
            raise AttributeError(name)
        self.accessed.append(name)
        return value


class OwnerNestedSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


class EntryNestedSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    label = serializers.CharField()


class TrackedRootSerializer(serializers.Serializer):
    title = serializers.CharField()
    owner = OwnerNestedSerializer()
    entries = EntryNestedSerializer(many=True)
    secret = serializers.CharField()
    expensive = serializers.SerializerMethodField()

    expensive_calls = 0

    def get_expensive(self, obj):
        type(self).expensive_calls += 1
        return 'expensive'


def tracked_obj():
    return TrackedInstance(
        title='hello',
        owner=TrackedInstance(id=1, name='alice'),
        entries=[
            TrackedInstance(id=1, label='a'),
            TrackedInstance(id=2, label='b'),
        ],
        secret='s3cr3t',
    )


class ParseProjectionTests(TestCase):
    def test_parses_flat_and_nested_paths(self):
        assert parse_projection('title,owner.name') == {
            'title': None,
            'owner': {'name': None},
        }

    def test_parent_selection_absorbs_children(self):
        assert parse_projection('owner.name,owner') == {'owner': None}
        assert parse_projection('owner,owner.name') == {'owner': None}

    def test_sibling_paths_are_merged(self):
        assert parse_projection('owner.id,owner.name') == {
            'owner': {'id': None, 'name': None},
        }

    def test_whitespace_and_empty_entries_are_ignored(self):
        assert parse_projection(' title ,, owner.name ') == {
            'title': None,
            'owner': {'name': None},
        }
        assert parse_projection('') == {}

    def test_empty_path_segment_is_rejected(self):
        with pytest.raises(ProjectionError):
            parse_projection('owner..name')
        with pytest.raises(ProjectionError):
            parse_projection('owner.')


class SerializerProjectionTests(TestCase):
    def test_projection_prunes_unselected_fields(self):
        serializer = TrackedRootSerializer(tracked_obj())
        apply_projection(serializer, parse_projection('title,owner.name,entries.id'))
        assert serializer.data == {
            'title': 'hello',
            'owner': {'name': 'alice'},
            'entries': [{'id': 1}, {'id': 2}],
        }

    def test_output_order_follows_serializer_not_request(self):
        serializer = TrackedRootSerializer(tracked_obj())
        apply_projection(serializer, parse_projection('entries.id,title'))
        assert list(serializer.data.keys()) == ['title', 'entries']

    def test_parent_selection_absorbs_child_selection(self):
        serializer = TrackedRootSerializer(tracked_obj())
        apply_projection(serializer, parse_projection('owner.name,owner'))
        assert serializer.data['owner'] == {'id': 1, 'name': 'alice'}

    def test_pruned_fields_are_never_evaluated(self):
        obj = tracked_obj()
        TrackedRootSerializer.expensive_calls = 0
        serializer = TrackedRootSerializer(obj)
        apply_projection(serializer, parse_projection('title'))
        assert serializer.data == {'title': 'hello'}
        # Only the selected attribute was read off the instance.
        assert obj.accessed == ['title']
        # The SerializerMethodField for the pruned field was not called.
        assert TrackedRootSerializer.expensive_calls == 0

    def test_selected_method_field_is_evaluated(self):
        TrackedRootSerializer.expensive_calls = 0
        serializer = TrackedRootSerializer(tracked_obj())
        apply_projection(serializer, parse_projection('expensive'))
        assert serializer.data == {'expensive': 'expensive'}
        assert TrackedRootSerializer.expensive_calls == 1

    def test_projection_uses_field_names_not_source(self):
        class RenamedOwnerSerializer(serializers.Serializer):
            name = serializers.CharField(source='display_name')

        class RenamedRootSerializer(serializers.Serializer):
            title = serializers.CharField(source='headline')
            owner = RenamedOwnerSerializer(source='profile')

        obj = TrackedInstance(
            headline='hello',
            profile=TrackedInstance(display_name='alice'),
        )
        serializer = RenamedRootSerializer(obj)
        apply_projection(serializer, parse_projection('owner.name'))
        assert serializer.data == {'owner': {'name': 'alice'}}

        with pytest.raises(ProjectionError, match='profile'):
            apply_projection(
                RenamedRootSerializer(obj), parse_projection('profile.name')
            )
        with pytest.raises(ProjectionError, match='headline'):
            apply_projection(
                RenamedRootSerializer(obj), parse_projection('headline')
            )

    def test_unknown_field_is_rejected(self):
        with pytest.raises(ProjectionError, match="Unknown field 'unknown'"):
            apply_projection(
                TrackedRootSerializer(tracked_obj()),
                parse_projection('unknown'),
            )

    def test_nested_unknown_field_is_rejected(self):
        with pytest.raises(ProjectionError, match="Unknown field 'owner.unknown'"):
            apply_projection(
                TrackedRootSerializer(tracked_obj()),
                parse_projection('owner.unknown'),
            )

    def test_nested_selection_on_scalar_is_rejected(self):
        with pytest.raises(ProjectionError, match='title'):
            apply_projection(
                TrackedRootSerializer(tracked_obj()),
                parse_projection('title.value'),
            )

    def test_write_only_field_is_rejected(self):
        class WriteOnlySerializer(serializers.Serializer):
            title = serializers.CharField()
            password = serializers.CharField(write_only=True)

        with pytest.raises(ProjectionError, match='write-only'):
            apply_projection(
                WriteOnlySerializer({}), parse_projection('password')
            )

    def test_projection_applies_to_top_level_many(self):
        serializer = EntryNestedSerializer(
            [TrackedInstance(id=1, label='a'), TrackedInstance(id=2, label='b')],
            many=True,
        )
        apply_projection(serializer, parse_projection('id'))
        assert serializer.data == [{'id': 1}, {'id': 2}]

    def test_instances_of_same_class_are_isolated(self):
        projected = TrackedRootSerializer(tracked_obj())
        apply_projection(projected, parse_projection('title'))
        assert set(projected.fields) == {'title'}

        untouched = TrackedRootSerializer(tracked_obj())
        assert set(untouched.fields) == {
            'title', 'owner', 'entries', 'secret', 'expensive',
        }
        # Nested serializer classes are not modified either.
        assert set(OwnerNestedSerializer._declared_fields) == {'id', 'name'}
        assert set(EntryNestedSerializer._declared_fields) == {'id', 'label'}
        assert set(TrackedRootSerializer._declared_fields) == {
            'title', 'owner', 'entries', 'secret', 'expensive',
        }

    def test_recursive_serializer_projection_terminates(self):
        class NodeSerializer(serializers.Serializer):
            name = serializers.CharField()
            note = serializers.CharField()

        NodeSerializer._declared_fields['children'] = NodeSerializer(many=True)
        try:
            tree = {
                'name': 'root', 'note': 'r',
                'children': [
                    {
                        'name': 'child', 'note': 'c',
                        'children': [
                            {'name': 'leaf', 'note': 'l', 'children': []},
                        ],
                    },
                ],
            }
            serializer = NodeSerializer(tree)
            apply_projection(
                serializer,
                parse_projection('name,children.children.name'),
            )
            assert serializer.data == {
                'name': 'root',
                'children': [
                    {'children': [{'name': 'leaf'}]},
                ],
            }
        finally:
            del NodeSerializer._declared_fields['children']

    def test_null_entries_keep_shape(self):
        class ListRootSerializer(serializers.Serializer):
            title = serializers.CharField()
            entries = EntryNestedSerializer(many=True)

        data = {
            'title': 't',
            'entries': [
                {'id': 1, 'label': 'a'},
                None,
                {'id': 2, 'label': 'b'},
            ],
        }
        assert ListRootSerializer(data).data == {
            'title': 't',
            'entries': [{'id': 1, 'label': 'a'}, None, {'id': 2, 'label': 'b'}],
        }

        serializer = ListRootSerializer(data)
        apply_projection(serializer, parse_projection('entries.id'))
        assert serializer.data == {
            'entries': [{'id': 1}, None, {'id': 2}],
        }

    def test_allowed_fields_whitelist(self):
        allowed = {'title', 'owner.name'}
        serializer = TrackedRootSerializer(tracked_obj())
        apply_projection(
            serializer, parse_projection('owner.name'), allowed_fields=allowed
        )
        assert serializer.data == {'owner': {'name': 'alice'}}

        with pytest.raises(ProjectionError, match='not included'):
            apply_projection(
                TrackedRootSerializer(tracked_obj()),
                parse_projection('entries.id'),
                allowed_fields=allowed,
            )

    def test_wholesale_parent_beyond_whitelist_is_rejected(self):
        # The whitelist only exposes owner.name, so selecting all of owner
        # would leak fields outside the whitelist.
        with pytest.raises(ProjectionError, match='not included'):
            apply_projection(
                TrackedRootSerializer(tracked_obj()),
                parse_projection('owner'),
                allowed_fields={'owner.name'},
            )

    def test_whitelist_entry_covers_nested_paths(self):
        serializer = TrackedRootSerializer(tracked_obj())
        apply_projection(
            serializer,
            parse_projection('owner.name'),
            allowed_fields={'owner'},
        )
        assert serializer.data == {'owner': {'name': 'alice'}}


class ProjectionOwnerModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectionOwner
        fields = ['id', 'name']


class ProjectionEntryModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectionEntry
        fields = ['id', 'label']


class ProjectionArticleModelSerializer(serializers.ModelSerializer):
    owner = ProjectionOwnerModelSerializer()
    entries = ProjectionEntryModelSerializer(many=True)

    class Meta:
        model = ProjectionArticle
        fields = ['id', 'title', 'owner', 'entries']


class ProjectedArticleView(FieldProjectionMixin, generics.RetrieveAPIView):
    queryset = ProjectionArticle.objects.all()
    serializer_class = ProjectionArticleModelSerializer
    projection_allowed_fields = {
        'id', 'title', 'owner.id', 'owner.name', 'entries.id', 'entries.label',
    }


class UnrestrictedArticleView(FieldProjectionMixin, generics.RetrieveAPIView):
    queryset = ProjectionArticle.objects.all()
    serializer_class = ProjectionArticleModelSerializer


class ProjectionViewTests(TestCase):
    def setUp(self):
        self.owner = ProjectionOwner.objects.create(name='alice')
        self.article = ProjectionArticle.objects.create(
            title='hello', owner=self.owner,
        )
        ProjectionEntry.objects.create(article=self.article, label='a')
        ProjectionEntry.objects.create(article=self.article, label='b')

    def test_mobile_projection(self):
        request = factory.get(
            '/articles/1?fields=title,owner.name,entries.id'
        )
        response = ProjectedArticleView.as_view()(
            request, pk=self.article.pk
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.data == {
            'title': 'hello',
            'owner': {'name': 'alice'},
            'entries': [{'id': 1}, {'id': 2}],
        }

    def test_full_response_without_projection(self):
        request = factory.get('/articles/1')
        response = ProjectedArticleView.as_view()(
            request, pk=self.article.pk
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.data == {
            'id': self.article.pk,
            'title': 'hello',
            'owner': {'id': self.owner.pk, 'name': 'alice'},
            'entries': [{'id': 1, 'label': 'a'}, {'id': 2, 'label': 'b'}],
        }

    def test_empty_fields_param_is_ignored(self):
        request = factory.get('/articles/1?fields=')
        response = ProjectedArticleView.as_view()(
            request, pk=self.article.pk
        )
        assert response.status_code == status.HTTP_200_OK
        assert 'owner' in response.data

    def test_unknown_field_returns_400(self):
        request = factory.get('/articles/1?fields=unknown')
        response = ProjectedArticleView.as_view()(
            request, pk=self.article.pk
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert 'unknown' in str(response.data['detail'])

    def test_scalar_subpath_returns_400(self):
        request = factory.get('/articles/1?fields=title.value')
        response = ProjectedArticleView.as_view()(
            request, pk=self.article.pk
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert 'title' in str(response.data['detail'])

    def test_path_outside_whitelist_returns_400(self):
        request = factory.get('/articles/1?fields=owner')
        response = ProjectedArticleView.as_view()(
            request, pk=self.article.pk
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert 'owner' in str(response.data['detail'])

    def test_write_only_field_returns_400(self):
        class WriteOnlySerializer(serializers.Serializer):
            title = serializers.CharField()
            password = serializers.CharField(write_only=True)

        class WriteOnlyView(FieldProjectionMixin, generics.GenericAPIView):
            serializer_class = WriteOnlySerializer

            def get(self, request):
                serializer = self.get_serializer(
                    {'title': 't', 'password': 'hidden'}
                )
                return Response(serializer.data)

        request = factory.get('/?fields=password')
        response = WriteOnlyView.as_view()(request)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert 'write-only' in str(response.data['detail'])

    def test_repeated_requests_do_not_interfere(self):
        view = UnrestrictedArticleView.as_view()

        request = factory.get('/articles/1?fields=title')
        response = view(request, pk=self.article.pk)
        assert response.data == {'title': 'hello'}

        request = factory.get('/articles/1?fields=owner.id')
        response = view(request, pk=self.article.pk)
        assert response.data == {'owner': {'id': self.owner.pk}}

        request = factory.get('/articles/1')
        response = view(request, pk=self.article.pk)
        assert set(response.data) == {'id', 'title', 'owner', 'entries'}


class ProjectionQueryTests(TestCase):
    def setUp(self):
        self.owner = ProjectionOwner.objects.create(name='alice')
        self.article = ProjectionArticle.objects.create(
            title='hello', owner=self.owner,
        )
        ProjectionEntry.objects.create(article=self.article, label='a')
        ProjectionEntry.objects.create(article=self.article, label='b')

    def get_article(self):
        return ProjectionArticle.objects.get(pk=self.article.pk)

    def test_full_serialization_hits_all_relations(self):
        article = self.get_article()
        serializer = ProjectionArticleModelSerializer(article)
        with self.assertNumQueries(2):
            data = serializer.data
        assert data['owner'] == {'id': self.owner.pk, 'name': 'alice'}
        assert len(data['entries']) == 2

    def test_projected_serialization_skips_pruned_relations(self):
        article = self.get_article()
        serializer = ProjectionArticleModelSerializer(article)
        apply_projection(serializer, parse_projection('title,owner.name'))
        with self.assertNumQueries(1):
            data = serializer.data
        assert data == {'title': 'hello', 'owner': {'name': 'alice'}}

    def test_scalar_projection_performs_no_queries(self):
        article = self.get_article()
        serializer = ProjectionArticleModelSerializer(article)
        apply_projection(serializer, parse_projection('title'))
        with self.assertNumQueries(0):
            data = serializer.data
        assert data == {'title': 'hello'}
