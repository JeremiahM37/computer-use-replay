"""Defensive browser observation and dispatch checks."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from computer_use_replay.browser_perception import LivePerception
from computer_use_replay.contracts import (
    Target,
)
from computer_use_replay.policy import Binding, Stop


class _ProbeLocator:
    def __init__(self, count=1, valid=True, same=False):
        self._count, self.valid, self.same = count, valid, same

    async def count(self):
        return self._count

    async def evaluate(self, _script, *_args):
        if "el === other" in _script:
            return self.same
        return self.valid

    async def element_handle(self):
        return object()


def _probe_surface(binding, *, metadata=("lookup", "source", "css", "field", ())):
    target = Target(kind="css", name='form [name="member_id"]', frames=("workbench",))
    control = SimpleNamespace(target=target, operations=("fill",))
    return SimpleNamespace(
        policy=SimpleNamespace(binding=binding),
        _live_controls={"candidate": control},
        _live_scopes={"candidate": metadata},
        _live_current_ids={"candidate"},
        _live_pinned_handles={},
        _replay_groundings={},
        _locator=lambda *_args, **_kwargs: _ProbeLocator(),
        _frame=lambda _target: object(),
    )


LIVE = Path("profiles/juniper_live.json")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case, code",
    [
        ("missing", "live_candidate_stale"),
        ("historical", "live_candidate_stale"),
        ("revoked", "action_policy"),
        ("link", "action_policy"),
        ("duplicate", "ambiguous_target"),
        ("frame", "frame_missing"),
        ("changed", "action_policy"),
    ],
)
async def test_live_action_validator_fails_closed_for_dispatch_states(case, code):
    binding = Binding.load(LIVE)
    surface = _probe_surface(binding)
    if case == "missing":
        surface._live_controls.clear()
    elif case == "historical":
        surface._live_current_ids.clear()
    elif case == "revoked":
        surface.policy.binding = binding.model_copy(update={"live_scopes": ()})
    elif case == "link":
        surface._live_scopes["candidate"] = ("lookup", "source", "css", "link", ())
    elif case == "duplicate":
        surface._locator = lambda *_args, **_kwargs: _ProbeLocator(count=2)
    elif case == "frame":
        surface._frame = lambda _target: None
    elif case == "changed":
        surface._locator = lambda *_args, **_kwargs: _ProbeLocator(valid=False)
    with pytest.raises(Stop, match=code):
        await LivePerception(surface).validate_action("candidate", "fill")


@pytest.mark.asyncio
async def test_live_action_validator_rejects_operation_not_advertised_by_candidate():
    binding = Binding.load(LIVE)
    surface = _probe_surface(binding)
    with pytest.raises(Stop, match="action_policy"):
        await LivePerception(surface).validate_action("candidate", "click")


@pytest.mark.asyncio
async def test_live_action_validator_preserves_authored_human_control_precedence():
    binding = Binding.load(LIVE)
    surface = _probe_surface(binding)
    surface._live_controls["candidate"] = SimpleNamespace(
        target=binding.controls["finalize"].target,
        operations=("click",),
    )
    surface._live_scopes["candidate"] = ("lookup", "source", "css", "button", ())
    surface._locator = lambda *_args, **_kwargs: _ProbeLocator(same=True)
    with pytest.raises(Stop, match="human_required"):
        await LivePerception(surface).validate_action("candidate", "click")


@pytest.mark.asyncio
async def test_live_action_validator_continues_when_authored_target_differs():
    binding = Binding.load(LIVE)
    surface = _probe_surface(binding)
    surface._live_controls["candidate"] = SimpleNamespace(
        target=binding.controls["finalize"].target,
        operations=("click",),
    )
    surface._live_scopes["candidate"] = ("lookup", "source", "css", "button", ())
    assert await LivePerception(surface).validate_action("candidate", "click") is None


@pytest.mark.asyncio
async def test_live_observer_skips_a_detached_reviewed_frame():
    binding = Binding.load(LIVE)

    class DetachedFrame:
        def is_detached(self):
            return True

    surface = SimpleNamespace(
        page=SimpleNamespace(main_frame=DetachedFrame()),
        policy=SimpleNamespace(binding=binding),
        _live_epoch=0,
        _live_controls={},
        _live_scopes={},
        _live_current_ids=set(),
        _replay_groundings={},
        _live_history={},
        _frame=lambda _target: DetachedFrame(),
    )
    assert await LivePerception(surface).observe() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record, operations",
    [
        (
            {
                "index": 0,
                "name": None,
                "role": "link",
                "label": "safe link",
                "ready": True,
                "enabled": True,
                "excluded": False,
                "digest": "A",
            },
            ("click",),
        ),
        (
            {
                "index": 0,
                "name": None,
                "role": "button",
                "label": "ungranted",
                "ready": True,
                "enabled": True,
                "excluded": False,
                "digest": "B",
            },
            ("fill",),
        ),
    ],
)
async def test_live_observer_filters_link_and_ungranted_operation(record, operations):
    binding = Binding.load(LIVE)
    binding = binding.model_copy(
        update={
            "live_scopes": tuple(
                scope.model_copy(update={"operations": operations}) for scope in binding.live_scopes
            )
        }
    )

    class Locator:
        async def count(self):
            return 1

        async def evaluate_all(self, _script, _arg):
            return True if "forms" in _script else [record]

        def locator(self, _selector):
            return self

        def filter(self, **_kwargs):
            return self

    class Frame:
        def is_detached(self):
            return False

        def locator(self, _selector):
            return Locator()

    frame = Frame()
    surface = SimpleNamespace(
        page=SimpleNamespace(main_frame=frame),
        policy=SimpleNamespace(binding=binding),
        _live_epoch=0,
        _live_controls={},
        _live_scopes={},
        _live_current_ids=set(),
        _replay_groundings={},
        _live_history={},
        _arguments={},
        _frame=lambda _target: frame,
    )
    assert await LivePerception(surface).observe() == []
