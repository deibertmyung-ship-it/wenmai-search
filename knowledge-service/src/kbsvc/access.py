"""Shared document visibility rules."""

from __future__ import annotations


def document_is_visible(document_acl: list | None, allowed: set[str] | list[str] | None) -> bool:
    """Return whether a principal with *allowed* ACL tags may read a document.

    An empty principal ACL is the existing anonymous/admin mode and imposes no
    restriction. Public documents remain visible to every restricted principal.
    """

    allowed_tags = set(allowed or ())
    if not allowed_tags:
        return True
    document_tags = set(document_acl or ("public",))
    return "public" in document_tags or bool(document_tags & allowed_tags)
