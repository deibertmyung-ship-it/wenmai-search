"""The webfont gate.

deploy/build-fonts.py is optional - kbweb has to render whether or not it has
been run, because the font stacks in tokens.css fall through to whatever the
machine has. base.html therefore links the generated stylesheet only when the
generated shards are really present, and this is the predicate that decides.

The interesting case is the stale one: fonts.css survives in a working tree
long after someone deletes the shard directories, and linking it then would
serve a stylesheet whose every @import 404s.
"""

from __future__ import annotations

import flask
import pytest
from kbweb import _webfonts_present


@pytest.fixture
def static_dir(tmp_path):
    (tmp_path / "css").mkdir()
    return tmp_path


def _app(static_dir) -> flask.Flask:
    return flask.Flask(__name__, static_folder=str(static_dir))


def test_absent_when_nothing_has_been_built(static_dir):
    assert _webfonts_present(_app(static_dir)) is False


def test_absent_when_the_stylesheet_survives_without_its_shards(static_dir):
    (static_dir / "css" / "fonts.css").write_text("@import url('../fonts/song/result.css');")
    assert _webfonts_present(_app(static_dir)) is False


def test_absent_when_shards_exist_without_the_stylesheet(static_dir):
    (static_dir / "fonts" / "song").mkdir(parents=True)
    (static_dir / "fonts" / "song" / "result.css").write_text("@font-face{}")
    assert _webfonts_present(_app(static_dir)) is False


def test_present_when_both_halves_are_there(static_dir):
    (static_dir / "css" / "fonts.css").write_text("@import url('../fonts/song/result.css');")
    (static_dir / "fonts" / "song").mkdir(parents=True)
    (static_dir / "fonts" / "song" / "result.css").write_text("@font-face{}")
    assert _webfonts_present(_app(static_dir)) is True


def test_one_face_is_enough(static_dir):
    """Building only kai is a legitimate partial state, not a broken one."""
    (static_dir / "css" / "fonts.css").write_text("@import url('../fonts/kai/result.css');")
    (static_dir / "fonts" / "kai").mkdir(parents=True)
    (static_dir / "fonts" / "kai" / "result.css").write_text("@font-face{}")
    assert _webfonts_present(_app(static_dir)) is True
