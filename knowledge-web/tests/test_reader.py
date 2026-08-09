from __future__ import annotations

from kbweb.reader import with_highlight_fragments


def test_reader_splits_highlight_without_creating_html() -> None:
    chunk = with_highlight_fragments(
        {
            "text": "前<script>中</script>后",
            "highlights": [{"local_start": 9, "local_end": 19}],
        }
    )

    assert chunk["fragments"] == [
        {"text": "前<script>", "highlighted": False, "anchor": False},
        {"text": "中</script>", "highlighted": True, "anchor": True},
        {"text": "后", "highlighted": False, "anchor": False},
    ]


def test_reader_discards_out_of_bounds_ranges() -> None:
    chunk = with_highlight_fragments(
        {"text": "正文", "highlights": [{"local_start": -1, "local_end": 99}]}
    )

    assert chunk["fragments"] == [
        {"text": "正文", "highlighted": False, "anchor": False}
    ]
