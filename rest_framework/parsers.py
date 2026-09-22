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
    def __init__(self, data, files):
        self.data = data
        self.files = files


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


class NestedJSONMultiPartParser(BaseParser):
    """
    Parser for multipart requests that carry a JSON document together with
    file uploads, using separate "operations" and "map" control parts.

    The "operations" part contains a JSON object in which every file
    position is marked with `null`. The "map" part contains a JSON object
    mapping each uploaded file's field name to one or more JSON Pointers
    (RFC 6901) into the "operations" document. Every pointer must address
    an existing `null` value, which is then replaced by the uploaded file.
    Missing parents are not created and array holes are not filled.

    `.data` will be the "operations" document with the uploaded files
    placed at the mapped locations. `.files` will be empty.
    """
    media_type = 'multipart/form-data'

    operations_part = 'operations'
    map_part = 'map'

    def parse(self, stream, media_type=None, parser_context=None):
        """
        Parses the incoming bytestream as a multipart encoded form,
        and returns a DataAndFiles object.

        `.data` will be the "operations" document with uploaded files
        placed at the locations given by the "map" part.
        `.files` will be an empty `QueryDict`.
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
        except MultiPartParserError as exc:
            raise ParseError('Multipart form parse error - %s' % str(exc))

        try:
            operations = self.parse_control_part(data, self.operations_part)
            file_map = self.parse_control_part(data, self.map_part)
            placements = self.resolve_placements(operations, file_map, files)
        except ParseError:
            # The request is rejected, so close any uploaded files to avoid
            # leaking the temporary files created while parsing it.
            for _, file_list in files.lists():
                for uploaded_file in file_list:
                    uploaded_file.close()
            raise

        # Every location has been validated up front, so applying the
        # placements cannot fail halfway through and leave the operations
        # document partially modified.
        for (parent, key), uploaded_file in placements:
            parent[key] = uploaded_file

        return DataAndFiles(operations, QueryDict(''))

    def parse_control_part(self, data, name):
        """
        Return the JSON object held by the control part `name`, raising a
        `ParseError` if the part is missing, duplicated, or invalid.
        """
        values = data.getlist(name)
        if not values:
            raise ParseError(
                "Multipart request is missing the '%s' part." % name
            )
        if len(values) > 1:
            raise ParseError(
                "Multipart request contains duplicate '%s' parts." % name
            )
        try:
            parsed = json.loads(values[0])
        except ValueError as exc:
            raise ParseError(
                "The '%s' part is not valid JSON - %s" % (name, exc)
            )
        if not isinstance(parsed, dict):
            raise ParseError(
                "The '%s' part must contain a JSON object." % name
            )
        return parsed

    def resolve_placements(self, operations, file_map, files):
        """
        Match file parts with their mapped locations in the operations
        document, returning a list of `((parent, key), uploaded_file)`
        placements. Raises a `ParseError` without modifying `operations`
        if anything is invalid.
        """
        for name in file_map:
            if name not in files:
                raise ParseError(
                    "The 'map' part references file part '%s', "
                    "which was not uploaded." % name
                )
        for name in files:
            if name not in file_map:
                raise ParseError(
                    "File part '%s' was uploaded but is not referenced "
                    "by the 'map' part." % name
                )
            if len(files.getlist(name)) != 1:
                raise ParseError(
                    "File part '%s' was uploaded more than once." % name
                )

        placements = []
        seen = {}
        for name, pointers in file_map.items():
            uploaded_file = files[name]
            for pointer in self.normalize_pointers(name, pointers):
                parent, key = self.resolve_pointer(operations, pointer, name)
                location = (id(parent), key)
                if location in seen:
                    other_name, other_pointer = seen[location]
                    raise ParseError(
                        "File parts '%s' and '%s' are both mapped to the "
                        "same location ('%s' and '%s')."
                        % (other_name, name, other_pointer, pointer)
                    )
                if parent[key] is not None:
                    raise ParseError(
                        "JSON pointer '%s' for file part '%s' does not "
                        "point to a null value." % (pointer, name)
                    )
                seen[location] = (name, pointer)
                placements.append(((parent, key), uploaded_file))
        return placements

    def normalize_pointers(self, name, pointers):
        """
        Return the list of JSON pointers given for the file part `name`.
        """
        if isinstance(pointers, str):
            pointers = [pointers]
        if (
            not isinstance(pointers, list) or not pointers or
            not all(isinstance(pointer, str) for pointer in pointers)
        ):
            raise ParseError(
                "The 'map' entry for file part '%s' must be a non-empty "
                "list of JSON pointers." % name
            )
        return pointers

    def resolve_pointer(self, operations, pointer, name):
        """
        Resolve a JSON pointer against the operations document, returning
        the `(parent, key)` pair it addresses. Raises a `ParseError` if
        the pointer cannot be resolved.
        """
        if not pointer.startswith('/'):
            raise ParseError(
                "Invalid JSON pointer '%s' for file part '%s'."
                % (pointer, name)
            )
        segments = [
            segment.replace('~1', '/').replace('~0', '~')
            for segment in pointer.split('/')[1:]
        ]
        current = operations
        for segment in segments[:-1]:
            current = self.descend(current, segment, pointer, name)
        segment = segments[-1]
        if isinstance(current, dict):
            if segment not in current:
                raise ParseError(
                    "JSON pointer '%s' for file part '%s' does not exist "
                    "in the operations document." % (pointer, name)
                )
            return current, segment
        if isinstance(current, list):
            return current, self.list_index(current, segment, pointer, name)
        raise ParseError(
            "JSON pointer '%s' for file part '%s' traverses a value that "
            "is not an object or array." % (pointer, name)
        )

    def descend(self, current, segment, pointer, name):
        """
        Resolve a single non-final JSON pointer segment.
        """
        if isinstance(current, dict):
            if segment not in current:
                raise ParseError(
                    "JSON pointer '%s' for file part '%s' does not exist "
                    "in the operations document." % (pointer, name)
                )
            return current[segment]
        if isinstance(current, list):
            return current[self.list_index(current, segment, pointer, name)]
        raise ParseError(
            "JSON pointer '%s' for file part '%s' traverses a value that "
            "is not an object or array." % (pointer, name)
        )

    def list_index(self, current, segment, pointer, name):
        """
        Return `segment` as an index into the list `current`, raising a
        `ParseError` if it is not a valid existing index.
        """
        try:
            index = int(segment)
        except ValueError:
            raise ParseError(
                "JSON pointer '%s' for file part '%s' uses '%s', which is "
                "not a valid array index." % (pointer, name, segment)
            )
        if not 0 <= index < len(current):
            raise ParseError(
                "JSON pointer '%s' for file part '%s' is out of range for "
                "the array in the operations document." % (pointer, name)
            )
        return index


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
