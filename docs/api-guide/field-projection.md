# Field projection

Field projection lets a view opt in to client-driven selection of the
fields included in a response.  Lightweight consumers, such as mobile
clients, can trim a response down to just the paths they need — for
example 'title', 'owner.name' and 'entries.id' — while other consumers
keep receiving the full representation.

Projection is read-only and is applied to serializer *instances*: the
field definitions shared on the serializer class are never modified, so
requests reusing the same serializer class never affect each other.
Fields that are not selected are removed before serialization starts,
which means their attribute lookups, 'SerializerMethodField' methods and
lazy relationship accesses are never evaluated.

---

## Enabling projection on a view

Add 'FieldProjectionMixin' before the generic view class:

    from rest_framework import generics
    from rest_framework.projection import FieldProjectionMixin

    class EntryList(FieldProjectionMixin, generics.ListAPIView):
        queryset = Entry.objects.all()
        serializer_class = EntrySerializer
        projection_allowed_fields = ['id', 'title', 'owner.name']

Clients then select paths with the 'fields' query parameter, using the
serializer's public field names (not the 'source' attribute names):

    GET /entries/?fields=title,owner.name

Paths propagate through nested serializers and 'many=True' lists, and may
be given as a comma-separated value or by repeating the parameter.
Selecting a parent field as a whole absorbs any child selections, and the
output order always follows the original serializer.

Views without the mixin ignore the parameter and keep returning the full
representation.

## View attributes

* 'projection_query_param' — the query parameter to read, defaults to
  '"fields"'.
* 'projection_allowed_fields' — an optional whitelist of dotted paths.
  When set, any requested path outside the whitelist is rejected.  A
  whitelisted parent opens its whole subtree, while fully selecting the
  parent of whitelisted leaves is rejected, since it would expose fields
  that were not explicitly allowed.

## Errors

Invalid projections respond with '400 Bad Request'.  This includes
unknown field paths, sub-paths of scalar fields, 'write_only' fields, and
paths outside the whitelist.

## Applying projections directly

Projections can also be applied to any serializer instance directly:

    from rest_framework.projection import apply_projection

    serializer = EntrySerializer(queryset, many=True)
    apply_projection(serializer, ['title', 'owner.name'],
                     allowed_fields=['id', 'title', 'owner.name'])
    serializer.data
