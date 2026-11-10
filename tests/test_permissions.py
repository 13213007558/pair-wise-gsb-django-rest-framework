import base64
import unittest
from unittest import mock

import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser, Group, Permission, User
from django.db import models
from django.test import TestCase
from django.urls import ResolverMatch

from rest_framework import (
    HTTP_HEADER_ENCODING, authentication, generics, permissions, serializers,
    status, views
)
from rest_framework.routers import DefaultRouter
from rest_framework.test import APIRequestFactory
from tests.models import BasicModel

factory = APIRequestFactory()


class BasicSerializer(serializers.ModelSerializer):
    class Meta:
        model = BasicModel
        fields = '__all__'


class RootView(generics.ListCreateAPIView):
    queryset = BasicModel.objects.all()
    serializer_class = BasicSerializer
    authentication_classes = [authentication.BasicAuthentication]
    permission_classes = [permissions.DjangoModelPermissions]


class InstanceView(generics.RetrieveUpdateDestroyAPIView):
    queryset = BasicModel.objects.all()
    serializer_class = BasicSerializer
    authentication_classes = [authentication.BasicAuthentication]
    permission_classes = [permissions.DjangoModelPermissions]


class GetQuerySetListView(generics.ListCreateAPIView):
    serializer_class = BasicSerializer
    authentication_classes = [authentication.BasicAuthentication]
    permission_classes = [permissions.DjangoModelPermissions]

    def get_queryset(self):
        return BasicModel.objects.all()


class EmptyListView(generics.ListCreateAPIView):
    queryset = BasicModel.objects.none()
    serializer_class = BasicSerializer
    authentication_classes = [authentication.BasicAuthentication]
    permission_classes = [permissions.DjangoModelPermissions]


class IgnoredGetQuerySetListView(GetQuerySetListView):
    _ignore_model_permissions = True


root_view = RootView.as_view()
api_root_view = DefaultRouter().get_api_root_view()
instance_view = InstanceView.as_view()
get_queryset_list_view = GetQuerySetListView.as_view()
empty_list_view = EmptyListView.as_view()
ignored_get_queryset_list_view = IgnoredGetQuerySetListView.as_view()


def basic_auth_header(username, password):
    credentials = ('%s:%s' % (username, password))
    base64_credentials = base64.b64encode(credentials.encode(HTTP_HEADER_ENCODING)).decode(HTTP_HEADER_ENCODING)
    return 'Basic %s' % base64_credentials


@pytest.mark.usefixtures("reset_sequences")
class ModelPermissionsIntegrationTests(TestCase):
    def setUp(self):
        User.objects.create_user('disallowed', 'disallowed@example.com', 'password')
        user = User.objects.create_user('permitted', 'permitted@example.com', 'password')
        user.user_permissions.set([
            Permission.objects.get(codename='add_basicmodel'),
            Permission.objects.get(codename='change_basicmodel'),
            Permission.objects.get(codename='delete_basicmodel')
        ])

        user = User.objects.create_user('updateonly', 'updateonly@example.com', 'password')
        user.user_permissions.set([
            Permission.objects.get(codename='change_basicmodel'),
        ])

        self.permitted_credentials = basic_auth_header('permitted', 'password')
        self.disallowed_credentials = basic_auth_header('disallowed', 'password')
        self.updateonly_credentials = basic_auth_header('updateonly', 'password')

        BasicModel(text='foo').save()

    def test_has_create_permissions(self):
        request = factory.post('/', {'text': 'foobar'}, format='json',
                               HTTP_AUTHORIZATION=self.permitted_credentials)
        response = root_view(request, pk=1)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_api_root_view_discard_default_django_model_permission(self):
        """
        We check that DEFAULT_PERMISSION_CLASSES can
        apply to APIRoot view. More specifically we check expected behavior of
        ``_ignore_model_permissions`` attribute support.
        """
        request = factory.get('/', format='json',
                              HTTP_AUTHORIZATION=self.permitted_credentials)
        request.resolver_match = ResolverMatch('get', (), {})
        response = api_root_view(request)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_ignore_model_permissions_with_unauthenticated_user(self):
        """
        We check that the ``_ignore_model_permissions`` attribute
        doesn't ignore the authentication.
        """
        request = factory.get('/', format='json')
        request.resolver_match = ResolverMatch('get', (), {})
        response = ignored_get_queryset_list_view(request)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_ignore_model_permissions_with_authenticated_user(self):
        """
        We check that the ``_ignore_model_permissions`` attribute
        with an authenticated user.
        """
        request = factory.get('/', format='json',
                              HTTP_AUTHORIZATION=self.permitted_credentials)
        request.resolver_match = ResolverMatch('get', (), {})
        response = ignored_get_queryset_list_view(request)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_get_queryset_has_create_permissions(self):
        request = factory.post('/', {'text': 'foobar'}, format='json',
                               HTTP_AUTHORIZATION=self.permitted_credentials)
        response = get_queryset_list_view(request, pk=1)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_has_put_permissions(self):
        request = factory.put('/1', {'text': 'foobar'}, format='json',
                              HTTP_AUTHORIZATION=self.permitted_credentials)
        response = instance_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_has_delete_permissions(self):
        request = factory.delete('/1', HTTP_AUTHORIZATION=self.permitted_credentials)
        response = instance_view(request, pk=1)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_does_not_have_create_permissions(self):
        request = factory.post('/', {'text': 'foobar'}, format='json',
                               HTTP_AUTHORIZATION=self.disallowed_credentials)
        response = root_view(request, pk=1)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_does_not_have_put_permissions(self):
        request = factory.put('/1', {'text': 'foobar'}, format='json',
                              HTTP_AUTHORIZATION=self.disallowed_credentials)
        response = instance_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_does_not_have_delete_permissions(self):
        request = factory.delete('/1', HTTP_AUTHORIZATION=self.disallowed_credentials)
        response = instance_view(request, pk=1)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_options_permitted(self):
        request = factory.options(
            '/',
            HTTP_AUTHORIZATION=self.permitted_credentials
        )
        response = root_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('actions', response.data)
        self.assertEqual(list(response.data['actions']), ['POST'])

        request = factory.options(
            '/1',
            HTTP_AUTHORIZATION=self.permitted_credentials
        )
        response = instance_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('actions', response.data)
        self.assertEqual(list(response.data['actions']), ['PUT'])

    def test_options_disallowed(self):
        request = factory.options(
            '/',
            HTTP_AUTHORIZATION=self.disallowed_credentials
        )
        response = root_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn('actions', response.data)

        request = factory.options(
            '/1',
            HTTP_AUTHORIZATION=self.disallowed_credentials
        )
        response = instance_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn('actions', response.data)

    def test_options_updateonly(self):
        request = factory.options(
            '/',
            HTTP_AUTHORIZATION=self.updateonly_credentials
        )
        response = root_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn('actions', response.data)

        request = factory.options(
            '/1',
            HTTP_AUTHORIZATION=self.updateonly_credentials
        )
        response = instance_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('actions', response.data)
        self.assertEqual(list(response.data['actions']), ['PUT'])

    def test_empty_view_does_not_assert(self):
        request = factory.get('/1', HTTP_AUTHORIZATION=self.permitted_credentials)
        response = empty_list_view(request, pk=1)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_calling_method_not_allowed(self):
        request = factory.generic('METHOD_NOT_ALLOWED', '/', HTTP_AUTHORIZATION=self.permitted_credentials)
        response = root_view(request)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

        request = factory.generic('METHOD_NOT_ALLOWED', '/1', HTTP_AUTHORIZATION=self.permitted_credentials)
        response = instance_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_check_auth_before_queryset_call(self):
        class View(RootView):
            def get_queryset(_):
                self.fail('should not reach due to auth check')
        view = View.as_view()

        request = factory.get('/', HTTP_AUTHORIZATION='')
        response = view(request)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_queryset_assertions(self):
        class View(views.APIView):
            authentication_classes = [authentication.BasicAuthentication]
            permission_classes = [permissions.DjangoModelPermissions]
        view = View.as_view()

        request = factory.get('/', HTTP_AUTHORIZATION=self.permitted_credentials)
        msg = 'Cannot apply DjangoModelPermissions on a view that does not set `.queryset` or have a `.get_queryset()` method.'
        with self.assertRaisesMessage(AssertionError, msg):
            view(request)

        # Faulty `get_queryset()` methods should trigger the above "view does not have a queryset" assertion.
        class View(RootView):
            def get_queryset(self):
                return None
        view = View.as_view()

        request = factory.get('/', HTTP_AUTHORIZATION=self.permitted_credentials)
        with self.assertRaisesMessage(AssertionError, 'View.get_queryset() returned None'):
            view(request)


class BasicPermModel(models.Model):
    text = models.CharField(max_length=100)

    class Meta:
        app_label = 'tests'


class BasicPermSerializer(serializers.ModelSerializer):
    class Meta:
        model = BasicPermModel
        fields = '__all__'


# Custom object-level permission, that includes 'view' permissions
class ViewObjectPermissions(permissions.DjangoObjectPermissions):
    perms_map = {
        'GET': ['%(app_label)s.view_%(model_name)s'],
        'OPTIONS': ['%(app_label)s.view_%(model_name)s'],
        'HEAD': ['%(app_label)s.view_%(model_name)s'],
        'POST': ['%(app_label)s.add_%(model_name)s'],
        'PUT': ['%(app_label)s.change_%(model_name)s'],
        'PATCH': ['%(app_label)s.change_%(model_name)s'],
        'DELETE': ['%(app_label)s.delete_%(model_name)s'],
    }


class ObjectPermissionInstanceView(generics.RetrieveUpdateDestroyAPIView):
    queryset = BasicPermModel.objects.all()
    serializer_class = BasicPermSerializer
    authentication_classes = [authentication.BasicAuthentication]
    permission_classes = [ViewObjectPermissions]


object_permissions_view = ObjectPermissionInstanceView.as_view()


class ObjectPermissionListView(generics.ListAPIView):
    queryset = BasicPermModel.objects.all()
    serializer_class = BasicPermSerializer
    authentication_classes = [authentication.BasicAuthentication]
    permission_classes = [ViewObjectPermissions]


object_permissions_list_view = ObjectPermissionListView.as_view()


class GetQuerysetObjectPermissionInstanceView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = BasicPermSerializer
    authentication_classes = [authentication.BasicAuthentication]
    permission_classes = [ViewObjectPermissions]

    def get_queryset(self):
        return BasicPermModel.objects.all()


get_queryset_object_permissions_view = GetQuerysetObjectPermissionInstanceView.as_view()


@pytest.mark.usefixtures("reset_sequences")
@unittest.skipUnless('guardian' in settings.INSTALLED_APPS, 'django-guardian not installed')
class ObjectPermissionsIntegrationTests(TestCase):
    """
    Integration tests for the object level permissions API.
    """
    def setUp(self):
        from guardian.shortcuts import assign_perm

        # create users
        create = User.objects.create_user
        users = {
            'fullaccess': create('fullaccess', 'fullaccess@example.com', 'password'),
            'readonly': create('readonly', 'readonly@example.com', 'password'),
            'writeonly': create('writeonly', 'writeonly@example.com', 'password'),
            'deleteonly': create('deleteonly', 'deleteonly@example.com', 'password'),
        }

        # give everyone model level permissions, as we are not testing those
        everyone = Group.objects.create(name='everyone')
        model_name = BasicPermModel._meta.model_name
        app_label = BasicPermModel._meta.app_label
        f = '{}_{}'.format
        perms = {
            'view': f('view', model_name),
            'change': f('change', model_name),
            'delete': f('delete', model_name)
        }
        for perm in perms.values():
            perm = f'{app_label}.{perm}'
            assign_perm(perm, everyone)
        everyone.user_set.add(*users.values())

        # appropriate object level permissions
        readers = Group.objects.create(name='readers')
        writers = Group.objects.create(name='writers')
        deleters = Group.objects.create(name='deleters')

        model = BasicPermModel.objects.create(text='foo')

        assign_perm(perms['view'], readers, model)
        assign_perm(perms['change'], writers, model)
        assign_perm(perms['delete'], deleters, model)

        readers.user_set.add(users['fullaccess'], users['readonly'])
        writers.user_set.add(users['fullaccess'], users['writeonly'])
        deleters.user_set.add(users['fullaccess'], users['deleteonly'])

        self.credentials = {}
        for user in users.values():
            self.credentials[user.username] = basic_auth_header(user.username, 'password')

    # Delete
    def test_can_delete_permissions(self):
        request = factory.delete('/1', HTTP_AUTHORIZATION=self.credentials['deleteonly'])
        response = object_permissions_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_cannot_delete_permissions(self):
        request = factory.delete('/1', HTTP_AUTHORIZATION=self.credentials['readonly'])
        response = object_permissions_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # Update
    def test_can_update_permissions(self):
        request = factory.patch(
            '/1', {'text': 'foobar'}, format='json',
            HTTP_AUTHORIZATION=self.credentials['writeonly']
        )
        response = object_permissions_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get('text'), 'foobar')

    def test_cannot_update_permissions(self):
        request = factory.patch(
            '/1', {'text': 'foobar'}, format='json',
            HTTP_AUTHORIZATION=self.credentials['deleteonly']
        )
        response = object_permissions_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_cannot_update_permissions_non_existing(self):
        request = factory.patch(
            '/999', {'text': 'foobar'}, format='json',
            HTTP_AUTHORIZATION=self.credentials['deleteonly']
        )
        response = object_permissions_view(request, pk='999')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Read
    def test_can_read_permissions(self):
        request = factory.get('/1', HTTP_AUTHORIZATION=self.credentials['readonly'])
        response = object_permissions_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_cannot_read_permissions(self):
        request = factory.get('/1', HTTP_AUTHORIZATION=self.credentials['writeonly'])
        response = object_permissions_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_can_read_get_queryset_permissions(self):
        """
        same as ``test_can_read_permissions`` but with a view
        that rely on ``.get_queryset()`` instead of ``.queryset``.
        """
        request = factory.get('/1', HTTP_AUTHORIZATION=self.credentials['readonly'])
        response = get_queryset_object_permissions_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # Read list
    # Note: this previously tested `DjangoObjectPermissionsFilter`, which has
    # since been moved to a separate package. These now act as sanity checks.
    def test_can_read_list_permissions(self):
        request = factory.get('/', HTTP_AUTHORIZATION=self.credentials['readonly'])
        response = object_permissions_list_view(request)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0].get('id'), 1)

    def test_cannot_method_not_allowed(self):
        request = factory.generic('METHOD_NOT_ALLOWED', '/', HTTP_AUTHORIZATION=self.credentials['readonly'])
        response = object_permissions_list_view(request)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class BasicPerm(permissions.BasePermission):
    def has_permission(self, request, view):
        return False


class BasicPermWithDetail(permissions.BasePermission):
    message = 'Custom: You cannot access this resource'
    code = 'permission_denied_custom'

    def has_permission(self, request, view):
        return False


class BasicObjectPerm(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        return False


class BasicObjectPermWithDetail(permissions.BasePermission):
    message = 'Custom: You cannot access this resource'
    code = 'permission_denied_custom'

    def has_object_permission(self, request, view, obj):
        return False


class PermissionInstanceView(generics.RetrieveUpdateDestroyAPIView):
    queryset = BasicModel.objects.all()
    serializer_class = BasicSerializer


class DeniedView(PermissionInstanceView):
    permission_classes = (BasicPerm,)


class DeniedViewWithDetail(PermissionInstanceView):
    permission_classes = (BasicPermWithDetail,)


class DeniedObjectView(PermissionInstanceView):
    permission_classes = (BasicObjectPerm,)


class DeniedObjectViewWithDetail(PermissionInstanceView):
    permission_classes = (BasicObjectPermWithDetail,)


denied_view = DeniedView.as_view()

denied_view_with_detail = DeniedViewWithDetail.as_view()

denied_object_view = DeniedObjectView.as_view()

denied_object_view_with_detail = DeniedObjectViewWithDetail.as_view()


@pytest.mark.usefixtures("reset_sequences")
class CustomPermissionsTests(TestCase):
    def setUp(self):
        BasicModel(text='foo').save()
        User.objects.create_user('username', 'username@example.com', 'password')
        credentials = basic_auth_header('username', 'password')
        self.request = factory.get('/1', format='json', HTTP_AUTHORIZATION=credentials)
        self.custom_message = 'Custom: You cannot access this resource'
        self.custom_code = 'permission_denied_custom'

    def test_permission_denied(self):
        response = denied_view(self.request, pk=1)
        detail = response.data.get('detail')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertNotEqual(detail, self.custom_message)
        self.assertNotEqual(detail.code, self.custom_code)

    def test_permission_denied_with_custom_detail(self):
        response = denied_view_with_detail(self.request, pk=1)
        detail = response.data.get('detail')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(detail, self.custom_message)
        self.assertEqual(detail.code, self.custom_code)

    def test_permission_denied_for_object(self):
        response = denied_object_view(self.request, pk=1)
        detail = response.data.get('detail')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertNotEqual(detail, self.custom_message)
        self.assertNotEqual(detail.code, self.custom_code)

    def test_permission_denied_for_object_with_custom_detail(self):
        response = denied_object_view_with_detail(self.request, pk=1)
        detail = response.data.get('detail')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(detail, self.custom_message)
        self.assertEqual(detail.code, self.custom_code)


class PermissionsCompositionTests(TestCase):

    def setUp(self):
        self.username = 'john'
        self.email = 'lennon@thebeatles.com'
        self.password = 'password'
        self.user = User.objects.create_user(
            self.username,
            self.email,
            self.password
        )
        self.client.login(username=self.username, password=self.password)

    def test_and_false(self):
        request = factory.get('/1', format='json')
        request.user = AnonymousUser()
        composed_perm = permissions.IsAuthenticated & permissions.AllowAny
        assert composed_perm().has_permission(request, None) is False

    def test_and_true(self):
        request = factory.get('/1', format='json')
        request.user = self.user
        composed_perm = permissions.IsAuthenticated & permissions.AllowAny
        assert composed_perm().has_permission(request, None) is True

    def test_or_false(self):
        request = factory.get('/1', format='json')
        request.user = AnonymousUser()
        composed_perm = permissions.IsAuthenticated | permissions.AllowAny
        assert composed_perm().has_permission(request, None) is True

    def test_or_true(self):
        request = factory.get('/1', format='json')
        request.user = self.user
        composed_perm = permissions.IsAuthenticated | permissions.AllowAny
        assert composed_perm().has_permission(request, None) is True

    def test_not_false(self):
        request = factory.get('/1', format='json')
        request.user = AnonymousUser()
        composed_perm = ~permissions.IsAuthenticated
        assert composed_perm().has_permission(request, None) is True

    def test_not_true(self):
        request = factory.get('/1', format='json')
        request.user = self.user
        composed_perm = ~permissions.AllowAny
        assert composed_perm().has_permission(request, None) is False

    def test_several_levels_without_negation(self):
        request = factory.get('/1', format='json')
        request.user = self.user
        composed_perm = (
            permissions.IsAuthenticated &
            permissions.IsAuthenticated &
            permissions.IsAuthenticated &
            permissions.IsAuthenticated
        )
        assert composed_perm().has_permission(request, None) is True

    def test_several_levels_and_precedence_with_negation(self):
        request = factory.get('/1', format='json')
        request.user = self.user
        composed_perm = (
            permissions.IsAuthenticated &
            ~ permissions.IsAdminUser &
            permissions.IsAuthenticated &
            ~(permissions.IsAdminUser & permissions.IsAdminUser)
        )
        assert composed_perm().has_permission(request, None) is True

    def test_several_levels_and_precedence(self):
        request = factory.get('/1', format='json')
        request.user = self.user
        composed_perm = (
            permissions.IsAuthenticated &
            permissions.IsAuthenticated |
            permissions.IsAuthenticated &
            permissions.IsAuthenticated
        )
        assert composed_perm().has_permission(request, None) is True

    def test_or_laziness(self):
        request = factory.get('/1', format='json')
        request.user = AnonymousUser()

        with mock.patch.object(permissions.AllowAny, 'has_permission', return_value=True) as mock_allow:
            with mock.patch.object(permissions.IsAuthenticated, 'has_permission', return_value=False) as mock_deny:
                composed_perm = (permissions.AllowAny | permissions.IsAuthenticated)
                hasperm = composed_perm().has_permission(request, None)
                assert hasperm is True
                assert mock_allow.call_count == 1
                mock_deny.assert_not_called()

        with mock.patch.object(permissions.AllowAny, 'has_permission', return_value=True) as mock_allow:
            with mock.patch.object(permissions.IsAuthenticated, 'has_permission', return_value=False) as mock_deny:
                composed_perm = (permissions.IsAuthenticated | permissions.AllowAny)
                hasperm = composed_perm().has_permission(request, None)
                assert hasperm is True
                assert mock_deny.call_count == 1
                assert mock_allow.call_count == 1

    def test_object_or_laziness(self):
        request = factory.get('/1', format='json')
        request.user = AnonymousUser()

        with mock.patch.object(permissions.AllowAny, 'has_object_permission', return_value=True) as mock_allow:
            with mock.patch.object(permissions.IsAuthenticated, 'has_object_permission', return_value=False) as mock_deny:
                composed_perm = (permissions.AllowAny | permissions.IsAuthenticated)
                hasperm = composed_perm().has_object_permission(request, None, None)
                assert hasperm is True
                assert mock_allow.call_count == 1
                mock_deny.assert_not_called()

        with mock.patch.object(permissions.AllowAny, 'has_object_permission', return_value=True) as mock_allow:
            with mock.patch.object(permissions.IsAuthenticated, 'has_object_permission', return_value=False) as mock_deny:
                composed_perm = (permissions.IsAuthenticated | permissions.AllowAny)
                hasperm = composed_perm().has_object_permission(request, None, None)
                assert hasperm is True
                assert mock_deny.call_count == 0
                assert mock_allow.call_count == 1

    def test_and_laziness(self):
        request = factory.get('/1', format='json')
        request.user = AnonymousUser()

        with mock.patch.object(permissions.AllowAny, 'has_permission', return_value=True) as mock_allow:
            with mock.patch.object(permissions.IsAuthenticated, 'has_permission', return_value=False) as mock_deny:
                composed_perm = (permissions.AllowAny & permissions.IsAuthenticated)
                hasperm = composed_perm().has_permission(request, None)
                assert hasperm is False
                assert mock_allow.call_count == 1
                assert mock_deny.call_count == 1

        with mock.patch.object(permissions.AllowAny, 'has_permission', return_value=True) as mock_allow:
            with mock.patch.object(permissions.IsAuthenticated, 'has_permission', return_value=False) as mock_deny:
                composed_perm = (permissions.IsAuthenticated & permissions.AllowAny)
                hasperm = composed_perm().has_permission(request, None)
                assert hasperm is False
                assert mock_deny.call_count == 1
                mock_allow.assert_not_called()

    def test_object_and_laziness(self):
        request = factory.get('/1', format='json')
        request.user = AnonymousUser()

        with mock.patch.object(permissions.AllowAny, 'has_object_permission', return_value=True) as mock_allow:
            with mock.patch.object(permissions.IsAuthenticated, 'has_object_permission', return_value=False) as mock_deny:
                composed_perm = (permissions.AllowAny & permissions.IsAuthenticated)
                hasperm = composed_perm().has_object_permission(request, None, None)
                assert hasperm is False
                assert mock_allow.call_count == 1
                assert mock_deny.call_count == 1

        with mock.patch.object(permissions.AllowAny, 'has_object_permission', return_value=True) as mock_allow:
            with mock.patch.object(permissions.IsAuthenticated, 'has_object_permission', return_value=False) as mock_deny:
                composed_perm = (permissions.IsAuthenticated & permissions.AllowAny)
                hasperm = composed_perm().has_object_permission(request, None, None)
                assert hasperm is False
                assert mock_deny.call_count == 1
                mock_allow.assert_not_called()

    def test_unimplemented_has_object_permission(self):
        "test for issue 6402 https://github.com/encode/django-rest-framework/issues/6402"
        request = factory.get('/1', format='json')
        request.user = AnonymousUser()

        class IsAuthenticatedUserOwner(permissions.IsAuthenticated):
            def has_object_permission(self, request, view, obj):
                return True

        composed_perm = (IsAuthenticatedUserOwner | permissions.IsAdminUser)
        hasperm = composed_perm().has_object_permission(request, None, None)
        assert hasperm is False

    def test_operand_holder_is_hashable(self):
        assert hash(permissions.IsAuthenticated & permissions.IsAdminUser)

    def test_operand_holder_hash_same_for_same_operands_and_operator(self):
        first_operand_holder = (
            permissions.IsAuthenticated & permissions.IsAdminUser
        )
        second_operand_holder = (
            permissions.IsAuthenticated & permissions.IsAdminUser
        )

        assert hash(first_operand_holder) == hash(second_operand_holder)

    def test_operand_holder_hash_differs_for_different_operands(self):
        first_operand_holder = (
            permissions.IsAuthenticated & permissions.IsAdminUser
        )
        second_operand_holder = (
            permissions.AllowAny & permissions.IsAdminUser
        )
        third_operand_holder = (
            permissions.IsAuthenticated & permissions.AllowAny
        )

        assert hash(first_operand_holder) != hash(second_operand_holder)
        assert hash(first_operand_holder) != hash(third_operand_holder)
        assert hash(second_operand_holder) != hash(third_operand_holder)

    def test_operand_holder_hash_differs_for_different_operators(self):
        first_operand_holder = (
            permissions.IsAuthenticated & permissions.IsAdminUser
        )
        second_operand_holder = (
            permissions.IsAuthenticated | permissions.IsAdminUser
        )

        assert hash(first_operand_holder) != hash(second_operand_holder)

    def test_filtering_permissions(self):
        unfiltered_permissions = [
            permissions.IsAuthenticated & permissions.IsAdminUser,
            permissions.IsAuthenticated & permissions.IsAdminUser,
            permissions.AllowAny,
        ]
        expected_permissions = [
            permissions.IsAuthenticated & permissions.IsAdminUser,
            permissions.AllowAny,
        ]

        filtered_permissions = [*dict.fromkeys(unfiltered_permissions)]

        assert filtered_permissions == expected_permissions


class RecordingPermission(permissions.BasePermission):
    """
    Permission whose two check results are configured per instance and which
    appends every check it performs to a shared, ordered call log.

    The log records tuples of (permission name, stage), where stage is either
    'view' for ``has_permission`` or 'object' for ``has_object_permission``.
    It allows tests to assert not just the final decision but also the exact
    short-circuiting call order of composed permission expressions.
    """
    log = []
    name = 'recording'
    view_result = False
    object_result = False

    def has_permission(self, request, view):
        self.log.append((self.name, 'view'))
        return self.view_result

    def has_object_permission(self, request, view, obj):
        self.log.append((self.name, 'object'))
        return self.object_result


def make_recording_perm(name, view_result, object_result):
    # Results are class attributes because OperandHolder instantiates the
    # permission classes without arguments when a composition is called.
    return type(
        'Recording_%s' % name,
        (RecordingPermission,),
        {
            'name': name,
            'view_result': view_result,
            'object_result': object_result,
        }
    )


def reset_permission_log():
    RecordingPermission.log.clear()


class ComposedPermissionContractTests(TestCase):
    """
    Independent truth-table criteria for composed permission expressions.

    Every row controls ``has_permission`` and ``has_object_permission``
    separately on each branch. The key security property exercised here is
    that a view-level allow on one branch must never be combined with an
    object-level allow on another branch to produce an object-level allow
    (cross-branch privilege escalation).
    """

    def setUp(self):
        reset_permission_log()
        self.request = factory.get('/1', format='json')
        self.request.user = self.client
        self.obj = object()

    def _check(self, composed):
        instance = composed()
        return (
            instance.has_permission(self.request, None),
            instance.has_object_permission(self.request, None, self.obj),
        )

    def test_and_requires_both_stages_on_both_branches(self):
        a = make_recording_perm('a', True, True)
        b = make_recording_perm('b', True, True)
        self.assertEqual(self._check(a & b), (True, True))

        a = make_recording_perm('a', True, True)
        b = make_recording_perm('b', True, False)
        self.assertEqual(self._check(a & b), (True, False))

        a = make_recording_perm('a', False, True)
        b = make_recording_perm('b', True, True)
        # The view stage denies, but the object stage is an independent
        # AND of the two object checks, both of which pass here.
        self.assertEqual(self._check(a & b), (False, True))

    def test_and_object_stage_evaluates_each_branch_independently(self):
        # Branch a passes the view stage but fails the object stage;
        # branch b passes the object stage. AND must still deny the object.
        a = make_recording_perm('a', True, False)
        b = make_recording_perm('b', True, True)
        self.assertEqual(self._check(a & b), (True, False))

    def test_or_view_stage_allows_if_either_branch_allows(self):
        a = make_recording_perm('a', True, False)
        b = make_recording_perm('b', False, True)
        view_allowed, _ = self._check(a | b)
        self.assertIs(view_allowed, True)

        a = make_recording_perm('a', False, True)
        b = make_recording_perm('b', False, False)
        self.assertEqual(self._check(a | b), (False, False))

    def test_or_object_stage_rejects_cross_branch_stitching(self):
        """
        Security counterexample: branch a only passes the view check and
        branch b only passes the object check. A naive ``or`` of the two
        object checks would allow the object even though no single branch
        passes both stages for this request.
        """
        a = make_recording_perm('a', True, False)
        b = make_recording_perm('b', False, True)
        _, object_allowed = self._check(a | b)
        self.assertIs(object_allowed, False)

    def test_or_object_stage_allows_whole_branch_that_passes_both_stages(self):
        a = make_recording_perm('a', True, False)
        b = make_recording_perm('b', True, True)
        self.assertEqual(self._check(a | b), (True, True))

    def test_or_object_stage_evaluates_each_branch_independently(self):
        # Branch b only passes the object check, not the view check, so it
        # cannot rescue a branch a that only passes the view check.
        a = make_recording_perm('a', True, False)
        b = make_recording_perm('b', False, True)
        _, object_allowed = self._check(a | b)
        self.assertIs(object_allowed, False)

    def test_not_inverts_each_stage_independently(self):
        a = make_recording_perm('a', False, False)
        self.assertEqual(self._check(~a), (True, True))

        a = make_recording_perm('a', True, False)
        self.assertEqual(self._check(~a), (False, True))

        a = make_recording_perm('a', False, True)
        self.assertEqual(self._check(~a), (True, False))

    def test_nested_and_of_ors_keeps_stages_within_each_branch(self):
        # (a | b) & (c | d): view allows everywhere, but the object-level
        # allow on the second OR is supplied by a different branch than the
        # one that passes its view check. Each OR must still deny the object.
        a = make_recording_perm('a', True, False)
        b = make_recording_perm('b', False, False)
        c = make_recording_perm('c', True, False)
        d = make_recording_perm('d', False, True)
        self.assertEqual(self._check((a | b) & (c | d)), (True, False))

    def test_nested_or_of_ands_requires_one_complete_branch(self):
        # (a & b) | (c & d): the second AND passes neither stage, the first
        # passes both; the whole expression must allow both stages.
        a = make_recording_perm('a', True, True)
        b = make_recording_perm('b', True, True)
        c = make_recording_perm('c', False, True)
        d = make_recording_perm('d', True, False)
        self.assertEqual(self._check((a & b) | (c & d)), (True, True))

    def test_nested_negation_does_not_stitch_stages(self):
        # (~a) | b: a passes only the object stage, so ~a passes only the
        # view stage; b passes only the object stage. No branch passes both
        # stages, hence the composed object check must deny.
        a = make_recording_perm('a', False, True)
        b = make_recording_perm('b', False, True)
        self.assertEqual(self._check(~a | b), (True, False))

    def test_and_short_circuit_call_order_view_stage(self):
        reset_permission_log()
        a = make_recording_perm('a', False, True)
        b = make_recording_perm('b', True, True)
        view_allowed = (a & b)().has_permission(self.request, None)
        self.assertIs(view_allowed, False)
        self.assertEqual(RecordingPermission.log, [('a', 'view')])

    def test_and_short_circuit_call_order_object_stage(self):
        reset_permission_log()
        a = make_recording_perm('a', True, False)
        b = make_recording_perm('b', True, True)
        object_allowed = (a & b)().has_object_permission(
            self.request, None, self.obj
        )
        self.assertIs(object_allowed, False)
        self.assertEqual(RecordingPermission.log, [('a', 'object')])

    def test_or_short_circuit_call_order_view_stage(self):
        reset_permission_log()
        a = make_recording_perm('a', True, False)
        b = make_recording_perm('b', False, True)
        view_allowed = (a | b)().has_permission(self.request, None)
        self.assertIs(view_allowed, True)
        self.assertEqual(RecordingPermission.log, [('a', 'view')])

    def test_or_object_stage_rechecks_view_checks_in_order(self):
        """
        The OR object stage re-runs each branch's view check before its
        object check, so a branch that fails the view stage never reaches
        its object check even inside the object stage.
        """
        reset_permission_log()
        a = make_recording_perm('a', False, True)
        b = make_recording_perm('b', True, True)
        object_allowed = (a | b)().has_object_permission(
            self.request, None, self.obj
        )
        self.assertIs(object_allowed, True)
        self.assertEqual(
            RecordingPermission.log,
            [('a', 'view'), ('b', 'view'), ('b', 'object')],
        )

    def test_or_object_stage_short_circuits_on_first_complete_branch(self):
        reset_permission_log()
        a = make_recording_perm('a', True, True)
        b = make_recording_perm('b', True, True)
        object_allowed = (a | b)().has_object_permission(
            self.request, None, self.obj
        )
        self.assertIs(object_allowed, True)
        self.assertEqual(
            RecordingPermission.log,
            [('a', 'view'), ('a', 'object')],
        )


class OwnedModel(models.Model):
    text = models.CharField(max_length=100)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, null=True)

    class Meta:
        app_label = 'tests'


class OwnedModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = OwnedModel
        fields = '__all__'


class RecordingStaffPermission(permissions.BasePermission):
    """
    Admin branch: passes the view check for staff users only.
    The object check is intentionally not overridden, so it keeps the
    ``BasePermission`` default of ``True``. Every check is recorded.
    """
    log = []

    def has_permission(self, request, view):
        self.log.append(('staff', 'view'))
        return bool(request.user and request.user.is_staff)


class RecordingOwnerPermission(permissions.BasePermission):
    """
    Object-access branch: any authenticated user passes the view check,
    and only the object's owner passes the object check.
    """
    log = []

    def has_permission(self, request, view):
        self.log.append(('owner', 'view'))
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        self.log.append(('owner', 'object'))
        return obj.owner_id == getattr(request.user, 'id', None)


def reset_recording_perm_logs():
    RecordingStaffPermission.log.clear()
    RecordingOwnerPermission.log.clear()


def recording_perm_log():
    return RecordingStaffPermission.log + RecordingOwnerPermission.log


class ComposedObjectDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = OwnedModel.objects.all()
    serializer_class = OwnedModelSerializer
    permission_classes = [RecordingStaffPermission | RecordingOwnerPermission]

    def perform_update(self, serializer):
        PermissionCompositionIntegrationTests.action_log.append('update')
        super().perform_update(serializer)


class LegacyOR(permissions.OR):
    """
    Reconstructs the historical OR implementation whose object stage OR-ed
    the two object checks without re-running each branch's view check.
    It represents the vulnerable semantics these tests guard against.
    """

    def has_object_permission(self, request, view, obj):
        return (
            self.op1.has_object_permission(request, view, obj) or
            self.op2.has_object_permission(request, view, obj)
        )


class LegacyStaffOrOwner:
    """
    Holder mirroring OperandHolder: views instantiate permission classes
    without arguments, so build the legacy composition inside __new__.
    """

    def __new__(cls, *args, **kwargs):
        return LegacyOR(
            RecordingStaffPermission(), RecordingOwnerPermission()
        )


class LegacyComposedObjectDetailView(ComposedObjectDetailView):
    permission_classes = [LegacyStaffOrOwner]


composed_object_detail_view = ComposedObjectDetailView.as_view()
legacy_composed_object_detail_view = LegacyComposedObjectDetailView.as_view()


class ComposedObjectListView(generics.ListAPIView):
    queryset = OwnedModel.objects.all()
    serializer_class = OwnedModelSerializer
    permission_classes = [RecordingStaffPermission | RecordingOwnerPermission]


composed_object_list_view = ComposedObjectListView.as_view()


class PermissionCompositionIntegrationTests(TestCase):
    """
    End-to-end tests driving a real GenericAPIView: a request passes the
    view-level permission check, then ``get_object`` runs the object-level
    check. Assertions observe whether the business action (the update) is
    actually performed, not just the composed permission in isolation.
    """
    action_log = []

    def setUp(self):
        reset_recording_perm_logs()
        self.action_log.clear()
        self.admin = User.objects.create_user(
            'admin', 'admin@example.com', 'password', is_staff=True
        )
        self.owner = User.objects.create_user(
            'owner', 'owner@example.com', 'password'
        )
        self.outsider = User.objects.create_user(
            'outsider', 'outsider@example.com', 'password'
        )
        self.owned = OwnedModel.objects.create(text='original', owner=self.owner)
        self.payload = {'text': 'changed', 'owner': self.owner.pk}

    def _request(self, view, user):
        request = factory.patch(
            '/%d' % self.owned.pk, self.payload, format='json'
        )
        if user is not None:
            request.user = user
        return view(request, pk=self.owned.pk)

    def test_admin_access_is_allowed_and_action_runs(self):
        response = self._request(composed_object_detail_view, self.admin)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.owned.refresh_from_db()
        self.assertEqual(self.owned.text, 'changed')
        self.assertEqual(self.action_log, ['update'])

    def test_owner_access_is_allowed_and_action_runs(self):
        response = self._request(composed_object_detail_view, self.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.owned.refresh_from_db()
        self.assertEqual(self.owned.text, 'changed')
        self.assertEqual(self.action_log, ['update'])

    def test_outsider_is_denied_and_action_does_not_run(self):
        response = self._request(composed_object_detail_view, self.outsider)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.owned.refresh_from_db()
        self.assertEqual(self.owned.text, 'original')
        self.assertEqual(self.action_log, [])

    def test_anonymous_is_denied_and_action_does_not_run(self):
        response = self._request(
            composed_object_detail_view, AnonymousUser()
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.owned.refresh_from_db()
        self.assertEqual(self.owned.text, 'original')
        self.assertEqual(self.action_log, [])

    def test_object_stage_call_order_passes_view_check_of_each_branch(self):
        reset_recording_perm_logs()
        self._request(composed_object_detail_view, self.owner)
        # The view stage checks both branches left to right; the object
        # stage re-runs each branch's view check before its object check.
        # The two recording classes keep separate logs, so merge each log
        # in its own chronological order.
        self.assertEqual(
            RecordingStaffPermission.log,
            [('staff', 'view'), ('staff', 'view')],
        )
        self.assertEqual(
            RecordingOwnerPermission.log,
            [('owner', 'view'), ('owner', 'view'), ('owner', 'object')],
        )

    def test_legacy_or_semantics_leak_the_outsider_through_object_stage(self):
        """
        Regression counterexample under the historical OR semantics: the
        outsider passes the owner branch's view check while the admin
        branch's default object check returns ``True``. The vulnerable
        object-stage OR stitches those two halves together and lets a
        request through that must not be allowed.
        """
        response = self._request(
            legacy_composed_object_detail_view, self.outsider
        )
        self.assertEqual(
            response.status_code, status.HTTP_200_OK,
            'legacy OR semantics wrongly allowed the outsider to update '
            'an object they do not own',
        )
        self.owned.refresh_from_db()
        self.assertEqual(self.owned.text, 'changed')
        self.assertEqual(self.action_log, ['update'])

    def test_removing_one_or_view_check_releases_a_denied_request(self):
        """
        Mutation test: temporarily remove one view check from the OR object
        stage (the re-evaluation of the admin branch's has_permission).
        The outsider's request, denied by the real view above, is then
        allowed because the admin branch's default object check returns
        True without proving the request passed its view check.
        """
        original = permissions.OR.has_object_permission
        self.addCleanup(
            setattr, permissions.OR, 'has_object_permission', original
        )

        def mutated_has_object_permission(self, request, view, obj):
            return (
                self.op1.has_object_permission(request, view, obj)
            ) or (
                self.op2.has_permission(request, view)
                and self.op2.has_object_permission(request, view, obj)
            )

        with mock.patch.object(
            permissions.OR,
            'has_object_permission',
            mutated_has_object_permission,
        ):
            response = self._request(
                composed_object_detail_view, self.outsider
            )

        self.assertEqual(
            response.status_code, status.HTTP_200_OK,
            'removing the admin branch view check from the OR object '
            'stage must surface as the outsider being wrongly allowed',
        )

    def test_list_endpoint_does_not_run_object_permissions(self):
        """
        Contract boundary: a list view only runs view-level permission
        checks. It does not call has_object_permission for each row, so
        objects the requesting user cannot access individually are still
        present in the response. Per-object list filtering must be done in
        the queryset (or via a filter backend), not assumed from the
        composed permission classes.
        """
        reset_recording_perm_logs()
        request = factory.get('/')
        request.user = self.outsider
        response = composed_object_list_view(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['id'], self.owned.pk)
        self.assertNotIn(('owner', 'object'), recording_perm_log())
        self.assertNotIn(('staff', 'object'), recording_perm_log())

    def test_list_endpoint_still_enforces_view_level_permission(self):
        request = factory.get('/')
        request.user = AnonymousUser()
        response = composed_object_list_view(request)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
