"""Presentation caption/state bookkeeping that needs no real browser."""

from computer_use_replay.browser import BrowserSurface


def test_caption_prefix_before_any_step_is_capability_only():
    surface = BrowserSurface(None, None, None)
    assert surface._caption_prefix() == [""]
    surface.presentation_context("read_savings")
    assert surface._caption_prefix() == ["read_savings"]


def test_caption_prefix_with_total_shows_step_n_of_n():
    surface = BrowserSurface(None, None, None)
    surface.presentation_context("read_savings", 6)
    surface.presentation_step(0)
    assert surface._caption_prefix() == ["read_savings", "step 1/6"]
    surface.presentation_step(5)
    assert surface._caption_prefix() == ["read_savings", "step 6/6"]


def test_caption_prefix_without_total_shows_step_n_only():
    surface = BrowserSurface(None, None, None)
    surface.presentation_context("read_savings")
    surface.presentation_step(3)
    assert surface._caption_prefix() == ["read_savings", "step 4"]


def test_note_decision_is_one_shot():
    surface = BrowserSurface(None, None, None)
    assert surface._decision_note is None
    surface.note_decision("model chose: click View accounts (call 4)")
    assert surface._decision_note == "model chose: click View accounts (call 4)"


def test_present_and_pace_defaults():
    surface = BrowserSurface(None, None, None)
    assert surface.present is False
    assert surface.pace == 0.9
    surface = BrowserSurface(None, None, None, present=True, pace=0.05)
    assert surface.present is True
    assert surface.pace == 0.05
