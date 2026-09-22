"""
Read-only field projection support.

Provides a reusable, opt-in mechanism that lets API clients select a subset
of serializer fields through a query parameter, eg:

    GET /articles/1?fields=title,owner.name,entries.id

Projection is applied to serializer *instances* only. The shared field
definitions declared on serializer classes are never modified, so two
requests reusing the same serializer class do not affect each other.

Fields that are not selected are removed from the instance before
serialization runs, which means their `get_attribute()` calls,
`SerializerMethodField` methods and lazy relationship lookups are never
triggered. Projection is not implemented by serializing everything and
deleting keys afterwards.
"""

from rest_framework.exceptions import ParseError
from rest_framework.serializers import ListSerializer, Serializer

__all__ = [
    'ProjectionError',
    'FieldProjectionMixin',
    'parse_projection',
    'apply_projection',
]


class ProjectionError(ParseError):
    """
    Raised when a requested field projection cannot be satisfied.

    Results in a `400 Bad Request` response with a descriptive message.
    """

    default_detail = 'Invalid field projection.'


_unset = object()


def parse_projection(value):
    """
    Parse a `fields` query parameter value into a projection tree.

    Returns a dict mapping field names to either `None` (the field is
    selected as a whole) or a nested dict of child selections.

    Selecting a parent field as a whole absorbs any child selections for
    that parent, and child selections made before the parent selection are
    discarded, eg `owner.name,owner` and `owner,owner.name` both compile
    to `{'owner': None}`.
    """
    tree = {}
    for raw_path in value.split(','):
        path = raw_path.strip()
        if not path:
            continue
        parts = path.split('.')
        node = tree
        for index, part in enumerate(parts):
            if not part:
                raise ProjectionError(
                    "Invalid empty field name in projection path '%s'." % path
                )
            existing = node.get(part, _unset)
            if existing is None:
                # Already selected as a whole; child selections are absorbed.
                break
            if index == len(parts) - 1:
                # Selecting a field as a whole absorbs child selections.
                node[part] = None
            else:
                if existing is _unset:
                    existing = node[part] = {}
                node = existing
    return tree


def _normalize_allowed_fields(allowed_fields):
    return {
        path.strip() for path in allowed_fields if path and path.strip()
    }


def _check_allowed(path, allowed_fields, wholesale):
    dotted = '.'.join(path)
    for allowed in allowed_fields:
        if wholesale:
            # Selecting a field as a whole exposes its whole subtree, so the
            # whitelist must contain the path itself or one of its ancestors.
            if dotted == allowed or dotted.startswith(allowed + '.'):
                return
        else:
            # Intermediate paths only need to lead to a whitelisted path.
            if allowed == dotted or allowed.startswith(dotted + '.'):
                return
    raise ProjectionError(
        "Projection path '%s' is not included in the allowed fields." % dotted
    )


def _apply_projection(serializer, tree, allowed_fields, path):
    if isinstance(serializer, ListSerializer):
        # Projection applies to the child serializer; the list itself,
        # including any `None` entries, keeps its original shape.
        _apply_projection(serializer.child, tree, allowed_fields, path)
        return

    fields = serializer.fields
    for name, children in tree.items():
        field_path = path + (name,)
        dotted = '.'.join(field_path)
        if allowed_fields is not None:
            _check_allowed(field_path, allowed_fields, wholesale=children is None)
        try:
            field = fields[name]
        except KeyError:
            raise ProjectionError(
                "Unknown field '%s' in projection." % dotted
            )
        if field.write_only:
            raise ProjectionError(
                "Field '%s' is write-only and cannot be projected." % dotted
            )
        if children is not None:
            target = field
            while isinstance(target, ListSerializer):
                target = target.child
            if not isinstance(target, Serializer):
                raise ProjectionError(
                    "Field '%s' does not support nested projection." % dotted
                )
            _apply_projection(target, children, allowed_fields, field_path)

    # Prune fields that were not selected. `fields` is a per-instance
    # cached property backed by copies of the declared fields, so this never
    # touches the shared definitions on the serializer class.
    for name in list(fields.keys()):
        if name not in tree:
            del fields[name]


def apply_projection(serializer, tree, allowed_fields=None):
    """
    Apply a parsed projection tree to a serializer instance.

    `tree` is a projection tree as returned by `parse_projection()`.
    `allowed_fields` is an optional iterable of dotted paths that acts as a
    whitelist; a requested path is allowed when it is equal to, or nested
    under, one of the whitelisted paths.

    Raises `ProjectionError` for unknown fields, nested selections on
    scalar fields, write-only fields and paths outside the whitelist.
    """
    if allowed_fields is not None:
        allowed_fields = _normalize_allowed_fields(allowed_fields)
    _apply_projection(serializer, tree, allowed_fields, path=())
    return serializer


class FieldProjectionMixin:
    """
    Opt-in read-only field projection for generic views.

    Clients select fields with a query parameter using the serializer's
    public field names (not `source` attribute names), eg:

        GET /articles/1?fields=title,owner.name,entries.id

    Projection propagates through nested serializers and `many=True`
    relationships. Output key order always follows the original serializer.

    Views may set `projection_allowed_fields` to a whitelist of dotted
    paths to restrict what clients may select.
    """

    projection_query_param = 'fields'
    projection_allowed_fields = None

    def get_projection_allowed_fields(self):
        """
        Return the optional whitelist of dotted projection paths, or
        `None` to allow any path that is valid for the serializer.
        """
        return self.projection_allowed_fields

    def get_projection_tree(self):
        """
        Return the parsed projection tree for the current request, or
        `None` when no projection was requested.
        """
        value = self.request.query_params.get(self.projection_query_param)
        if value is None:
            return None
        return parse_projection(value)

    def get_serializer(self, *args, **kwargs):
        serializer = super().get_serializer(*args, **kwargs)
        tree = self.get_projection_tree()
        if tree:
            apply_projection(
                serializer,
                tree,
                allowed_fields=self.get_projection_allowed_fields(),
            )
        return serializer
