"""Reproduce live form discovery and changed-input replay with a real model.

Outputs stay in ignored runs/ by default. Only structured events, withheld results,
and executable artifacts are saved; no model transcripts or raw page captures.
"""

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path

from computer_use_replay.browser import BrowserSurface
from computer_use_replay.contracts import GoalRequest
from computer_use_replay.control import Handoff, Ownership
from computer_use_replay.demo import serve_demo
from computer_use_replay.discovery import discover
from computer_use_replay.engine import Execution, Replay
from computer_use_replay.evidence import Evidence, atomic_json
from computer_use_replay.planner import ModelPlanner
from computer_use_replay.policy import Binding, Policy
from computer_use_replay.providers import ProviderConfig

# These labels do not appear in the product profile. The wrapper also changes
# layout without changing form destinations or business success conditions.
VARIANT = """addEventListener('DOMContentLoaded', () => {
  const names = {'Find member':'Search directory', 'Open member':'Inspect member',
    'View accounts':'Browse accounts', 'Open savings ledger':'View savings details'};
  for (const button of document.querySelectorAll('button')) {
    const name = names[button.textContent.trim()];
    if (name) { button.textContent = name; const wrapper = document.createElement('section');
      wrapper.style.padding = '12px'; button.replaceWith(wrapper); wrapper.append(button); }
  }
});"""


def source_hashes():
    paths = [
        *Path("src/computer_use_replay").glob("*.py"),
        Path("profiles/juniper_live.json"),
        Path(__file__),
        Path("requests/read_savings.json"),
        Path("requests/prepare_subaccount.json"),
    ]
    return {
        str(path.relative_to(Path.cwd()) if path.is_absolute() else path): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(paths)
    }


async def capture(args):
    root = Path(args.output)
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=False)
    sources = await asyncio.to_thread(source_hashes)
    binding = Binding.load(Path("profiles/juniper_live.json"))
    config = ProviderConfig.from_env("ollama", args.model, args.model_url)
    rows = []
    for name, variant in (
        ("read_savings", False),
        ("prepare_subaccount", False),
        ("read_savings", True),
    ):
        label = name + ("_relabeled" if variant else "")
        request = GoalRequest.load(Path("requests") / f"{name}.json")
        values, changed = {"member_id": "00123"}, {"member_id": "00456"}
        if name == "prepare_subaccount":
            values["nickname"], changed["nickname"] = "Vacation", "Travel"
        async with serve_demo() as (origin, app):
            evidence = Evidence(root / label, "discovery")
            policy = Policy(binding, origin)
            ownership = Ownership(evidence)
            planner = ModelPlanner(config, evidence)
            decide = planner.decide
            observations = 0

            async def checked_decide(context, decide=decide):
                nonlocal observations
                payload = json.dumps(context)
                for private in ("00123", "1204.57", "Vacation", "SYNTHETIC PERSON", "PRIVATE-NOTE"):
                    assert private not in payload, "Private fixture data entered model context"
                observations += bool(context["observation"].get("live_candidates"))
                return await decide(context)

            planner.decide = checked_decide
            start = time.monotonic()
            async with BrowserSurface(policy, ownership, evidence) as surface:
                if variant:
                    await surface.context.add_init_script(VARIANT)
                artifact, result = await discover(
                    Execution(surface, policy, evidence, Handoff(ownership)),
                    planner,
                    request,
                    values,
                    root / label / "artifact.json",
                )
            discovery_seconds = time.monotonic() - start
            assert artifact is not None and result.status == "success", result
            assert artifact.schema_version == "3.0" and artifact.grounded
            assert planner.calls > 0 and observations > 0
            evidence = Evidence(root / label, "replay")
            policy, ownership = Policy(binding, origin), Ownership(evidence)
            start = time.monotonic()
            async with BrowserSurface(policy, ownership, evidence) as surface:
                if variant:
                    await surface.context.add_init_script(VARIANT)
                result = await Replay(Execution(surface, policy, evidence, Handoff(ownership))).run(
                    artifact, changed
                )
            replay_seconds = time.monotonic() - start
            assert result.status == "success" and result.llm_calls == 0, result
            expected = (
                {"available_balance": {"amount": "8902.10", "currency": "USD"}}
                if name == "read_savings"
                else {"review_status": "Ready for confirmation"}
            )
            assert result.outputs == expected and app.state.finalizations == 0
            row = {
                "case": label,
                "status": "success",
                "discovery_calls": planner.calls,
                "live_observation_turns": observations,
                "grounded_targets": len(artifact.grounded),
                "discovery_seconds": round(discovery_seconds, 3),
                "replay_seconds": round(replay_seconds, 3),
                "replay_calls": 0,
                "changed_inputs_verified": True,
                "private_context_canaries_absent": True,
            }
            rows.append(row)
            atomic_json(root / "summary.json", rows)
            print(json.dumps(row), flush=True)
    assert sources == await asyncio.to_thread(source_hashes), "Source changed during capture"
    atomic_json(
        root / "capture.json",
        {"source_files": sources, "provider": "ollama", "model": config.model},
    )
    print(f"PASS: {len(rows)} genuine live discoveries and changed-input model-free replays")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="runs/live-evidence")
    parser.add_argument("--model", default=None)
    parser.add_argument("--model-url", default=None)
    asyncio.run(capture(parser.parse_args()))


if __name__ == "__main__":
    main()
