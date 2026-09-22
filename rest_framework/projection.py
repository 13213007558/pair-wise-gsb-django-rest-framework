"""
Reusable, read-only field projection ("sparse fieldsets") support.

Field projection lets a view explicitly opt in to client-driven selection
of the fields that should be serialized, so that lightweight consumers
(for example mobile clients) can trim a response down to paths such as
'title', 'owner.name' or 'entries.id' while other consumers keep
receiving the full representation.

Projection is applied to serializer *instances*, never to the shared
field definitions on the serializer class, so two requests reusing the
same serializer class never affect each other.  Fields that are not
selected are removed from the instance before serialization starts,
which means their 'get_attribute()' calls, 'SerializerMethodField'
methods and lazy relationship accesses are never triggered.
"""

from django.utils.translation import gettext_lazy as _

from rest_framework.exceptions import ParseError
from rest_framework.serializers import BaseSerializer, ListSerializer

__all__ = [
    'ProjectionError',
    'compile_projection',
    'apply_projection',
    'FieldProjectionMixin',
]


class ProjectionError(ParseError):
    """
    Raised when a requested field projection cannot be satisfied.
    """
    status_code = 400
    default_detail = _('Invalid field projection.')
    default_code = 'invalid_projection'


def _insert_path(tree, segments):
    """
    Insert one dotted path (as a list of segments) into a projection tree.

    A node mapping to an empty dict is fully selected.  Selecting a parent
    absorbs any child selections, and child selections under an already
    fully selected parent are absorbed by the parent.
    """
    node = tree
    for segment in segments[:-1]:
        child = node.get(segment)
        if child is None:
            child = node[segment] = {}
        elif not child:
            # The parent is already fully selected; the child selection
            # is absorbed.
            return
        node = child
    # Selecting the parent absorbs any previously registered children.
    node[segments[-1]] = {}


def compile_projection(paths):
    """
    Compile an iterable of dotted field paths into a projection tree.

    The tree maps field names to nested dicts; an empty dict means the
    field is selected as a whole.
    """
    tree = {}
    for path in paths:
        segments = [segment.strip() for segment in path.split('.')]
        if any(not segment for segment in segments):
            raise ProjectionError(
                _("Invalid field path '%(path)s'.") % {'path': path}
            )
        _insert_path(tree, segments)
    return tree


def _is_allowed(whitelist_tree, segments, is_leaf):
    """
    Return True if the dotted path is covered by the whitelist tree.

    A whitelisted parent opens its whole subtree.  Fully selecting
    (``is_leaf``) a parent of whitelisted leaves is rejected, because it
    would expose fields that were not explicitly whitelisted.
    """
    node = whitelist_tree
    for segment in segments:
        if not node:
            return True
        if segment not in node:
            return False
        node = node[segment]
    return not is_leaf or not node


def _projectable_child(field):
    """
    Return the nested serializer a projection may descend into, or None
    if the field is a scalar (or otherwise non-projectable) field.
    """
    if isinstance(field, ListSerializer):
        child = field.child
        return child if isinstance(child, BaseSerializer) else None
    if isinstance(field, BaseSerializer):
        return field
    # ListField/DictField style fields may wrap a serializer child.
    child = getattr(field, 'child', None)
    if isinstance(child, BaseSerializer):
        return child
    return None


def _apply_projection(serializer, tree, whitelist_tree, segments):
    if isinstance(serializer, ListSerializer):
        _apply_projection(serializer.child, tree, whitelist_tree, segments)
        return

    fields = serializer.fields
    for name, subtree in tree.items():
        path = segments + (name,)
        dotted = '.'.join(path)
        if name not in fields:
            raise ProjectionError(
                _("Unknown field '%(path)s' in projection.") % {'path': dotted}
            )
        field = fields[name]
        if field.write_only:
            raise ProjectionError(
                _("Field '%(path)s' is write-only and cannot be projected.")
                % {'path': dotted}
            )
        if whitelist_tree is not None and not _is_allowed(whitelist_tree, path, not subtree):
            raise ProjectionError(
                _("Field '%(path)s' is not included in the allowed projection.")
                % {'path': dotted}
            )
        if subtree:
            child = _projectable_child(field)
            if child is None:
                raise ProjectionError(
                    _("Cannot select sub-fields of '%(path)s' because it is "
                      "not a nested serializer.") % {'path': dotted}
                )
            _apply_projection(child, subtree, whitelist_tree, path)

    # Prune every field that was not selected.  Only the remaining fields
    # are touched during serialization, so dropped fields never evaluate
    # attributes, methods or relationships.  Iteration order (and thus the
    # output order) follows the original serializer.
    for name in list(fields.keys()):
        if name not in tree:
            del fields[name]


def apply_projection(serializer, paths, allowed_fields=None):
    """
    Restrict 'serializer' to the dotted field 'paths', in place.

    'paths' uses the serializer's public field names (not 'source'
    attribute names) and propagates through nested serializers and
    'many=True' lists.  'allowed_fields' is an optional whitelist of
    dotted paths; requested paths outside it raise 'ProjectionError'.

    Only the serializer instance is modified; the field definitions shared
    on the serializer class are left untouched.
    """
    tree = compile_projection(paths)
    whitelist_tree = (
        compile_projection(allowed_fields) if allowed_fields is not None else None
    )
    _apply_projection(serializer, tree, whitelist_tree, ())
    return serializer


class FieldProjectionMixin:
    """
    Opt a view into client-driven field projection via a query parameter.

    Add the mixin before the generic view class and, optionally, set
    'projection_allowed_fields' to a whitelist of dotted paths::

        class EntryList(FieldProjectionMixin, generics.ListAPIView):
            serializer_class = EntrySerializer
            projection_allowed_fields = ['id', 'title', 'owner.name']

    'GET /entries/?fields=title,owner.name' then returns only the
    selected paths.  Views without the mixin ignore the parameter and keep
    returning the full representation.
    """

    projection_query_param = 'fields'
    projection_allowed_fields = None

    def get_projection_query_param(self):
        return self.projection_query_param

    def get_projection_allowed_fields(self):
        return self.projection_allowed_fields

    def get_projection_paths(self):
        """
        Return the requested dotted paths, or None when no projection was
        requested.  Supports comma-separated values and repeated params.
        """
        param = self.get_projection_query_param()
        values = self.request.query_params.getlist(param)
        paths = []
        for value in values:
            paths.extend(segment.strip() for segment in value.split(','))
        return [path for path in paths if path] or None

    def get_serializer(self, *args, **kwargs):
        serializer = super().get_serializer(*args, **kwargs)
        paths = self.get_projection_paths()
        if paths:
            apply_projection(
                serializer,
                paths,
                allowed_fields=self.get_projection_allowed_fields(),
            )
        return serializer
