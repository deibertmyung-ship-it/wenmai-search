"""Orthographic normalization for the *retrieval representation* of text.

The corpus is mixed: 14.3M characters, mostly simplified, but traditional and
old glyph forms occur throughout (陰 524×, 發 512×, 陽 402×). Without folding,
a query for 阴阳 and a query for 陰陽 retrieve disjoint result sets - measured
0/10 overlap on the lexical side.

**Never applied to stored text.** The payload's `text` is what renders snippets
and citations; rewriting a classical source would be falsification. This folds
only what goes into an index or an embedding.

## Why a character table instead of calling zhconv on whole strings

`citation.build_snippet` locates highlight spans by `str.find` and returns
offsets into the raw snippet. If normalization could change a string's length,
those offsets would silently drift. Building a strictly 1:1 character table
makes `len(normalize(s)) == len(s)` true by construction, so offsets stay valid
and highlights can be matched against a folded copy of the original.

The table is derived from zhconv's `zh-hans` mapping, which is character-level.
`zh-cn` is deliberately NOT used: it also substitutes mainland vocabulary
(軟體 → 软件), and rewriting the wording of a classical text is not our business.
"""

from __future__ import annotations

import zhconv

from .config import get_settings

# Codepoint ranges scanned to build the fold table: CJK Ext-A, the main CJK
# block, and CJK Compatibility Ideographs.
_CJK_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF))

# Characters that must never fold, because the domain gives the two forms
# different meanings. zhconv maps 乾 → 干 on the "dry" reading, but in this
# corpus 乾 is always the trigram/hexagram (乾坤 2,749× · 乾卦 · 乾元 · 乾道,
# appearing beside 巽/艮/坤). Folding it would merge 乾坤 with 干支 - two core
# and unrelated concepts. Verified against the corpus, not assumed.
_PROTECTED = frozenset("乾")

# Old glyph forms zhconv's table does not carry. Seeded from the corpus; extend
# as more are found. Keys must be single characters and values single
# characters, so the 1:1 guarantee holds.
_EXTRA: dict[str, str] = {
    "巻": "卷",
    "歳": "岁",
    "冩": "写",
    "毎": "每",
}


def _build_table() -> dict[int, str]:
    table: dict[int, str] = {}
    for low, high in _CJK_RANGES:
        for codepoint in range(low, high + 1):
            char = chr(codepoint)
            if char in _PROTECTED:
                continue
            folded = zhconv.convert(char, "zh-hans")
            # Skip anything that is not a clean 1:1 substitution; the offset
            # guarantee matters more than covering an exotic mapping.
            if folded != char and len(folded) == 1:
                table[codepoint] = folded
    for source, target in _EXTRA.items():
        if source in _PROTECTED:
            continue
        table[ord(source)] = target
    return table


_TABLE = _build_table()


def normalize(text: str) -> str:
    """Fold traditional and old glyph forms so variants of a word match.

    Length-preserving: every mapping is one character to one character.
    """
    if not text or not get_settings().normalize_cjk:
        return text
    return text.translate(_TABLE)


def fold_table_size() -> int:
    """Exposed so a test can notice if the upstream table ever collapses."""
    return len(_TABLE)
