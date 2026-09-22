import pytest
from django.db import models
from django.test import TestCase

from rest_framework import generics, serializers
from rest_framework.projection import (
    FieldProjectionMixin, ProjectionError, apply_projection, compile_projection
)
from rest_framework.test import APIRequestFactory

from tests.models import RESTFrameworkModel

factory = APIRequestFactory()


# --- Serializers used across the plain (non-ORM) tests ---------------------

class OwnerSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    email = serializers.CharField()


class EntrySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    note = serializers.CharField()


class RecordSerializer(serializers.Serializer):
    title = serializers.CharField()
    owner = OwnerSerializer()
    entries = EntrySerializer(many=True)
    secret = serializers.CharField(write_only=True)


def make_record():
    return {
        'title': 'hello',
        'owner': {'id': 1, 'name': 'amy', 'email': 'amy@example.com'},
        'entries': [
            {'id': 10, 'note': 'first'},
            {'id': 11, 'note': 'second'},
        ],
        'secret': 'hidden',
    }


class TestCompileProjection:
    def test_compiles_dotted_paths(self):
        assert compile_projection(['title', 'owner.name']) == {
            'title': {},
            'owner': {'name': {}},
        }

    def test_parent_selection_absorbs_children(self):
        assert compile_projection(['owner.name', 'owner']) == {'owner': {}}

    def test_child_selection_absorbed_by_parent(self):
        assert compile_projection(['owner', 'owner.name']) == {'owner': {}}

    def test_empty_segments_rejected(self):
        with pytest.raises(ProjectionError):
            compile_projection(['owner..name'])
        with pytest.raises(ProjectionError):
            compile_projection(['.owner'])


class TestApplyProjection:
    def test_selects_requested_paths(self):
        serializer = RecordSerializer()
        apply_projection(serializer, ['title', 'owner.name', 'entries.id'])
        assert serializer.to_representation(make_record()) == {
            'title': 'hello',
            'owner': {'name': 'amy'},
            'entries': [{'id': 10}, {'id': 11}],
        }

    def test_output_order_follows_serializer(self):
        serializer = RecordSerializer()
        apply_projection(serializer, ['entries.id', 'owner.name', 'title'])
        result = serializer.to_representation(make_record())
        assert list(result.keys()) == ['title', 'owner', 'entries']
        assert list(result['owner'].keys()) == ['name']

    def test_parent_selection_absorbs_child_selection(self):
        serializer = RecordSerializer()
        apply_projection(serializer, ['owner.name', 'owner'])
        assert serializer.to_representation(make_record())['owner'] == {
            'id': 1, 'name': 'amy', 'email': 'amy@example.com',
        }

    def test_projection_on_many_root(self):
        serializer = RecordSerializer(many=True)
        apply_projection(serializer, ['title'])
        assert serializer.to_representation([make_record(), make_record()]) == [
            {'title': 'hello'}, {'title': 'hello'},
        ]

    def test_unknown_field_rejected(self):
        with pytest.raises(ProjectionError, match="Unknown field 'nope'"):
            apply_projection(RecordSerializer(), ['nope'])

    def test_unknown_nested_field_rejected(self):
        with pytest.raises(ProjectionError, match="Unknown field 'owner.nope'"):
            apply_projection(RecordSerializer(), ['owner.nope'])

    def test_scalar_subpath_rejected(self):
        with pytest.raises(ProjectionError, match="sub-fields of 'title'"):
            apply_projection(RecordSerializer(), ['title.length'])

    def test_write_only_field_rejected(self):
        with pytest.raises(ProjectionError, match="write-only"):
            apply_projection(RecordSerializer(), ['secret'])

    def test_whitelist_allows_listed_paths(self):
        serializer = RecordSerializer()
        apply_projection(
            serializer, ['title', 'owner.name'],
            allowed_fields=['title', 'owner.name'],
        )
        assert serializer.to_representation(make_record()) == {
            'title': 'hello', 'owner': {'name': 'amy'},
        }

    def test_whitelist_rejects_unlisted_paths(self):
        with pytest.raises(ProjectionError, match="not included in the allowed"):
            apply_projection(
                RecordSerializer(), ['owner.email'],
                allowed_fields=['owner.name'],
            )

    def test_whitelist_rejects_parent_of_whitelisted_leaves(self):
        # Selecting all of 'owner' would expose 'email', which the
        # whitelist did not open up.
        with pytest.raises(ProjectionError, match="not included in the allowed"):
            apply_projection(
                RecordSerializer(), ['owner'],
                allowed_fields=['owner.name'],
            )

    def test_whitelisted_parent_opens_subtree(self):
        serializer = RecordSerializer()
        apply_projection(
            serializer, ['owner.name'], allowed_fields=['owner'],
        )
        assert serializer.to_representation(make_record()) == {
            'owner': {'name': 'amy'},
        }

    def test_paths_use_field_names_not_source(self):
        class RenamedSerializer(serializers.Serializer):
            writer = OwnerSerializer(source='owner')

        serializer = RenamedSerializer()
        apply_projection(serializer, ['writer.name'])
        assert serializer.to_representation({'owner': {'id': 1, 'name': 'amy'}}) == {
            'writer': {'name': 'amy'},
        }

        with pytest.raises(ProjectionError, match="Unknown field 'owner'"):
            apply_projection(RenamedSerializer(), ['owner.name'])

    def test_serializer_class_is_not_mutated(self):
        declared_before = set(RecordSerializer._declared_fields)
        apply_projection(RecordSerializer(), ['title'])
        assert set(RecordSerializer._declared_fields) == declared_before
        # A fresh instance of the same class is unaffected.
        assert set(RecordSerializer().fields) == declared_before

    def test_instances_are_isolated(self):
        first = RecordSerializer()
        apply_projection(first, ['title'])
        second = RecordSerializer()
        apply_projection(second, ['owner.name'])
        assert set(first.fields) == {'title'}
        assert set(second.fields) == {'owner'}
        assert second.to_representation(make_record()) == {
            'owner': {'name': 'amy'},
        }


class TestProjectionDoesNotEvaluateDroppedFields:
    def test_dropped_attributes_and_methods_are_never_accessed(self):
        accesses = []

        class Tracked:
            @property
            def title(self):
                accesses.append('title')
                return 'hello'

            @property
            def expensive(self):
                accesses.append('expensive')
                return 'expensive'

        class TrackedSerializer(serializers.Serializer):
            title = serializers.CharField()
            expensive = serializers.CharField()
            computed = serializers.SerializerMethodField()

            def get_computed(self, obj):
                accesses.append('computed')
                return 'computed'

        serializer = TrackedSerializer()
        apply_projection(serializer, ['title'])
        assert serializer.to_representation(Tracked()) == {'title': 'hello'}
        assert accesses == ['title']


class TestRecursiveSerializerProjection:
    def test_recursive_projection_terminates(self):
        class NodeSerializer(serializers.Serializer):
            name = serializers.CharField()

        NodeSerializer._declared_fields['children'] = serializers.ListField(
            child=NodeSerializer()
        )
        try:
            data = {
                'name': 'root',
                'children': [
                    {'name': 'leaf', 'children': []},
                ],
            }
            serializer = NodeSerializer()
            apply_projection(serializer, ['name', 'children.name'])
            assert serializer.to_representation(data) == {
                'name': 'root',
                'children': [{'name': 'leaf'}],
            }

            serializer = NodeSerializer()
            apply_projection(serializer, ['name'])
            assert serializer.to_representation(data) == {'name': 'root'}
        finally:
            del NodeSerializer._declared_fields['children']


class TestNullEntriesKeepShape:
    def test_null_entries_are_preserved(self):
        class NullableEntrySerializer(serializers.Serializer):
            id = serializers.IntegerField(required=False)
            note = serializers.CharField(required=False)

        class NullableRecordSerializer(serializers.Serializer):
            title = serializers.CharField()
            entries = NullableEntrySerializer(many=True)

        data = {
            'title': 'hello',
            'entries': [{'id': 10, 'note': 'first'}, None, {'id': 11, 'note': 'second'}],
        }
        full = NullableRecordSerializer().to_representation(data)
        assert len(full['entries']) == 3

        serializer = NullableRecordSerializer()
        apply_projection(serializer, ['entries.id'])
        projected = serializer.to_representation(data)
        assert projected == {
            'entries': [{'id': 10}, {}, {'id': 11}],
        }


# --- ORM-backed tests -------------------------------------------------------

class ProjectionAuthor(RESTFrameworkModel):
    name = models.CharField(max_length=100)


class ProjectionBook(RESTFrameworkModel):
    title = models.CharField(max_length=100)
    author = models.ForeignKey(
        ProjectionAuthor, on_delete=models.CASCADE, related_name='books'
    )


class ProjectionAuthorSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectionAuthor
        fields = ['id', 'name']


class ProjectionBookSerializer(serializers.ModelSerializer):
    author = ProjectionAuthorSerializer(read_only=True)

    class Meta:
        model = ProjectionBook
        fields = ['id', 'title', 'author']


class ProjectedBookList(FieldProjectionMixin, generics.ListAPIView):
    queryset = ProjectionBook.objects.all()
    serializer_class = ProjectionBookSerializer
    projection_allowed_fields = ['id', 'title', 'author.name']


class FullBookList(generics.ListAPIView):
    queryset = ProjectionBook.objects.all()
    serializer_class = ProjectionBookSerializer


class TestProjectionQueries(TestCase):
    def setUp(self):
        self.author = ProjectionAuthor.objects.create(name='amy')
        for index in range(3):
            ProjectionBook.objects.create(
                title='book-%d' % index, author=self.author
            )

    def test_full_serialization_follows_lazy_relation(self):
        serializer = ProjectionBookSerializer(ProjectionBook.objects.all(), many=True)
        # 1 query for the books, plus 1 lazy author query per book.
        with self.assertNumQueries(4):
            data = serializer.data
        assert data[0]['author'] == {'id': self.author.pk, 'name': 'amy'}

    def test_pruned_relation_is_never_queried(self):
        serializer = ProjectionBookSerializer(ProjectionBook.objects.all(), many=True)
        apply_projection(serializer, ['title'])
        with self.assertNumQueries(1):
            data = serializer.data
        assert data == [
            {'title': 'book-0'}, {'title': 'book-1'}, {'title': 'book-2'},
        ]


class TestFieldProjectionMixin(TestCase):
    def setUp(self):
        self.author = ProjectionAuthor.objects.create(name='amy')
        self.book = ProjectionBook.objects.create(title='book-0', author=self.author)

    def test_view_returns_projected_response(self):
        view = ProjectedBookList.as_view()
        request = factory.get('/', {'fields': 'title,author.name'})
        response = view(request)
        assert response.status_code == 200
        assert response.data == [
            {'title': 'book-0', 'author': {'name': 'amy'}},
        ]

    def test_view_supports_repeated_params(self):
        view = ProjectedBookList.as_view()
        request = factory.get('/?fields=title&fields=author.name')
        response = view(request)
        assert response.status_code == 200
        assert response.data == [
            {'title': 'book-0', 'author': {'name': 'amy'}},
        ]

    def test_view_without_param_returns_full_response(self):
        view = ProjectedBookList.as_view()
        response = view(factory.get('/'))
        assert response.status_code == 200
        assert response.data == [{
            'id': self.book.pk,
            'title': 'book-0',
            'author': {'id': self.author.pk, 'name': 'amy'},
        }]

    def test_view_without_mixin_ignores_param(self):
        view = FullBookList.as_view()
        response = view(factory.get('/', {'fields': 'title'}))
        assert response.status_code == 200
        assert response.data == [{
            'id': self.book.pk,
            'title': 'book-0',
            'author': {'id': self.author.pk, 'name': 'amy'},
        }]

    def test_unknown_field_returns_400(self):
        view = ProjectedBookList.as_view()
        response = view(factory.get('/', {'fields': 'nope'}))
        assert response.status_code == 400
        assert 'nope' in str(response.data)

    def test_scalar_subpath_returns_400(self):
        view = ProjectedBookList.as_view()
        response = view(factory.get('/', {'fields': 'title.foo'}))
        assert response.status_code == 400
        assert 'title' in str(response.data)

    def test_path_outside_whitelist_returns_400(self):
        view = ProjectedBookList.as_view()
        response = view(factory.get('/', {'fields': 'author.id'}))
        assert response.status_code == 400
        assert 'author.id' in str(response.data)

    def test_two_requests_do_not_interfere(self):
        view = ProjectedBookList.as_view()
        first = view(factory.get('/', {'fields': 'title'}))
        assert first.data == [{'title': 'book-0'}]
        second = view(factory.get('/', {'fields': 'author.name'}))
        assert second.data == [{'author': {'name': 'amy'}}]
        third = view(factory.get('/'))
        assert third.data == [{
            'id': self.book.pk,
            'title': 'book-0',
            'author': {'id': self.author.pk, 'name': 'amy'},
        }]
