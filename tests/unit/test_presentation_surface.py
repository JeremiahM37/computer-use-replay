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


def test_execution_caption_resolves_live_controls_and_missing_target(binding):
    """Live steps have no authored key; diagnostics still need a useful caption."""
    from types import SimpleNamespace

    from computer_use_replay.engine import Execution
    from computer_use_replay.policy import Policy

    surface = SimpleNamespace(_live_controls={"live_observed": binding.controls["search"]})
    execution = Execution(surface, Policy(binding, "http://localhost"), None, None)
    assert execution._control_label("live_observed") == "Find member"
    assert execution._control_label("live_expired") == "live_expired"


async def test_unknown_live_target_is_absent_instead_of_raising_key_error(binding):
    from computer_use_replay.policy import Policy

    surface = BrowserSurface(Policy(binding, "http://localhost"), None, None)
    assert await surface.count("live_expired") == 0


async def test_pin_rejects_stale_selection_and_exhausted_budget(binding):
    import pytest

    from computer_use_replay.policy import Policy, Stop

    surface = BrowserSurface(Policy(binding, "http://localhost"), None, None)
    with pytest.raises(Stop, match="live_candidate_stale"):
        await surface.pin_live_candidate("live_absent")
    surface._live_current_ids = {"live_new"}
    surface._live_controls = {"live_new": binding.controls["search"]}
    surface._live_history = {f"live_{i}": None for i in range(binding.max_steps)}
    with pytest.raises(Stop, match="live_candidate_limit"):
        await surface.pin_live_candidate("live_new")
    assert not surface._live_pinned_handles


async def test_pin_rejects_element_that_disappears_after_unique_match(binding, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import pytest

    from computer_use_replay.policy import Policy, Stop

    surface = BrowserSurface(Policy(binding, "http://localhost"), None, None)
    surface._live_current_ids = {"live_selected"}
    surface._live_controls = {"live_selected": binding.controls["search"]}
    surface._live_scopes = {"live_selected": ("lookup", "reviewed_static", "role", "button", ())}
    locator = SimpleNamespace(
        count=AsyncMock(return_value=1), element_handle=AsyncMock(return_value=None)
    )
    monkeypatch.setattr(surface, "_locator", lambda _target: locator)
    with pytest.raises(Stop, match="live_candidate_stale"):
        await surface.pin_live_candidate("live_selected")
    assert not surface._live_pinned_handles
