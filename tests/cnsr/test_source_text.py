"""The new code's own text: an invisible character in a string literal is written as an escape."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCES = sorted(
    path
    for directory in ("src/cnsr", "tests/cnsr", "tests/worker")
    for path in (ROOT / directory).rglob("*")
    if path.is_file() and path.suffix in (".py", ".sh", ".js", ".mjs", ".json", ".yml")
)


def test_there_are_sources_to_check():
    assert len(SOURCES) > 50


@pytest.mark.parametrize("path", SOURCES, ids=lambda path: str(path.relative_to(ROOT)))
def test_no_literal_byte_order_mark(path):
    assert "\ufeff" not in path.read_text(encoding="utf-8"), "write it as \\ufeff"
