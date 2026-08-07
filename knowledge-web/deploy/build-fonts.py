#!/usr/bin/env python3
"""Build self-hosted, subset CJK webfonts for kbweb.

Why this exists
---------------
tokens.css names Songti SC / SimSun / Noto Serif CJK SC and hopes the machine
has one of them. macOS does. Windows falls to SimSun, whose bitmap-era look
works against the 古籍善本 direction. Linux usually falls all the way through
to Georgia and then to whatever CJK face the browser picks. The whole visual
direction rests on the typeface, so leaving it to the client's font menu
undoes the design.

Both faces here are OFL-1.1 and may be embedded and served freely:

    song  Noto Serif SC   - the corpus
    kai   LXGW WenKai     - titles and annotation

A full CJK serif is ~25MB, which is not shippable. cn-font-split slices each
face into unicode-range shards, so a browser downloads only the shards holding
glyphs the page actually renders - a few dozen KB for a page of 文言文.

Boundaries
----------
This runs at deploy time. It is not imported by the app, not part of
`flask run`, and adds no runtime dependency; npx is used once here and nothing
from npm is committed. If this script never runs, tokens.css falls through to
the local font stack and kbweb renders exactly as it did before - the webfont
stylesheet is only linked when the shards are actually present.

Usage
-----
    python deploy/build-fonts.py             # fetch and split both faces
    python deploy/build-fonts.py --check     # report what is present
    python deploy/build-fonts.py --face kai  # rebuild one face
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parent.parent
STATIC = WEB_ROOT / "kbweb" / "static"
FONT_DIR = STATIC / "fonts"
CACHE_DIR = FONT_DIR / ".src"

# The CSS filename cn-font-split writes into each output directory.
RESULT_CSS = "result.css"


@dataclass(frozen=True)
class Face:
    key: str
    family: str
    url: str
    filename: str
    note: str


FACES: tuple[Face, ...] = (
    Face(
        key="song",
        family="Noto Serif SC",
        url="https://github.com/google/fonts/raw/main/ofl/notoserifsc/NotoSerifSC%5Bwght%5D.ttf",
        filename="NotoSerifSC.ttf",
        note="corpus body text",
    ),
    Face(
        key="kai",
        family="LXGW WenKai",
        url=(
            "https://github.com/lxgw/LxgwWenKai/releases/download/"
            "v1.520/LXGWWenKai-Regular.ttf"
        ),
        filename="LXGWWenKai-Regular.ttf",
        note="titles and annotation",
    ),
)


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024 or unit == "MB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}MB"


def download(face: Face) -> Path:
    """Fetch the source face, reusing the cached copy when one exists."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = CACHE_DIR / face.filename
    if target.exists() and target.stat().st_size > 0:
        print(f"  cached  {face.filename} ({_human(target.stat().st_size)})")
        return target

    print(f"  fetch   {face.url}")
    partial = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(face.url) as response, partial.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    # Rename only after a complete read, so an interrupted run does not leave
    # a truncated file that the next run would happily treat as cached.
    partial.replace(target)
    print(f"  got     {face.filename} ({_human(target.stat().st_size)})")
    return target


def _clear(out_dir: Path, attempts: int = 5) -> None:
    """Remove a face's output directory, retrying briefly.

    On Windows anything holding a handle on the directory - a shell sitting in
    it, an indexer, the previous cn-font-split process still unwinding - makes
    rmtree raise WinError 32 for a moment. Retrying beats failing the build,
    but a handle that never clears is still an error worth surfacing.
    """
    for attempt in range(attempts):
        if not out_dir.exists():
            return
        try:
            shutil.rmtree(out_dir)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise SystemExit(
                    f"cannot clear {out_dir}: something is holding it open. "
                    "Close any shell or editor sitting in that directory and retry."
                ) from None
            time.sleep(0.5)


def split(face: Face, source: Path) -> None:
    """Slice one face into unicode-range shards via cn-font-split."""
    out_dir = FONT_DIR / face.key
    _clear(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    npx = shutil.which("npx")
    if npx is None:
        raise SystemExit(
            "npx not found. cn-font-split is a build-time tool only; install "
            "Node.js to run this script, or ship without webfonts (kbweb "
            "falls back to the local font stack)."
        )

    command = [
        npx,
        "--yes",
        "cn-font-split@latest",
        "run",
        "-i", str(source),
        "-o", str(out_dir),
        "--css.fontFamily", face.family,
        "--css.fontDisplay", "swap",
        "--testHtml", "false",
        "--reporter", "false",
    ]
    print(f"  split   {face.family} -> static/fonts/{face.key}/")
    result = subprocess.run(command, capture_output=True, text=True)

    # cn-font-split 7.4 writes every shard and the manifest, then dies with an
    # access violation (0xC0000005) tearing down its wasm runtime on Windows.
    # The exit code is therefore not evidence either way, so the artifacts are
    # verified directly below. A genuine failure still fails: it leaves the
    # manifest missing or its references dangling.
    try:
        shards, total = verify(out_dir)
    except SystemExit:
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip().splitlines()[-15:]
            print(
                f"  cn-font-split exited {result.returncode}; output is unusable:\n"
                + "\n".join(f"    {line}" for line in tail),
                file=sys.stderr,
            )
        raise

    if result.returncode != 0:
        print(
            f"  note    cn-font-split exited {result.returncode} after writing "
            f"complete output; artifacts verified, continuing"
        )
    print(f"  done    {shards} shards, {_human(total)} total")


def verify(out_dir: Path) -> tuple[int, int]:
    """Check a face's output stands on its own. Returns (shard count, bytes).

    Trusting the exit code is not an option here (see split), so this asserts
    what actually matters: a manifest exists, every URL it names resolves, and
    each target really is a woff2 rather than a truncated download.
    """
    css = out_dir / RESULT_CSS
    if not css.exists():
        raise SystemExit(f"no {RESULT_CSS} in {out_dir} - the split produced nothing usable")

    manifest = css.read_text(encoding="utf-8")
    faces = manifest.count("@font-face")
    refs = re.findall(r'url\(["\']?\./([^"\')]+)', manifest)
    if not faces or not refs:
        raise SystemExit(f"{css} declares no @font-face rules")

    missing = [name for name in refs if not (out_dir / name).exists()]
    if missing:
        raise SystemExit(
            f"{css} references {len(missing)} missing shard(s), e.g. {missing[0]}"
        )

    corrupt = [name for name in refs if (out_dir / name).read_bytes()[:4] != b"wOF2"]
    if corrupt:
        raise SystemExit(
            f"{len(corrupt)} shard(s) are not valid woff2, e.g. {corrupt[0]}"
        )

    return len(refs), sum((out_dir / name).stat().st_size for name in refs)


def write_stylesheet(faces: tuple[Face, ...]) -> None:
    """Emit the one stylesheet the template links, importing each face."""
    lines = [
        "/* Generated by deploy/build-fonts.py - do not edit.",
        " *",
        " * Each import is a cn-font-split manifest of unicode-range @font-face",
        " * rules; the browser fetches only the shards a page actually needs.",
        " * Linked from base.html only when these files exist, so a deployment",
        " * that skips the font build serves no broken references. */",
    ]
    for face in faces:
        lines.append(f'@import url("../fonts/{face.key}/{RESULT_CSS}");  /* {face.note} */')
    (STATIC / "css" / "fonts.css").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("  wrote   static/css/fonts.css")


def check() -> int:
    """Report which faces are built. Exit non-zero when any is missing."""
    missing = 0
    for face in FACES:
        try:
            shards, total = verify(FONT_DIR / face.key)
        except SystemExit as exc:
            missing += 1
            print(f"  absent  {face.key:5} {face.family} - {exc}")
        else:
            print(f"  ok      {face.key:5} {face.family} - {shards} shards, {_human(total)}")
    stylesheet = STATIC / "css" / "fonts.css"
    print(f"  {'ok     ' if stylesheet.exists() else 'absent '} static/css/fonts.css")
    return 1 if missing else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report state and exit")
    parser.add_argument(
        "--face",
        choices=[face.key for face in FACES],
        help="build only this face (default: all)",
    )
    parser.add_argument(
        "--keep-sources",
        action="store_true",
        help="keep the downloaded .ttf files under static/fonts/.src",
    )
    args = parser.parse_args()

    if args.check:
        return check()

    selected = tuple(f for f in FACES if args.face is None or f.key == args.face)
    for face in selected:
        print(f"{face.key}: {face.family} ({face.note})")
        split(face, download(face))

    write_stylesheet(FACES)

    if not args.keep_sources and CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)
        print("  cleaned static/fonts/.src")

    print("\nDone. Restart kbweb to pick up the stylesheet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
