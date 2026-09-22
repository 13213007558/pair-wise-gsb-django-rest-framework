import io
import json
import math
import os

import pytest
from django import forms
from django.core.files.uploadhandler import (
    MemoryFileUploadHandler, TemporaryFileUploadHandler
)
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import QueryDict
from django.http.request import RawPostDataException
from django.test import TestCase, override_settings
from django.test.client import MULTIPART_CONTENT, encode_multipart
from django.urls import path
from django.utils.datastructures import MultiValueDict

from rest_framework import serializers, status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.exceptions import ParseError
from rest_framework.parsers import (
    FileUploadParser, FormParser, JSONParser, MultiPartJSONParser,
    MultiPartParser
)
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIClient, APIRequestFactory


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


class ProfileSerializer(serializers.Serializer):
    avatar = serializers.FileField()


class NestedUploadSerializer(serializers.Serializer):
    profile = ProfileSerializer()
    attachments = serializers.ListField(child=serializers.FileField())


@api_view(['POST'])
@parser_classes((MultiPartJSONParser,))
def nested_upload_view(request):
    serializer = NestedUploadSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    avatar = serializer.validated_data['profile']['avatar']
    attachments = serializer.validated_data['attachments']
    return Response({
        'same_file': avatar is attachments[0],
        'file_count': len(request.FILES),
        'filename': avatar.name,
        'size': avatar.size,
    }, status=status.HTTP_200_OK)


urlpatterns = [
    path('nested-upload/', nested_upload_view),
]


OPERATIONS = {
    'profile': {'avatar': None},
    'attachments': [None],
}


class TrackingTemporaryFileUploadHandler(TemporaryFileUploadHandler):
    created_paths = []

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self.created_paths.append(self.file.temporary_file_path())


class TestMultiPartJSONParser(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.client = APIClient()
        self.boundary = 'BoUnDaRyStRiNg'

    def parse(self, parts):
        content = encode_multipart(self.boundary, parts)
        content_type = '%s; boundary=%s' % (MULTIPART_CONTENT, self.boundary)
        request = Request(self.factory.post(
            '/', content, content_type=content_type
        ))
        request.parsers = (MultiPartJSONParser(),)
        return request

    def valid_parts(self, **overrides):
        parts = {
            'operations': json.dumps(OPERATIONS),
            'map': json.dumps({
                'photo': ['/profile/avatar', '/attachments/0'],
            }),
            'photo': SimpleUploadedFile('dot.name.txt', b'file content'),
        }
        parts.update(overrides)
        return parts

    def test_maps_one_file_to_multiple_json_pointers(self):
        request = self.parse(self.valid_parts())

        data = request.data
        assert data['profile']['avatar'] is data['attachments'][0]
        assert data['profile']['avatar'] is request.FILES['photo']
        assert 'photo' not in data
        assert data['profile']['avatar'].name == 'dot.name.txt'
        assert data['profile']['avatar'].size == 12

    def test_supports_escaped_pointers_and_array_indexes(self):
        operations = {'a/b~c': [None]}
        request = self.parse({
            'operations': json.dumps(operations),
            'map': json.dumps({'file': ['/a~1b~0c/0']}),
            'file': io.BytesIO(b'content'),
        })

        assert request.data['a/b~c'][0] is request.FILES['file']

    @override_settings(ROOT_URLCONF='tests.test_parsers')
    def test_nested_serializer_validates_real_apiclient_upload(self):
        response = self.client.post('/nested-upload/', self.valid_parts())

        assert response.status_code == status.HTTP_200_OK
        assert response.data == {
            'same_file': True,
            'file_count': 1,
            'filename': 'dot.name.txt',
            'size': 12,
        }

    @override_settings(ROOT_URLCONF='tests.test_parsers')
    def test_slashes_and_dots_in_filename_are_not_field_paths(self):
        operations = json.dumps(OPERATIONS)
        mapping = json.dumps({
            'photo': ['/profile/avatar', '/attachments/0'],
        })
        content = (
            '--%s\r\n'
            'Content-Disposition: form-data; name="operations"\r\n'
            '\r\n%s\r\n'
            '--%s\r\n'
            'Content-Disposition: form-data; name="map"\r\n'
            '\r\n%s\r\n'
            '--%s\r\n'
            'Content-Disposition: form-data; name="photo"; '
            'filename="directory/photo.dot.txt"\r\n'
            'Content-Type: application/octet-stream\r\n'
            '\r\nfile content\r\n'
            '--%s--\r\n'
        ) % (
            self.boundary, operations,
            self.boundary, mapping,
            self.boundary,
            self.boundary,
        )
        content_type = '%s; boundary=%s' % (MULTIPART_CONTENT, self.boundary)
        response = self.client.post(
            '/nested-upload/',
            data=content.encode(),
            content_type=content_type,
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data['same_file'] is True
        assert response.data['filename'] == 'photo.dot.txt'

    def invalid_request(self, **overrides):
        parts = self.valid_parts()
        parts.update(overrides)
        request = self.parse(parts)
        with pytest.raises(ParseError) as exc_info:
            request.data
        return exc_info.value

    def test_duplicate_missing_and_unexpected_control_parts(self):
        assert "duplicate 'operations'" in str(self.invalid_request(
            operations=[json.dumps(OPERATIONS), json.dumps(OPERATIONS)],
        ))
        assert "duplicate 'map'" in str(self.invalid_request(
            map=[json.dumps({
                'photo': ['/profile/avatar', '/attachments/0'],
            }), json.dumps({
                'photo': ['/profile/avatar', '/attachments/0'],
            })],
        ))
        parts = self.valid_parts()
        del parts['map']
        request = self.parse(parts)
        with pytest.raises(ParseError) as exc_info:
            request.data
        assert "missing 'map'" in str(exc_info.value)
        parts = self.valid_parts()
        del parts['operations']
        request = self.parse(parts)
        with pytest.raises(ParseError) as exc_info:
            request.data
        assert "missing 'operations'" in str(exc_info.value)
        assert 'unexpected part' in str(self.invalid_request(unexpected='value'))

    def test_invalid_control_json_and_values(self):
        assert 'invalid JSON' in str(self.invalid_request(operations='{'))
        assert "'operations' must be a JSON object" in str(
            self.invalid_request(operations='[]')
        )
        assert 'invalid JSON' in str(self.invalid_request(map='{'))
        assert "'map' must be a JSON object" in str(
            self.invalid_request(map='[]')
        )
        assert 'non-empty array' in str(self.invalid_request(
            map=json.dumps({'photo': '/profile/avatar'}),
        ))
        assert 'non-empty array' in str(self.invalid_request(
            map=json.dumps({'photo': []}),
        ))
        assert 'JSON Pointer string' in str(self.invalid_request(
            map=json.dumps({'photo': [0]}),
        ))

    def test_duplicate_pointer_and_non_null_target(self):
        operations = {'first': None, 'second': None}
        error = self.invalid_request(
            operations=json.dumps(operations),
            map=json.dumps({
                'photo': ['/first'],
                'other': ['/first'],
            }),
            other=SimpleUploadedFile('other.txt', b'other'),
        )
        assert 'already mapped' in str(error)
        assert "map['other'][0]" in str(error)
        assert "map['photo'][0]" in str(error)
        assert 'non-null' in str(self.invalid_request(
            operations=json.dumps({
                'profile': {'avatar': {}},
                'attachments': [None],
            }),
        ))

    def test_missing_and_invalid_pointer_targets(self):
        assert 'outside the existing array' in str(self.invalid_request(
            operations=json.dumps({
                'profile': {'avatar': None},
                'attachments': [],
            }),
        ))
        assert 'outside the existing array' in str(self.invalid_request(
            operations=json.dumps({
                'profile': {'avatar': None},
                'attachments': [None],
            }),
            map=json.dumps({
                'photo': ['/profile/avatar', '/attachments/1'],
            }),
        ))
        assert 'missing object key' in str(self.invalid_request(
            operations=json.dumps({
                'profile': {},
                'attachments': [None],
            }),
        ))
        assert 'non-container value' in str(self.invalid_request(
            operations=json.dumps({
                'profile': {'avatar': None, 'x': 1},
                'attachments': [None],
            }),
            map=json.dumps({
                'photo': ['/profile/avatar', '/profile/x/y'],
            }),
        ))
        assert 'invalid JSON Pointer' in str(self.invalid_request(
            map=json.dumps({'photo': ['profile/avatar']}),
        ))
        assert 'invalid JSON Pointer' in str(self.invalid_request(
            map=json.dumps({'photo': ['/profile~2/avatar']}),
        ))
        assert 'missing array index' in str(self.invalid_request(
            map=json.dumps({'photo': ['/attachments/-']}),
        ))
        assert 'missing array index' in str(self.invalid_request(
            map=json.dumps({'photo': ['/attachments/01']}),
        ))
        assert 'not a null value' in str(self.invalid_request(
            map=json.dumps({'photo': ['']}),
        ))

    def test_missing_referenced_file_and_unreferenced_file(self):
        parts = self.valid_parts()
        parts['map'] = json.dumps({
            'missing': ['/profile/avatar', '/attachments/0'],
        })
        request = self.parse(parts)
        with pytest.raises(ParseError) as exc_info:
            request.data
        assert 'without an uploaded file' in str(exc_info.value)

        assert 'not referenced' in str(self.invalid_request(
            map=json.dumps({'photo': ['/profile/avatar']}),
            extra=SimpleUploadedFile('extra.txt', b'extra'),
        ))

    def test_duplicate_file_part_is_rejected(self):
        parts = self.valid_parts()
        parts['photo'] = [
            SimpleUploadedFile('first.txt', b'first'),
            SimpleUploadedFile('second.txt', b'second'),
        ]
        request = self.parse(parts)
        with pytest.raises(ParseError) as exc_info:
            request.data
        assert 'duplicate file part' in str(exc_info.value)

    def test_failure_does_not_mutate_operations(self):
        operations = {'first': None, 'second': None}
        data = QueryDict('', mutable=True)
        data['operations'] = json.dumps(operations)
        data['map'] = json.dumps({
            'photo': ['/first', '/second'],
            'other': ['/second'],
        })
        files = MultiValueDict({
            'photo': [SimpleUploadedFile('photo.txt', b'photo')],
            'other': [SimpleUploadedFile('other.txt', b'other')],
        })
        parser = MultiPartJSONParser()
        with pytest.raises(ParseError):
            parser._parse_operations(data, files)
        assert operations == {'first': None, 'second': None}

    @override_settings(
        ROOT_URLCONF='tests.test_parsers',
        FILE_UPLOAD_HANDLERS=[
            'tests.test_parsers.TrackingTemporaryFileUploadHandler',
        ],
    )
    def test_failure_closes_temporary_uploads(self):
        TrackingTemporaryFileUploadHandler.created_paths.clear()
        response = self.client.post('/nested-upload/', self.valid_parts(
            map=json.dumps({'photo': ['/profile/avatar']}),
            extra=SimpleUploadedFile('extra.txt', b'extra'),
        ))

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert len(TrackingTemporaryFileUploadHandler.created_paths) == 2
        assert not any(
            os.path.exists(path)
            for path in TrackingTemporaryFileUploadHandler.created_paths
        )


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
