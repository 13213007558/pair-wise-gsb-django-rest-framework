import io
import math
import os

import pytest
from django import forms
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.uploadhandler import (
    MemoryFileUploadHandler, TemporaryFileUploadHandler
)
from django.http.request import RawPostDataException
from django.test import TestCase, override_settings
from django.urls import path
from django.utils.datastructures import MultiValueDict

from rest_framework import serializers
from rest_framework.exceptions import ParseError
from rest_framework.parsers import (
    FileUploadParser, FormParser, JSONParser, MultiPartParser,
    NestedJSONMultiPartParser
)
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIClient, APIRequestFactory
from rest_framework.utils import json
from rest_framework.views import APIView


class Form(forms.Form):
    field1 = forms.CharField(max_length=3)
    field2 = forms.CharField()


class TestFormParser(TestCase):
    def setUp(self):
        self.string = "field1=abc&field2=defghijk"

    def test_parse(self):
        """ Make sure the `QueryDict` works OK """
        parser = FormParser()

        stream = io.StringIO(self.string)
        data = parser.parse(stream)

        assert Form(data).is_valid() is True


class TestFileUploadParser(TestCase):
    def setUp(self):
        class MockRequest:
            pass
        self.stream = io.BytesIO(b"Test text file")
        request = MockRequest()
        request.upload_handlers = (MemoryFileUploadHandler(),)
        request.META = {
            'HTTP_CONTENT_DISPOSITION': 'Content-Disposition: inline; filename=file.txt',
            'HTTP_CONTENT_LENGTH': 14,
        }
        self.parser_context = {'request': request, 'kwargs': {}}

    def test_parse(self):
        """
        Parse raw file upload.
        """
        parser = FileUploadParser()
        self.stream.seek(0)
        data_and_files = parser.parse(self.stream, None, self.parser_context)
        file_obj = data_and_files.files['file']
        assert file_obj.size == 14

    def test_parse_missing_filename(self):
        """
        Parse raw file upload when filename is missing.
        """
        parser = FileUploadParser()
        self.stream.seek(0)
        self.parser_context['request'].META['HTTP_CONTENT_DISPOSITION'] = ''
        with pytest.raises(ParseError) as excinfo:
            parser.parse(self.stream, None, self.parser_context)
        assert str(excinfo.value) == 'Missing filename. Request should include a Content-Disposition header with a filename parameter.'

    def test_parse_missing_filename_multiple_upload_handlers(self):
        """
        Parse raw file upload with multiple handlers when filename is missing.
        Regression test for #2109.
        """
        parser = FileUploadParser()
        self.stream.seek(0)
        self.parser_context['request'].upload_handlers = (
            MemoryFileUploadHandler(),
            MemoryFileUploadHandler()
        )
        self.parser_context['request'].META['HTTP_CONTENT_DISPOSITION'] = ''
        with pytest.raises(ParseError) as excinfo:
            parser.parse(self.stream, None, self.parser_context)
        assert str(excinfo.value) == 'Missing filename. Request should include a Content-Disposition header with a filename parameter.'

    def test_parse_missing_filename_large_file(self):
        """
        Parse raw file upload when filename is missing with TemporaryFileUploadHandler.
        """
        parser = FileUploadParser()
        self.stream.seek(0)
        self.parser_context['request'].upload_handlers = (
            TemporaryFileUploadHandler(),
        )
        self.parser_context['request'].META['HTTP_CONTENT_DISPOSITION'] = ''
        with pytest.raises(ParseError) as excinfo:
            parser.parse(self.stream, None, self.parser_context)
        assert str(excinfo.value) == 'Missing filename. Request should include a Content-Disposition header with a filename parameter.'

    def test_get_filename(self):
        parser = FileUploadParser()
        filename = parser.get_filename(self.stream, None, self.parser_context)
        assert filename == 'file.txt'

    def test_get_encoded_filename(self):
        parser = FileUploadParser()

        self.__replace_content_disposition('inline; filename*=utf-8\'\'ÀĥƦ.txt')
        filename = parser.get_filename(self.stream, None, self.parser_context)
        assert filename == 'ÀĥƦ.txt'

        self.__replace_content_disposition('inline; filename=fallback.txt; filename*=utf-8\'\'ÀĥƦ.txt')
        filename = parser.get_filename(self.stream, None, self.parser_context)
        assert filename == 'ÀĥƦ.txt'

        self.__replace_content_disposition('inline; filename=fallback.txt; filename*=utf-8\'en-us\'ÀĥƦ.txt')
        filename = parser.get_filename(self.stream, None, self.parser_context)
        assert filename == 'ÀĥƦ.txt'

    def __replace_content_disposition(self, disposition):
        self.parser_context['request'].META['HTTP_CONTENT_DISPOSITION'] = disposition


class TestJSONParser(TestCase):
    def bytes(self, value):
        return io.BytesIO(value.encode())

    def test_float_strictness(self):
        parser = JSONParser()

        # Default to strict
        for value in ['Infinity', '-Infinity', 'NaN']:
            with pytest.raises(ParseError):
                parser.parse(self.bytes(value))

        parser.strict = False
        assert parser.parse(self.bytes('Infinity')) == float('inf')
        assert parser.parse(self.bytes('-Infinity')) == float('-inf')
        assert math.isnan(parser.parse(self.bytes('NaN')))


class TestPOSTAccessed(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def test_post_accessed_in_post_method(self):
        django_request = self.factory.post('/', {'foo': 'bar'})
        request = Request(django_request, parsers=[FormParser(), MultiPartParser()])
        django_request.POST
        assert request.POST == {'foo': ['bar']}
        assert request.data == {'foo': ['bar']}

    def test_post_accessed_in_post_method_with_json_parser(self):
        django_request = self.factory.post('/', {'foo': 'bar'})
        request = Request(django_request, parsers=[JSONParser()])
        django_request.POST
        assert request.POST == {}
        assert request.data == {}

    def test_post_accessed_in_put_method(self):
        django_request = self.factory.put('/', {'foo': 'bar'})
        request = Request(django_request, parsers=[FormParser(), MultiPartParser()])
        django_request.POST
        assert request.POST == {'foo': ['bar']}
        assert request.data == {'foo': ['bar']}

    def test_request_read_before_parsing(self):
        django_request = self.factory.put('/', {'foo': 'bar'})
        request = Request(django_request, parsers=[FormParser(), MultiPartParser()])
        django_request.read()
        with pytest.raises(RawPostDataException):
            request.POST
        with pytest.raises(RawPostDataException):
            request.POST
            request.data


class NestedProfileSerializer(serializers.Serializer):
    avatar = serializers.FileField()


class NestedDocumentSerializer(serializers.Serializer):
    title = serializers.CharField()
    profile = NestedProfileSerializer()
    attachments = serializers.ListField(child=serializers.FileField())


def summarize_files(value):
    if isinstance(value, dict):
        return {key: summarize_files(item) for key, item in value.items()}
    if isinstance(value, list):
        return [summarize_files(item) for item in value]
    if hasattr(value, 'read') and hasattr(value, 'name'):
        return {'filename': value.name, 'size': value.size}
    return value


class NestedUploadView(APIView):
    parser_classes = [NestedJSONMultiPartParser]

    def post(self, request):
        serializer = NestedDocumentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        avatar = serializer.validated_data['profile']['avatar']
        attachment = serializer.validated_data['attachments'][0]
        return Response({
            'title': serializer.validated_data['title'],
            'avatar': avatar.name,
            'attachment': attachment.name,
            'same_file': avatar is attachment,
        })


class EchoNestedDataView(APIView):
    parser_classes = [NestedJSONMultiPartParser]

    def post(self, request):
        return Response(summarize_files(request.data))


class RecordingTemporaryFileUploadHandler(TemporaryFileUploadHandler):
    instances = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.created = []
        RecordingTemporaryFileUploadHandler.instances.append(self)

    def file_complete(self, content_length):
        uploaded_file = super().file_complete(content_length)
        if uploaded_file is not None:
            self.created.append(uploaded_file)
        return uploaded_file


urlpatterns = [
    path('nested-upload/', NestedUploadView.as_view()),
    path('nested-echo/', EchoNestedDataView.as_view()),
]


@override_settings(ROOT_URLCONF='tests.test_parsers')
class TestNestedJSONMultiPartParser(TestCase):
    def setUp(self):
        self.api_client = APIClient()

    def operations(self, **overrides):
        operations = {
            'title': 'report',
            'profile': {'avatar': None},
            'attachments': [None],
        }
        operations.update(overrides)
        return operations

    def post(self, operations, file_map, files=None, view='/nested-upload/'):
        data = {
            'operations': json.dumps(operations),
            'map': json.dumps(file_map),
        }
        data.update(files or {})
        return self.api_client.post(view, data=data, format='multipart')

    def test_file_is_uploaded_once_and_placed_at_each_mapped_location(self):
        response = self.post(
            self.operations(),
            {'photo': ['/profile/avatar', '/attachments/0']},
            {'photo': SimpleUploadedFile(
                'avatar.png', b'fake-image', content_type='image/png')},
        )
        assert response.status_code == 200
        assert response.data == {
            'title': 'report',
            'avatar': 'avatar.png',
            'attachment': 'avatar.png',
            'same_file': True,
        }

    def test_json_pointer_escaping(self):
        operations = {'a~b': {'c/d': None}, 'tilde~1key': None}
        response = self.post(
            operations,
            {'file': ['/a~0b/c~1d', '/tilde~01key']},
            {'file': SimpleUploadedFile('x.txt', b'hello')},
            view='/nested-echo/',
        )
        assert response.status_code == 200
        assert response.data == {
            'a~b': {'c/d': {'filename': 'x.txt', 'size': 5}},
            'tilde~1key': {'filename': 'x.txt', 'size': 5},
        }

    def test_file_field_names_are_not_treated_as_paths(self):
        response = self.post(
            {'doc': {'file': None}},
            {'my.file/weird': ['/doc/file']},
            {'my.file/weird': SimpleUploadedFile('a.txt', b'1')},
            view='/nested-echo/',
        )
        assert response.status_code == 200
        assert response.data == {
            'doc': {'file': {'filename': 'a.txt', 'size': 1}},
        }

    def test_single_pointer_string_is_accepted(self):
        response = self.post(
            {'doc': {'file': None}},
            {'photo': '/doc/file'},
            {'photo': SimpleUploadedFile('a.txt', b'1')},
            view='/nested-echo/',
        )
        assert response.status_code == 200
        assert response.data == {
            'doc': {'file': {'filename': 'a.txt', 'size': 1}},
        }

    def test_array_hole_is_not_filled(self):
        response = self.post(
            self.operations(),
            {'photo': ['/attachments/1']},
            {'photo': SimpleUploadedFile('a.txt', b'1')},
        )
        assert response.status_code == 400
        assert '/attachments/1' in response.data['detail']

    def test_missing_parents_are_not_created(self):
        response = self.post(
            self.operations(),
            {'photo': ['/profile/nested/avatar']},
            {'photo': SimpleUploadedFile('a.txt', b'1')},
        )
        assert response.status_code == 400
        assert '/profile/nested/avatar' in response.data['detail']

    def test_pointer_to_non_null_value_is_rejected(self):
        response = self.post(
            self.operations(),
            {'photo': ['/title']},
            {'photo': SimpleUploadedFile('a.txt', b'1')},
        )
        assert response.status_code == 400
        assert '/title' in response.data['detail']

    def test_multiple_files_mapped_to_same_location_are_rejected(self):
        response = self.post(
            self.operations(),
            {'a': ['/profile/avatar'], 'b': ['/profile/avatar']},
            {
                'a': SimpleUploadedFile('a.txt', b'1'),
                'b': SimpleUploadedFile('b.txt', b'2'),
            },
        )
        assert response.status_code == 400
        assert '/profile/avatar' in response.data['detail']

    def test_map_referencing_missing_file_is_rejected(self):
        response = self.post(
            self.operations(),
            {'ghost': ['/profile/avatar']},
            {'photo': SimpleUploadedFile('a.txt', b'1')},
        )
        assert response.status_code == 400
        assert 'ghost' in response.data['detail']

    def test_unreferenced_file_is_rejected(self):
        response = self.post(
            self.operations(),
            {'photo': ['/profile/avatar']},
            {
                'photo': SimpleUploadedFile('a.txt', b'1'),
                'extra': SimpleUploadedFile('b.txt', b'2'),
            },
        )
        assert response.status_code == 400
        assert 'extra' in response.data['detail']

    def test_operations_must_be_a_json_object(self):
        response = self.post([None], {'photo': ['/0']},
                             {'photo': SimpleUploadedFile('a.txt', b'1')})
        assert response.status_code == 400
        assert 'operations' in response.data['detail']

    def test_invalid_operations_json_is_rejected(self):
        response = self.api_client.post('/nested-echo/', data={
            'operations': '{invalid',
            'map': '{}',
        }, format='multipart')
        assert response.status_code == 400
        assert 'operations' in response.data['detail']

    def test_missing_control_parts_are_rejected(self):
        response = self.api_client.post(
            '/nested-echo/', data={}, format='multipart')
        assert response.status_code == 400
        assert 'operations' in response.data['detail']

    def test_invalid_map_entry_is_rejected(self):
        response = self.post(
            self.operations(),
            {'photo': 3},
            {'photo': SimpleUploadedFile('a.txt', b'1')},
        )
        assert response.status_code == 400
        assert 'photo' in response.data['detail']

    def test_duplicate_control_parts_are_rejected(self):
        body = (
            b'--BoUnDaRy\r\n'
            b'Content-Disposition: form-data; name="operations"\r\n\r\n'
            b'{"title": "x"}\r\n'
            b'--BoUnDaRy\r\n'
            b'Content-Disposition: form-data; name="operations"\r\n\r\n'
            b'{"title": "y"}\r\n'
            b'--BoUnDaRy\r\n'
            b'Content-Disposition: form-data; name="map"\r\n\r\n'
            b'{}\r\n'
            b'--BoUnDaRy--\r\n'
        )
        response = self.api_client.post(
            '/nested-echo/', data=body,
            content_type='multipart/form-data; boundary=BoUnDaRy')
        assert response.status_code == 400
        assert 'operations' in response.data['detail']

    @override_settings(FILE_UPLOAD_HANDLERS=[
        'tests.test_parsers.RecordingTemporaryFileUploadHandler'])
    def test_uploaded_files_are_closed_when_mapping_fails(self):
        RecordingTemporaryFileUploadHandler.instances.clear()
        response = self.post(
            self.operations(),
            {'photo': ['/profile/avatar', '/missing/deep']},
            {'photo': SimpleUploadedFile('avatar.png', b'x' * 100)},
        )
        assert response.status_code == 400
        created = [
            uploaded_file
            for handler in RecordingTemporaryFileUploadHandler.instances
            for uploaded_file in handler.created
        ]
        assert len(created) == 1
        assert created[0].closed
        assert not os.path.exists(created[0].temporary_file_path())

    def test_failed_mapping_does_not_modify_operations(self):
        parser = NestedJSONMultiPartParser()
        operations = {'a': None, 'b': {'c': 1}}
        files = MultiValueDict({
            'f': [SimpleUploadedFile('f.txt', b'x')],
        })
        file_map = {'f': ['/a', '/b/c']}
        with pytest.raises(ParseError):
            parser.resolve_placements(operations, file_map, files)
        assert operations == {'a': None, 'b': {'c': 1}}
