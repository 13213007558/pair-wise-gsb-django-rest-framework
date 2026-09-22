"""
Parsers are used to parse the content of incoming HTTP requests.

They give us a generic way of being able to handle various media types
on the request, such as form content or json encoded data.
"""

import codecs
import contextlib

from django.conf import settings
from django.core.files.uploadhandler import StopFutureHandlers
from django.http import QueryDict
from django.http.multipartparser import ChunkIter
from django.http.multipartparser import (
    MultiPartParser as DjangoMultiPartParser
)
from django.http.multipartparser import MultiPartParserError
from django.utils.http import parse_header_parameters

from rest_framework import renderers
from rest_framework.exceptions import ParseError
from rest_framework.settings import api_settings
from rest_framework.utils import json


class DataAndFiles:
    def __init__(self, data, files, embed_files=False):
        self.data = data
        self.files = files
        self.embed_files = embed_files


class BaseParser:
    """
    All parsers should extend `BaseParser`, specifying a `media_type`
    attribute, and overriding the `.parse()` method.
    """
    media_type = None

    def parse(self, stream, media_type=None, parser_context=None):
        """
        Given a stream to read from, return the parsed representation.
        Should return parsed data, or a `DataAndFiles` object consisting of the
        parsed data and files.
        """
        raise NotImplementedError(".parse() must be overridden.")


class JSONParser(BaseParser):
    """
    Parses JSON-serialized data.
    """
    media_type = 'application/json'
    renderer_class = renderers.JSONRenderer
    strict = api_settings.STRICT_JSON

    def parse(self, stream, media_type=None, parser_context=None):
        """
        Parses the incoming bytestream as JSON and returns the resulting data.
        """
        parser_context = parser_context or {}
        encoding = parser_context.get('encoding', settings.DEFAULT_CHARSET)

        try:
            decoded_stream = codecs.getreader(encoding)(stream)
            parse_constant = json.strict_constant if self.strict else None
            return json.load(decoded_stream, parse_constant=parse_constant)
        except ValueError as exc:
            raise ParseError('JSON parse error - %s' % str(exc))


class FormParser(BaseParser):
    """
    Parser for form data.
    """
    media_type = 'application/x-www-form-urlencoded'

    def parse(self, stream, media_type=None, parser_context=None):
        """
        Parses the incoming bytestream as a URL encoded form,
        and returns the resulting QueryDict.
        """
        parser_context = parser_context or {}
        encoding = parser_context.get('encoding', settings.DEFAULT_CHARSET)
        return QueryDict(stream.read(), encoding=encoding)


class MultiPartParser(BaseParser):
    """
    Parser for multipart form data, which may include file data.
    """
    media_type = 'multipart/form-data'

    def parse(self, stream, media_type=None, parser_context=None):
        """
        Parses the incoming bytestream as a multipart encoded form,
        and returns a DataAndFiles object.

        `.data` will be a `QueryDict` containing all the form parameters.
        `.files` will be a `QueryDict` containing all the form files.
        """
        parser_context = parser_context or {}
        request = parser_context['request']
        encoding = parser_context.get('encoding', settings.DEFAULT_CHARSET)
        meta = request.META.copy()
        meta['CONTENT_TYPE'] = media_type
        upload_handlers = request.upload_handlers

        try:
            parser = DjangoMultiPartParser(meta, stream, upload_handlers, encoding)
            data, files = parser.parse()
            return DataAndFiles(data, files)
        except MultiPartParserError as exc:
            raise ParseError('Multipart form parse error - %s' % str(exc))


class MultiPartJSONParser(MultiPartParser):
    """
    Parser for multipart requests containing JSON data and file uploads.

    The ``operations`` part contains a JSON object. The ``map`` part is a
    JSON object mapping file part names to one or more JSON Pointers. Each
    pointer must identify a value that is ``null`` in ``operations``.
    """

    def parse(self, stream, media_type=None, parser_context=None):
        parsed = super().parse(stream, media_type, parser_context)

        try:
            operations = self._parse_operations(parsed.data, parsed.files)
            return DataAndFiles(operations, parsed.files, embed_files=True)
        except ParseError:
            self._close_files(parsed.files)
            raise

    def _parse_operations(self, data, files):
        controls = ('operations', 'map')

        for name in controls:
            if files.getlist(name):
                raise ParseError(
                    "Multipart JSON parse error - '%s' must be a text part."
                    % name
                )
            if len(data.getlist(name)) > 1:
                raise ParseError(
                    "Multipart JSON parse error - duplicate '%s' part." % name
                )
            if name not in data:
                raise ParseError(
                    "Multipart JSON parse error - missing '%s' part." % name
                )

        unexpected_parts = sorted(set(data) - set(controls))
        if unexpected_parts:
            raise ParseError(
                "Multipart JSON parse error - unexpected part %r."
                % unexpected_parts[0]
            )

        try:
            operations = json.loads(data['operations'])
        except ValueError as exc:
            raise ParseError(
                "Multipart JSON parse error - invalid JSON in 'operations' "
                "part - %s" % str(exc)
            )

        if not isinstance(operations, dict):
            raise ParseError(
                "Multipart JSON parse error - 'operations' must be a JSON "
                "object."
            )

        try:
            mapping = json.loads(data['map'])
        except ValueError as exc:
            raise ParseError(
                "Multipart JSON parse error - invalid JSON in 'map' part - %s"
                % str(exc)
            )

        if not isinstance(mapping, dict):
            raise ParseError(
                "Multipart JSON parse error - 'map' must be a JSON object."
            )

        for part_name, uploaded_files in files.lists():
            if part_name in controls:
                continue
            if len(uploaded_files) > 1:
                raise ParseError(
                    "Multipart JSON parse error - duplicate file part %r."
                    % part_name
                )

        assignments = []
        targets = {}

        for part_name, pointers in mapping.items():
            if not isinstance(pointers, list) or not pointers:
                raise ParseError(
                    "Multipart JSON parse error - map[%r] must be a non-empty "
                    "array of JSON Pointer strings." % part_name
                )

            if part_name not in files:
                raise ParseError(
                    "Multipart JSON parse error - map[%r] references a part "
                    "without an uploaded file." % part_name
                )

            uploaded_file = files[part_name]
            for index, pointer in enumerate(pointers):
                location = "map[%r][%d]" % (part_name, index)
                if not isinstance(pointer, str):
                    raise ParseError(
                        "Multipart JSON parse error - %s must be a JSON "
                        "Pointer string." % location
                    )

                if pointer in targets:
                    raise ParseError(
                        "Multipart JSON parse error - %s maps JSON Pointer "
                        "%r, already mapped by %s." % (
                            location,
                            pointer,
                            targets[pointer],
                        )
                    )

                targets[pointer] = location
                target = self._resolve_pointer(pointer, operations, location)
                assignments.append((target, uploaded_file))

        unreferenced_parts = sorted(set(files) - set(controls) - set(mapping))
        if unreferenced_parts:
            raise ParseError(
                "Multipart JSON parse error - file part %r is not referenced "
                "in 'map'." % unreferenced_parts[0]
            )

        for (parent, key), uploaded_file in assignments:
            parent[key] = uploaded_file

        return operations

    def _resolve_pointer(self, pointer, operations, location):
        if pointer == '':
            raise ParseError(
                "Multipart JSON parse error - %s points to the root object, "
                "which is not a null value."
                % location
            )

        if not pointer.startswith('/'):
            raise ParseError(
                "Multipart JSON parse error - %s contains invalid JSON "
                "Pointer %r." % (location, pointer)
            )

        tokens = pointer.split('/')[1:]
        container = operations

        for depth, token in enumerate(tokens):
            for index, character in enumerate(token):
                if character == '~' and (
                    index + 1 == len(token)
                    or token[index + 1] not in ('0', '1')
                ):
                    raise ParseError(
                        "Multipart JSON parse error - %s contains invalid JSON "
                        "Pointer %r." % (location, pointer)
                    )

            key = token.replace('~1', '/').replace('~0', '~')
            is_final = depth == len(tokens) - 1

            if isinstance(container, list):
                if key == '-' or not self._is_array_index(key):
                    raise ParseError(
                        "Multipart JSON parse error - %s points to a missing "
                        "array index." % location
                    )

                index = int(key)
                if index >= len(container):
                    raise ParseError(
                        "Multipart JSON parse error - %s points outside the "
                        "existing array." % location
                    )
                if is_final:
                    target = container[index]
                    if target is not None:
                        raise ParseError(
                            "Multipart JSON parse error - %s points to a "
                            "non-null value." % location
                        )
                    return (container, index)
                container = container[index]
            elif isinstance(container, dict):
                if key not in container:
                    raise ParseError(
                        "Multipart JSON parse error - %s points to a missing "
                        "object key." % location
                    )
                if is_final:
                    target = container[key]
                    if target is not None:
                        raise ParseError(
                            "Multipart JSON parse error - %s points to a "
                            "non-null value." % location
                        )
                    return (container, key)
                container = container[key]
            else:
                raise ParseError(
                    "Multipart JSON parse error - %s points through a "
                    "non-container value." % location
                )

    def _is_array_index(self, key):
        return (
            key.isascii()
            and key.isdigit()
            and (len(key) == 1 or not key.startswith('0'))
        )

    def _close_files(self, files):
        for uploaded_files in files.lists():
            for uploaded_file in uploaded_files[1]:
                uploaded_file.close()


JSONMultiPartParser = MultiPartJSONParser


class FileUploadParser(BaseParser):
    """
    Parser for file upload data.
    """
    media_type = '*/*'
    errors = {
        'unhandled': 'FileUpload parse error - none of upload handlers can handle the stream',
        'no_filename': 'Missing filename. Request should include a Content-Disposition header with a filename parameter.',
    }

    def parse(self, stream, media_type=None, parser_context=None):
        """
        Treats the incoming bytestream as a raw file upload and returns
        a `DataAndFiles` object.

        `.data` will be None (we expect request body to be a file content).
        `.files` will be a `QueryDict` containing one 'file' element.
        """
        parser_context = parser_context or {}
        request = parser_context['request']
        encoding = parser_context.get('encoding', settings.DEFAULT_CHARSET)
        meta = request.META
        upload_handlers = request.upload_handlers
        filename = self.get_filename(stream, media_type, parser_context)

        if not filename:
            raise ParseError(self.errors['no_filename'])

        # Note that this code is extracted from Django's handling of
        # file uploads in MultiPartParser.
        content_type = meta.get('HTTP_CONTENT_TYPE',
                                meta.get('CONTENT_TYPE', ''))
        try:
            content_length = int(meta.get('HTTP_CONTENT_LENGTH',
                                          meta.get('CONTENT_LENGTH', 0)))
        except (ValueError, TypeError):
            content_length = None

        # See if the handler will want to take care of the parsing.
        for handler in upload_handlers:
            result = handler.handle_raw_input(stream,
                                              meta,
                                              content_length,
                                              None,
                                              encoding)
            if result is not None:
                return DataAndFiles({}, {'file': result[1]})

        # This is the standard case.
        possible_sizes = [x.chunk_size for x in upload_handlers if x.chunk_size]
        chunk_size = min([2 ** 31 - 4] + possible_sizes)
        chunks = ChunkIter(stream, chunk_size)
        counters = [0] * len(upload_handlers)

        for index, handler in enumerate(upload_handlers):
            try:
                handler.new_file(None, filename, content_type,
                                 content_length, encoding)
            except StopFutureHandlers:
                upload_handlers = upload_handlers[:index + 1]
                break

        for chunk in chunks:
            for index, handler in enumerate(upload_handlers):
                chunk_length = len(chunk)
                chunk = handler.receive_data_chunk(chunk, counters[index])
                counters[index] += chunk_length
                if chunk is None:
                    break

        for index, handler in enumerate(upload_handlers):
            file_obj = handler.file_complete(counters[index])
            if file_obj is not None:
                return DataAndFiles({}, {'file': file_obj})

        raise ParseError(self.errors['unhandled'])

    def get_filename(self, stream, media_type, parser_context):
        """
        Detects the uploaded file name. First searches a 'filename' url kwarg.
        Then tries to parse Content-Disposition header.
        """
        with contextlib.suppress(KeyError):
            return parser_context['kwargs']['filename']

        with contextlib.suppress(AttributeError, KeyError, ValueError):
            meta = parser_context['request'].META
            disposition, params = parse_header_parameters(meta['HTTP_CONTENT_DISPOSITION'])
            if 'filename*' in params:
                return params['filename*']
            return params['filename']
