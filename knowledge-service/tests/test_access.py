from __future__ import annotations

import pytest

from kbsvc.access import document_is_visible


@pytest.mark.parametrize(
    ("document_acl", "allowed", "visible"),
    [
        (["public"], ["team-a"], True),
        (["team-a"], ["team-a"], True),
        (["team-a"], ["team-b"], False),
        (["team-a"], [], True),
        (None, ["team-b"], True),
    ],
)
def test_document_visibility_uses_shared_acl_semantics(document_acl, allowed, visible):
    assert document_is_visible(document_acl, allowed) is visible
