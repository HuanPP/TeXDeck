"""Learn: parts of the notes marked as learnt."""

from __future__ import annotations

from pathlib import Path

from app import progress


def test_progress_round_trips_without_duplicates_or_blanks(tmp_path: Path) -> None:
    assert progress.load(tmp_path) == {"done": []}
    assert progress.save(tmp_path, ["A", "B", "A", " "]) == {"done": ["A", "B"]}
    assert progress.load(tmp_path) == {"done": ["A", "B"]}


def test_a_broken_file_reads_as_nothing_learnt(tmp_path: Path) -> None:
    (tmp_path / progress.PROGRESS_FILE).write_text("{", encoding="utf-8")
    assert progress.load(tmp_path) == {"done": []}
