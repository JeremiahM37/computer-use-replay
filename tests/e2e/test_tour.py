"""`computer-use-replay demo`'s shared harness: the eight-scene tour, --discover, and the
session() scenario helper scripts/capture_evidence.py now imports from here.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from computer_use_replay.contracts import Capability
from computer_use_replay.tour import render_table, run_demo_cli, run_tour, session, terminal_session
from tests.support.planner import ScriptedPlanner, decisions

COMMITTED_READ_SAVINGS = Capability.model_validate_json(
    Path("capabilities/read_savings.json").read_text()
)


async def test_run_tour_replays_all_eight_scenes_zero_model_calls(tmp_path):
    report = await run_tour(tmp_path)
    assert report.ok
    assert [step.id for step in report.steps] == ["a", "b", "c", "d", "e", "f", "g", "h"]
    assert all(step.model_calls == 0 for step in report.steps)
    assert report.discovery is None
    by_id = {step.id: step for step in report.steps}
    assert by_id["a"].status == "success"
    assert by_id["b"].status == "success"
    assert by_id["c"].status == "business_outcome"
    assert by_id["d"].status == "success"
    assert by_id["e"].status == "success" and "presentation_drift" in by_id["e"].detail
    assert by_id["f"].capability == "prepare_subaccount"
    # Same read_savings artifact, the second surface (REPORT.md §4): also success,
    # also a presentation_drift signal (a text-terminal screen has no browser roles).
    assert by_id["g"].capability == "read_savings"
    assert by_id["g"].status == "success" and "presentation_drift" in by_id["g"].detail
    # The verified fallback ladder's own scene: a decoration-only, unreviewed
    # relabel with no overlay, rescued by the `normalized` rung (the ONLY rung
    # that ever acts) -- never a reviewed alternate.
    assert by_id["h"].capability == "read_savings"
    assert by_id["h"].status == "success" and "fallback_resolved" in by_id["h"].detail


async def test_run_tour_json_and_table_shapes(tmp_path):
    report = await run_tour(tmp_path)
    payload = report.to_json()
    json.dumps(payload)  # must be plain JSON-serializable
    assert payload["ok"] is True
    assert len(payload["steps"]) == 8
    table = render_table(report)
    assert "step" in table and "model calls" in table
    for step in report.steps:
        assert step.id in table


async def test_run_tour_reports_a_failed_step_without_raising(tmp_path, monkeypatch):
    from computer_use_replay.engine import Replay

    original_run = Replay.run
    calls = {"n": 0}

    async def flaky_run(self, artifact, arguments):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("synthetic failure for step a")
        return await original_run(self, artifact, arguments)

    monkeypatch.setattr(Replay, "run", flaky_run)
    report = await run_tour(tmp_path)
    assert not report.ok
    assert report.steps[0].ok is False
    assert "synthetic failure" in report.steps[0].detail
    assert report.steps[0].status == "error"
    assert report.steps[0].model_calls == 0


async def test_run_tour_with_scripted_discovery_uses_learned_artifact(tmp_path):
    def factory(evidence):
        return ScriptedPlanner(decisions(COMMITTED_READ_SAVINGS))

    report = await run_tour(tmp_path, discover=True, planner_factory=factory)
    assert report.ok
    assert report.discovery is not None
    assert report.discovery["model"] == "test_script"
    assert report.discovery["calls"] == 0  # ScriptedPlanner never increments its own counter
    assert len(report.discovery["steps"]) == len(COMMITTED_READ_SAVINGS.steps)
    assert (tmp_path / "learned_read_savings.json").exists()


async def test_run_tour_discover_raises_loudly_on_genuine_discovery_failure(tmp_path):
    from computer_use_replay.planner import Decision

    def factory(evidence):
        return ScriptedPlanner([Decision(op="stop", reason="cannot_proceed")])

    with pytest.raises(RuntimeError, match="Discovery failed"):
        await run_tour(tmp_path, discover=True, planner_factory=factory)


async def test_session_drift_renames_the_reviewed_search_button(tmp_path):
    async with session(tmp_path, "drift_probe", drift=True) as (ex, _app):
        await ex.surface.navigate()
        assert await ex.surface.count("search") == 0


async def test_session_fallback_override_reaches_the_binding(tmp_path):
    async with session(tmp_path, "fallback_off_probe", fallback="off") as (ex, _app):
        assert ex.policy.binding.fallback == "off"


async def test_terminal_session_fallback_override_reaches_the_binding(tmp_path):
    async with terminal_session(tmp_path, "terminal_fallback_off_probe", fallback="off") as (
        ex,
        _app,
    ):
        assert ex.policy.binding.fallback == "off"


async def test_run_demo_cli_prints_table_and_json(tmp_path, capsys):
    args = SimpleNamespace(
        discover=False, model=None, model_url=None, present=False, pace=0.9, evidence=str(tmp_path)
    )
    code = await run_demo_cli(args)
    assert code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert "step" in captured.err


def test_select_provider_uses_ollama_by_default(monkeypatch):
    from computer_use_replay.tour import _select_provider

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_MODEL", "qwen-test")
    config = _select_provider(SimpleNamespace(model=None, model_url=None))
    assert config.provider == "ollama"
    assert config.model == "qwen-test"


def test_select_provider_uses_openai_when_key_and_model_given(monkeypatch):
    from computer_use_replay.tour import _select_provider

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    config = _select_provider(SimpleNamespace(model="gpt-test", model_url=None))
    assert config.provider == "openai"
    assert config.model == "gpt-test"


def test_select_provider_stays_on_ollama_without_a_model_even_with_a_key(monkeypatch):
    from computer_use_replay.tour import _select_provider

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen-test")
    config = _select_provider(SimpleNamespace(model=None, model_url=None))
    assert config.provider == "ollama"


async def test_run_demo_cli_discover_wires_the_real_tour_end_to_end(tmp_path, monkeypatch):
    import computer_use_replay.planner as planner_module

    class FakePlanner(ScriptedPlanner):
        def __init__(self, config, evidence):
            super().__init__(decisions(COMMITTED_READ_SAVINGS))
            self.config = config

    monkeypatch.setattr(planner_module, "ModelPlanner", FakePlanner)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_MODEL", "qwen-test")
    args = SimpleNamespace(
        discover=True, model=None, model_url=None, present=False, pace=0.9, evidence=str(tmp_path)
    )
    code = await run_demo_cli(args)
    assert code == 0
