"""Check source contracts and the assignment-required, sanitized evidence bundle."""

import hashlib
import json
import subprocess
from pathlib import Path

from computer_use_replay.contracts import Capability, Failure
from computer_use_replay.evidence import Event, Snapshot
from computer_use_replay.policy import Binding, Policy


def check_git_tracked(root):
    """Every path the REAL, committed manifest references must be tracked by git,
    not merely present on this disk -- a file that exists locally but is
    gitignored (e.g. a generated evidence snapshot whose exact filename varies
    between capture runs) passes every other check here yet is silently absent
    from a fresh clone, where preflight would then fail on a missing file
    instead of a tracking gap. Deliberately separate from check_evidence(),
    which is also called against disposable tmp_path copies in tests that must
    not depend on those copies living inside a git work tree. Skips gracefully
    outside a git checkout entirely (a source archive).
    """
    try:
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return
    manifest = json.loads((root / "manifest.json").read_text())
    paths = [root / "manifest.json", root / "README.md", *(root / n for n in manifest["files"])]
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", *(str(p) for p in paths)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Not tracked by git (would be missing from a fresh clone):\n{result.stderr}"
    )


def check_evidence(root):
    """Verify the small submission bundle; unrelated local runs are not release inputs."""
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["schema_version"] == 1
    assert set(manifest["runs"]) == {
        "discovery",
        "replay",
        "failure",
        "business_outcome",
        "handoff",
        "tenant_b",
        "terminal",
        "goal_only",
        "fallback",
    }
    files = manifest["files"]
    required = {
        "read_savings.json",
        "goal_only.json",
        "goal_only/request.json",
        "fallback/fallback_review.json",
    } | {f"{role}/{name}" for role in manifest["runs"] for name in ("events.jsonl", "result.json")}
    assert required <= set(files)
    for name, expected in files.items():
        path = root / name
        assert path.resolve().is_relative_to(root.resolve()), "Evidence path escapes bundle"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, name
    assert {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()} == (
        set(files) | {"README.md", "manifest.json"}
    ), "Unlisted submission material"
    expected_sources = {
        str(path)
        for pattern in ("src/computer_use_replay/*.py", "profiles/*.json", "requests/*.json")
        for path in Path().glob(pattern)
    }
    assert set(manifest["source_files"]) == expected_sources
    for name, expected in manifest["source_files"].items():
        assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == expected, name

    artifact = Capability.model_validate_json((root / "read_savings.json").read_text())
    assert artifact.provenance.mode == "llm" and artifact.provenance.calls > 0
    Policy(Binding.load(Path("profiles/juniper.json")), "http://localhost").check_artifact(artifact)
    discovery = manifest["runs"]["discovery"]
    assert artifact.provenance.run_id == discovery["run_id"]
    assert artifact.provenance.calls == discovery["llm_calls"]

    # A separately, genuinely learned capability from goal-only onboarding: propose ->
    # accept -> discover, in the tracked bundle's own smallest reproducible sample.
    goal_only_artifact = Capability.model_validate_json((root / "goal_only.json").read_text())
    assert goal_only_artifact.provenance.mode == "llm" and goal_only_artifact.provenance.calls > 0
    assert goal_only_artifact.provenance.accepted_by == "flag"
    Policy(Binding.load(Path("profiles/juniper.json")), "http://localhost").check_artifact(
        goal_only_artifact
    )
    goal_only_run = manifest["runs"]["goal_only"]
    assert goal_only_artifact.provenance.run_id == goal_only_run["run_id"]
    assert goal_only_artifact.provenance.calls == goal_only_run["llm_calls"]
    draft = json.loads((root / "goal_only" / "request.json").read_text())
    assert draft["review"]["status"] == "draft" and draft["name"] == goal_only_artifact.name
    assert draft["review"]["proposed_by"] == goal_only_run["model_ref"]

    for role, run in manifest["runs"].items():
        rows = [
            json.loads(line) for line in (root / role / "events.jsonl").read_text().splitlines()
        ]
        assert rows and [row["sequence"] for row in rows] == list(range(1, len(rows) + 1))
        for row in rows:
            Event.model_validate({key: value for key, value in row.items() if key != "time"})
        result = json.loads((root / role / "result.json").read_text())
        assert result["status"] == run["status"]
        assert result["llm_calls"] == run["llm_calls"]
        model_rows = [row for row in rows if row["event"] == "model_response"]
        if role == "discovery":
            assert result["status"] == "success"
            assert len(model_rows) == artifact.provenance.calls
            assert all(row["provider"] == "ollama" for row in model_rows)
            assert any(
                row["event"] == "artifact_saved" and row["artifact_sha256"] == artifact.digest()
                for row in rows
            )
        elif role == "goal_only":
            # One model_response for the proposal, then one per discovery decision --
            # both phases share this one run's evidence, never scripted.
            assert result["status"] == "success"
            assert len(model_rows) == run["proposal_calls"] + goal_only_artifact.provenance.calls
            assert all(row["provider"] == "ollama" for row in model_rows)
            assert any(row["event"] == "contract_proposed" for row in rows)
            proposed = next(row for row in rows if row["event"] == "contract_proposed")
            assert proposed["capability"] == goal_only_artifact.name
            accepted = [row for row in rows if row["event"] == "contract_accepted"]
            assert len(accepted) == 1 and accepted[0]["code"] == "flag"
            assert any(
                row["event"] == "artifact_saved"
                and row["artifact_sha256"] == goal_only_artifact.digest()
                for row in rows
            )
            assert result["outputs"] == {name: "<withheld>" for name in goal_only_artifact.outputs}
            assert any(row["event"] == "success" for row in rows)
        else:
            assert result["llm_calls"] == 0 and not model_rows
            assert not any(row["event"] in {"decision", "model_retry"} for row in rows)
            assert any(
                row["event"] == "started" and row["artifact_sha256"] == artifact.digest()
                for row in rows
            )
        if role in {"discovery", "replay", "handoff", "tenant_b", "terminal", "fallback"}:
            assert result["status"] == "success"
            assert result["outputs"] == {"available_balance": "<withheld>"}
            assert any(row["event"] == "success" for row in rows)
        elif role == "goal_only":
            pass  # status/outputs/success already checked above, against its own artifact
        elif role == "business_outcome":
            assert result["status"] == "business_outcome" and result["code"] == run["code"]
            assert run["code"] == "member_not_found"
            assert any(
                row["event"] == "business_outcome" and row["code"] == "member_not_found"
                for row in rows
            )
        else:
            failure = Failure.model_validate(result["failure"])
            assert result["status"] == "failure" and failure.code == "operator_unavailable"
            assert failure.intervention_id and failure.evidence and failure.screenshot
            for name in (failure.evidence, failure.screenshot):
                assert f"failure/{name}" in files
            Snapshot.model_validate_json((root / "failure" / failure.evidence).read_text())
            assert (root / "failure" / failure.screenshot).read_bytes().startswith(b"\x89PNG")
            assert any(row["event"] == "intervention_requested" for row in rows)
        if role == "handoff":
            # A genuine live-session recovery, not a plain zero-model replay: an operator
            # claimed the paused lease and automation was explicitly resumed afterward.
            assert any(row["event"] == "intervention_requested" for row in rows)
            assert any(row["event"] == "control_claimed" for row in rows)
            assert any(row["event"] == "control_resumed" for row in rows)
        if role in {"tenant_b", "terminal"}:
            # Same saved capability, a presentation overlay only: replay stays correct,
            # and the drift between the recorded hints and the reviewed presentation --
            # tenant B's relabeling, or the terminal surface's entirely different target
            # kind -- is logged once on the success path, not silently absorbed.
            drift_rows = [row for row in rows if row["event"] == "presentation_drift"]
            assert len(drift_rows) == 1, drift_rows
            assert drift_rows[0]["count"] == len(drift_rows[0]["targets"]) > 0
            assert sorted(drift_rows[0]["targets"]) == sorted(artifact.targets)
        if role == "fallback":
            # No overlay, no reviewed alternate: a decoration-only relabel the
            # verified locator fallback ladder's `normalized` rung -- the ONLY
            # rung that ever acts -- rescues on its own, at acting time -- see
            # REPORT.md §2-4. Distinct from tenant_b/terminal above, whose
            # relabels are resolved through reviewed presentation, not this ladder.
            resolved_rows = [row for row in rows if row["event"] == "fallback_resolved"]
            assert len(resolved_rows) == 1 and resolved_rows[0]["target"] == "search"
            assert resolved_rows[0]["rung"] == run["rung"] == "normalized"
            assert result["warnings"] == [f"fallback_resolved:search:{run['rung']}"]
            review_text = (root / "fallback" / "fallback_review.json").read_text()
            review = json.loads(review_text)
            assert set(review) == {"search"}
            assert review["search"]["rung"] == run["rung"]
            assert review["search"]["op"] == "click"
            # Only the REVIEWED name may ever appear here -- never observed page text.
            assert review["search"]["target"]["name"] == "Find member"
            assert "FIND MEMBER" not in review_text
    for name in files:
        if Path(name).suffix not in {".json", ".jsonl"}:
            continue
        text = (root / name).read_text()
        assert all(
            secret not in text
            for secret in (
                '"00123"',
                '"00456"',
                "1204.57",
                "8902.10",
                "PRIVATE-NOTE",
                "SYNTHETIC PERSON",
            )
        ), f"Unredacted fixture value in {name}"


def check_live_evidence(root):
    """Cross-check real live-discovery artifacts against their selected event records."""
    capture = json.loads((root / "capture.json").read_text())
    expected_sources = {
        *map(str, Path("src/computer_use_replay").glob("*.py")),
        "profiles/juniper_live.json",
        "requests/read_savings.json",
        "requests/prepare_subaccount.json",
        "scripts/capture_live_evidence.py",
    }
    assert set(capture["source_files"]) == expected_sources
    for name, expected in capture["source_files"].items():
        assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == expected, name
    assert capture["provider"] == "ollama"
    cases = json.loads((root / "summary.json").read_text())
    assert {case["case"] for case in cases} == {
        "read_savings",
        "prepare_subaccount",
        "read_savings_relabeled",
    }
    policy = Policy(Binding.load(Path("profiles/juniper_live.json")), "http://localhost")
    for case in cases:
        directory = root / case["case"]
        artifact = Capability.model_validate_json((directory / "artifact.json").read_text())
        assert artifact.schema_version == "3.0" and artifact.grounded
        assert artifact.provenance.mode == "llm" and artifact.provenance.calls > 0
        assert len(artifact.grounded) == case["grounded_targets"]
        assert artifact.provenance.calls == case["discovery_calls"]
        assert case["status"] == "success" and case["replay_calls"] == 0
        policy.check_artifact(artifact)
        for mode in ("discovery", "replay"):
            result = json.loads((directory / mode / "result.json").read_text())
            rows = [
                json.loads(line)
                for line in (directory / mode / "events.jsonl").read_text().splitlines()
            ]
            assert [row["sequence"] for row in rows] == list(range(1, len(rows) + 1))
            for row in rows:
                Event.model_validate({key: value for key, value in row.items() if key != "time"})
            calls = [row for row in rows if row["event"] == "model_response"]
            expected_calls = artifact.provenance.calls if mode == "discovery" else 0
            assert len(calls) == result["llm_calls"] == expected_calls
            assert result["status"] == "success"
            assert result["outputs"] == {name: "<withheld>" for name in artifact.outputs}
            event = "artifact_saved" if mode == "discovery" else "started"
            assert any(
                row["event"] == event and row["artifact_sha256"] == artifact.digest()
                for row in rows
            )
            assert any(row["event"] == "success" for row in rows)


def check(capability_root=Path("capabilities"), evidence_root=Path("evidence")):
    assert (
        json.loads(Path("docs/capability.schema.json").read_text())
        == Capability.model_json_schema()
    )
    headings = [
        "Architecture",
        "Artifact schema",
        "Determinism & error handling",
        "Heterogeneity & multi-tenant",
        "Escalation & handoff",
        "Safety",
        "Cuts",
    ]
    report = Path("REPORT.md").read_text()
    assert all(f"## {i}. {name}" in report for i, name in enumerate(headings, 1))
    expected = {"read_savings.json", "prepare_subaccount.json"}
    assert capability_root.is_dir()
    assert {p.name for p in capability_root.glob("*.json")} == expected
    binding = Binding.load(Path("profiles/juniper.json"))
    overlay = binding.overlay(Path("profiles/tenant_b.json"))
    terminal = binding.overlay(Path("profiles/terminal.json"))
    assert binding.digest() == overlay.digest() == terminal.digest()
    for name in sorted(expected):
        artifact = Capability.model_validate_json((capability_root / name).read_text())
        assert artifact.provenance.mode == "llm"
        for variant in [binding, overlay, terminal]:
            Policy(variant, "http://localhost").check_artifact(artifact)
    check_evidence(evidence_root)
    check_live_evidence(evidence_root / "live_forms")
    check_git_tracked(evidence_root)
    print("PASS: source contracts and genuine discovery, replay, and masked failure evidence")


if __name__ == "__main__":
    check()
