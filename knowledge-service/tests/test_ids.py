"""Identifier derivation is the backbone of idempotent ingestion."""

from __future__ import annotations

import pytest

from kbsvc import ids
from kbsvc.errors import ValidationError
from kbsvc.storage.local import LocalObjectStore


def test_document_id_is_stable_across_calls():
    first = ids.document_id("t1", "s1", "book/a.txt")
    second = ids.document_id("t1", "s1", "book/a.txt")
    assert first == second


def test_document_id_differs_per_tenant_and_source():
    base = ids.document_id("t1", "s1", "a.txt")
    assert ids.document_id("t2", "s1", "a.txt") != base
    assert ids.document_id("t1", "s2", "a.txt") != base


def test_chunk_id_changes_when_content_changes():
    unchanged = ids.chunk_id("doc", 1, 0, "hash-a")
    assert ids.chunk_id("doc", 1, 0, "hash-a") == unchanged
    assert ids.chunk_id("doc", 1, 0, "hash-b") != unchanged
    assert ids.chunk_id("doc", 2, 0, "hash-a") != unchanged
    assert ids.chunk_id("doc", 1, 1, "hash-a") != unchanged


def test_hash_functions_agree_on_the_same_payload(tmp_path):
    payload = "六壬贼克".encode()
    path = tmp_path / "sample.txt"
    path.write_bytes(payload)
    assert ids.hash_bytes(payload) == ids.hash_file(path)
    assert ids.hash_stream([payload[:2], payload[2:]]) == ids.hash_bytes(payload)


def test_object_key_strips_user_controlled_path_segments():
    key = ids.object_key("../../etc", "doc-1", "abc123", ".txt")
    assert key.startswith("etc/") and ".." not in key
    assert key.endswith(".txt")


def test_local_store_rejects_keys_escaping_the_root(tmp_path):
    store = LocalObjectStore(tmp_path / "objects")
    with pytest.raises(ValidationError):
        store.put("../escaped.txt", b"nope")


def test_local_store_round_trips(tmp_path):
    store = LocalObjectStore(tmp_path / "objects")
    store.put("t/doc/hash.txt", b"payload")
    assert store.exists("t/doc/hash.txt")
    assert store.get("t/doc/hash.txt") == b"payload"
    store.delete("t/doc/hash.txt")
    assert not store.exists("t/doc/hash.txt")
