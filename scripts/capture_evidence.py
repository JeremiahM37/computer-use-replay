"""Small, reproducible submission bundle; live discovery is explicit and never scripted."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from computer_use_replay.contracts import Capability, GoalRequest
from computer_use_replay.discovery import discover
from computer_use_replay.engine import Replay
from computer_use_replay.evidence import Event, atomic_json
from computer_use_replay.onboarding import propose
from computer_use_replay.planner import ModelPlanner
from computer_use_replay.providers import ProviderConfig
from computer_use_replay.tour import session, terminal_session


def source_hashes():
    return {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(
            [
                *Path("src/computer_use_replay").glob("*.py"),
                *Path("profiles").glob("*.json"),
                *Path("requests").glob("*.json"),
                Path("scripts/capture_evidence.py"),
                Path("tests/fixtures/legacy_vendor.py"),
            ]
        )
    }


def initialize(root):
    root.mkdir(parents=True, exist_ok=False)
    try:
        source_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        source_commit = None
    report = {
        "schema_version": "2.0",
        "source_commit": source_commit,
        "source_files": source_hashes(),
        "discovery": [],
        "replays": [],
    }
    report["discovery_provenance"] = {
        "source_commit": report["source_commit"],
        "source_files": report["source_files"],
    }
    return report


async def capture(args):
    root = Path(args.output)
    report = await asyncio.to_thread(initialize, root)
    for name in ["read_savings", "prepare_subaccount"]:
        path = root / f"{name}.json"
        if args.discover:
            request = GoalRequest.load(Path(f"requests/{name}.json"))
            values = {"member_id": "00123"}
            if name == "prepare_subaccount":
                values["nickname"] = "Rainy day"
            async with session(root, "discovery_" + name) as (ex, app):
                planner = ModelPlanner(
                    ProviderConfig.from_env("ollama", args.model, args.model_url), ex.evidence
                )
                artifact, result = await discover(ex, planner, request, values, path)
                report["discovery"].append(
                    {
                        "run": ex.evidence.run_id,
                        "capability": name,
                        "status": result.status,
                        "llm_calls": planner.calls,
                    }
                )
                atomic_json(root / "manifest.json", report)
                if artifact is None:
                    raise RuntimeError(
                        f"Discovery failed: {getattr(result, 'code', None) or result.failure.code}; evidence retained at {root}"
                    )
                assert app.state.finalizations == 0
        else:
            artifact = Capability.model_validate_json(
                await asyncio.to_thread((Path(args.artifacts) / f"{name}.json").read_text)
            )
            atomic_json(path, artifact.model_dump(mode="json"))
        artifact = Capability.model_validate_json(await asyncio.to_thread(path.read_text))
        for tenant in ["a", "b"]:
            async with session(
                root,
                f"replay_{name}_{tenant}",
                "tenant_b" if tenant == "b" else "normal",
                tenant_b=tenant == "b",
            ) as (ex, app):
                values = {"member_id": "00456"}
                if name == "prepare_subaccount":
                    values["nickname"] = "New buffer"
                result = await Replay(ex).run(artifact, values)
                expected = (
                    {"available_balance": {"amount": "8902.10", "currency": "USD"}}
                    if name == "read_savings"
                    else {"review_status": "Ready for confirmation"}
                )
                assert result.status == "success" and result.outputs == expected, result
                assert result.llm_calls == 0 and app.state.finalizations == 0
                report["replays"].append(
                    {
                        "run": ex.evidence.run_id,
                        "capability": name,
                        "status": result.status,
                        "tenant": tenant,
                        "outputs_checked": True,
                        "llm_calls": 0,
                    }
                )
    savings = Capability.model_validate_json((root / "read_savings.json").read_text())
    for name, scenario, values, options, status, code in [
        ("not_found", "normal", {"member_id": "00999"}, {}, "business_outcome", "member_not_found"),
        ("expired", "expired", {"member_id": "00123"}, {}, "failure", "operator_unavailable"),
        ("human_resume", "expired", {"member_id": "00123"}, {"human": True}, "success", None),
        (
            "label_drift",
            "normal",
            {"member_id": "00123"},
            {"drift": True},
            "failure",
            "checkpoint_failed",
        ),
        ("invalid_input", "normal", {"member_id": 123}, {}, "failure", "invalid_input"),
    ]:
        async with session(root, "replay_" + name, scenario, **options) as (ex, app):
            result = await Replay(ex).run(savings, values)
            assert result.status == status, result
            actual_code = (
                result.failure.code if status == "failure" else getattr(result, "code", None)
            )
            assert actual_code == code and result.llm_calls == 0 and app.state.finalizations == 0
            report["replays"].append(
                {
                    "run": ex.evidence.run_id,
                    "capability": "read_savings",
                    "status": status,
                    "code": code,
                    "llm_calls": 0,
                    "operator": "scripted_same_session" if options.get("human") else None,
                }
            )
    report["files"] = await asyncio.to_thread(bundle_hashes, root)
    atomic_json(root / "manifest.json", report)
    print(
        f"PASS: {len(report['discovery'])} genuine discoveries; {len(report['replays'])} model-free replays. {root}"
    )


def bundle_hashes(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != "manifest.json"
    }


# The exact source scope scripts/preflight.py:check_evidence verifies the tracked
# submission bundle against. Kept separate from source_hashes() above, which also
# hashes the small set of scripts/tests that a *genuine* discovery run itself depends on.
SUBMISSION_SOURCE_GLOBS = ("src/computer_use_replay/*.py", "profiles/*.json", "requests/*.json")


def submission_source_hashes():
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for pattern in SUBMISSION_SOURCE_GLOBS
        for path in Path().glob(pattern)
    }


def current_commit():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


async def extend_submission(args):
    """Add a business-outcome, a same-session-handoff, a tenant-B drift run, a
    text-terminal replay on the second surface, a verified-fallback-ladder
    rescue and a recaptured expired-session failure to the tracked evidence/
    bundle, replaying the already-committed read_savings.json artifact (zero
    model calls). The genuine discovery/replay runs and capabilities/*.json
    bytes are untouched; failure/ is recaptured (not left as a genuine
    discovery recording) so its masked screenshot reflects the current masking
    CSS in browser.py:failure_screenshot.
    """
    root = Path(args.output)
    manifest = json.loads((root / "manifest.json").read_text())
    savings = Capability.model_validate_json((root / "read_savings.json").read_text())

    for name in ("business_outcome", "handoff", "tenant_b", "terminal", "failure", "fallback"):
        directory = root / name
        if directory.exists():
            shutil.rmtree(directory)

    async with session(root, "business_outcome") as (ex, app):
        result = await Replay(ex).run(savings, {"member_id": "00999"})
        assert result.status == "business_outcome" and result.code == "member_not_found", result
        assert result.llm_calls == 0 and app.state.finalizations == 0
        manifest["runs"]["business_outcome"] = {
            "run_id": ex.evidence.run_id,
            "status": result.status,
            "code": result.code,
            "llm_calls": 0,
        }

    # Same live session as the failure/ run (expired, no operator): here a scripted
    # operator claims the paused lease, performs the real renewal in the browser, and
    # automation resumes and completes. It is an agent operator, not a person -- see
    # evidence/README.md.
    async with session(root, "handoff", "expired", human=True) as (ex, app):
        result = await Replay(ex).run(savings, {"member_id": "00123"})
        assert result.status == "success", result
        assert result.llm_calls == 0 and app.state.finalizations == 0
        manifest["runs"]["handoff"] = {
            "run_id": ex.evidence.run_id,
            "status": result.status,
            "llm_calls": 0,
        }

    # Same saved capability, same reviewed product policy, tenant B's small
    # presentation overlay only (relabeled search/accounts/balance controls, the
    # "workbench" frame renamed to "operations"). Demonstrates multi-tenant reuse
    # and the presentation_drift signal on an otherwise ordinary success.
    async with session(root, "tenant_b", "tenant_b", tenant_b=True) as (ex, app):
        result = await Replay(ex).run(savings, {"member_id": "00456"})
        expected = {"available_balance": {"amount": "8902.10", "currency": "USD"}}
        assert result.status == "success" and result.outputs == expected, result
        assert result.llm_calls == 0 and app.state.finalizations == 0
        events = [
            json.loads(line)
            for line in (ex.evidence.directory / "events.jsonl").read_text().splitlines()
        ]
        drift_rows = [row for row in events if row["event"] == "presentation_drift"]
        assert len(drift_rows) == 1, drift_rows
        assert drift_rows[0]["count"] == len(drift_rows[0]["targets"]) > 0
        assert sorted(drift_rows[0]["targets"]) == sorted(savings.targets)
        manifest["runs"]["tenant_b"] = {
            "run_id": ex.evidence.run_id,
            "status": result.status,
            "llm_calls": 0,
        }

    # Same saved capability, same reviewed product policy, the second surface
    # type (REPORT.md §4): a text-terminal screen buffer instead of a browser
    # page, through profiles/terminal.json. No Playwright, no fixture server.
    async with terminal_session(root, "terminal") as (ex, app):
        result = await Replay(ex).run(savings, {"member_id": "00123"})
        expected = {"available_balance": {"amount": "1204.57", "currency": "USD"}}
        assert result.status == "success" and result.outputs == expected, result
        assert result.llm_calls == 0
        events = [
            json.loads(line)
            for line in (ex.evidence.directory / "events.jsonl").read_text().splitlines()
        ]
        drift_rows = [row for row in events if row["event"] == "presentation_drift"]
        assert len(drift_rows) == 1, drift_rows
        assert drift_rows[0]["count"] == len(drift_rows[0]["targets"]) > 0
        assert sorted(drift_rows[0]["targets"]) == sorted(savings.targets)
        manifest["runs"]["terminal"] = {
            "run_id": ex.evidence.run_id,
            "status": result.status,
            "llm_calls": 0,
        }

    # No overlay, no reviewed alternate: a decoration-only, unreviewed relabel
    # ("Find member" -> "FIND MEMBER...") that the verified fallback ladder's
    # `normalized` rung -- the ONLY rung that ever acts -- rescues on its own,
    # at acting time -- see REPORT.md §2-4 and docs/DESIGN_CHOICES.md.
    # Distinct from tenant_b/ above, whose relabel is resolved through a
    # reviewed alternate, not this ladder.
    async with session(root, "fallback", "normal", minor_relabel=True) as (ex, app):
        result = await Replay(ex).run(savings, {"member_id": "00123"})
        expected = {"available_balance": {"amount": "1204.57", "currency": "USD"}}
        assert result.status == "success" and result.outputs == expected, result
        assert result.llm_calls == 0 and app.state.finalizations == 0
        events = [
            json.loads(line)
            for line in (ex.evidence.directory / "events.jsonl").read_text().splitlines()
        ]
        resolved = [row for row in events if row["event"] == "fallback_resolved"]
        assert len(resolved) == 1 and resolved[0]["target"] == "search", resolved
        assert resolved[0]["rung"] == "normalized"
        assert result.warnings == (f"fallback_resolved:search:{resolved[0]['rung']}",)
        review_path = ex.evidence.directory / "fallback_review.json"
        review = json.loads(review_path.read_text())
        # Only the REVIEWED name may appear here -- never the observed "FIND MEMBER..." text.
        assert review["search"]["target"]["name"] == "Find member"
        assert "FIND MEMBER" not in review_path.read_text()
        manifest["runs"]["fallback"] = {
            "run_id": ex.evidence.run_id,
            "status": result.status,
            "llm_calls": 0,
            "rung": resolved[0]["rung"],
        }

    # Same "expired" scenario as the original failure/ run, no operator attached --
    # recaptured (never a genuine discovery) purely so its masked screenshot reflects
    # the current masking CSS. handoff/ above is the same story's resumed half, on a
    # separate live session.
    async with session(root, "failure", "expired") as (ex, app):
        result = await Replay(ex).run(savings, {"member_id": "00123"})
        assert result.status == "failure" and result.failure.code == "operator_unavailable", result
        assert result.failure.intervention_id and result.failure.evidence
        assert result.failure.screenshot
        assert result.llm_calls == 0 and app.state.finalizations == 0
        manifest["runs"]["failure"] = {
            "run_id": ex.evidence.run_id,
            "status": result.status,
            "llm_calls": 0,
        }

    manifest["source_commit"] = current_commit()
    manifest["source_files"] = submission_source_hashes()
    # README.md documents the bundle; like manifest.json itself it is not a hashed entry.
    manifest["files"] = {k: v for k, v in bundle_hashes(root).items() if k != "README.md"}
    atomic_json(root / "manifest.json", manifest)
    print(
        f"PASS: extended {root} with business_outcome/, handoff/, tenant_b/, terminal/, "
        "fallback/ and recaptured failure/ runs"
    )


async def capture_goal_only(args):
    """Adds evidence/goal_only/ to the tracked bundle: one genuine `propose()` call
    over the reviewed product vocabulary, an explicit acceptance (the same
    `--accept-draft` path the CLI offers), and one genuine `discover()` run --
    goal-only onboarding end to end, with real model calls throughout. Unlike
    extend_submission()'s recaptures, this cannot run scripted -- it needs a real
    model endpoint (OLLAMA_URL/OLLAMA_MODEL, or --model/--model-url).
    """
    root = Path(args.output)
    directory = root / "goal_only"
    if directory.exists():
        shutil.rmtree(directory)
    artifact_path = root / "goal_only.json"
    if artifact_path.exists():
        artifact_path.unlink()

    config = ProviderConfig.from_env("ollama", args.model, args.model_url)
    async with session(root, "goal_only") as (ex, app):
        request, proposal_calls = await propose(
            config,
            ex.evidence,
            ex.policy.binding,
            "Look up the requested member and read their current available savings balance.",
        )
        atomic_json(ex.evidence.directory / "request.json", request.model_dump(mode="json"))
        # Same acceptance path the CLI's --accept-draft flag records.
        ex.evidence.emit(Event(event="contract_accepted", capability=request.name, code="flag"))
        planner = ModelPlanner(config, ex.evidence)
        artifact, result = await discover(
            ex,
            planner,
            request,
            {"member_id": "00123"},
            artifact_path,
            accepted_by="flag",
        )
        assert result.status == "success" and artifact is not None, result
        assert result.llm_calls == planner.calls > 0
        assert app.state.finalizations == 0

    manifest = json.loads((root / "manifest.json").read_text())
    manifest["runs"]["goal_only"] = {
        "run_id": ex.evidence.run_id,
        "status": result.status,
        "llm_calls": planner.calls,
        "proposal_calls": proposal_calls,
        "capability": request.name,
        "model_ref": request.review.proposed_by,
    }
    manifest["source_commit"] = current_commit()
    manifest["source_files"] = submission_source_hashes()
    manifest["files"] = {k: v for k, v in bundle_hashes(root).items() if k != "README.md"}
    atomic_json(root / "manifest.json", manifest)
    print(
        f"PASS: added goal_only/ -- '{request.name}' proposed then discovered genuinely "
        f"({proposal_calls} proposal call, {planner.calls} discovery calls)"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="runs/capture-evidence",
        help="Private output directory (defaults to runs/capture-evidence)",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Make genuine Ollama calls for both goals; otherwise use committed artifacts",
    )
    parser.add_argument("--model")
    parser.add_argument(
        "--artifacts", default="capabilities", help="Directory of saved capability artifacts"
    )
    parser.add_argument("--model-url")
    parser.add_argument(
        "--extend-submission",
        action="store_true",
        help=(
            "Instead of a fresh private capture, add business_outcome/ and handoff/ runs "
            "to the tracked evidence/ bundle at --output (default evidence), leaving the "
            "existing discovery/replay/failure runs untouched"
        ),
    )
    parser.add_argument(
        "--goal-only",
        action="store_true",
        help=(
            "Add/refresh evidence/goal_only/: one genuine propose() + accept + discover() "
            "run over goal-only onboarding. Requires a real model endpoint; combine with "
            "--model/--model-url or OLLAMA_URL/OLLAMA_MODEL"
        ),
    )
    args = parser.parse_args()
    if args.goal_only:
        if args.output == "runs/capture-evidence":
            args.output = "evidence"
        asyncio.run(capture_goal_only(args))
    elif args.extend_submission:
        if args.output == "runs/capture-evidence":
            args.output = "evidence"
        asyncio.run(extend_submission(args))
    else:
        asyncio.run(capture(args))


if __name__ == "__main__":
    main()
