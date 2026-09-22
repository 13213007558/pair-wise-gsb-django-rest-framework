"""
JSON Merge Patch (RFC 7396) support.

This module provides the merge_patch() function used to apply a
JSON Merge Patch document to a target document. Views opt into merge
patch handling with MergePatchMixin from rest_framework.mixins
together with MergePatchParser from rest_framework.parsers.
"""
from collections.abc import Mapping

__all__ = ['merge_patch']


def merge_patch(target, patch):
    """
    Apply a JSON Merge Patch (RFC 7396) patch to a target document,
    returning the merged document. Neither argument is mutated.

    Objects are merged recursively. A None value in the patch removes the
    corresponding key from the result. Any non-object patch value,
    including arrays and scalars, replaces the target value wholesale.
    """
    if not isinstance(patch, Mapping):
        return patch

    if not isinstance(target, Mapping):
        target = {}

    merged = dict(target)
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = merge_patch(target.get(key), value)
    return merged
