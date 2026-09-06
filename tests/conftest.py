"""Shared test setup: never let a test touch the user's real recents file."""

import pytest


@pytest.fixture(autouse=True)
def _private_recents(tmp_path, monkeypatch):
    import hwgenie.grade_gui as gg
    monkeypatch.setattr(gg, "RECENTS_PATH", tmp_path / "_recents.json")
