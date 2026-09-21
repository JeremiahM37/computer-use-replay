"""The tour's per-step pass/fail expectations, tested against fake results --
no browser needed since these are pure functions over a Replay result."""

from types import SimpleNamespace

from computer_use_replay.tour import expect_drift, expect_fallback, expect_outcome, expect_success


def result(**kwargs):
    return SimpleNamespace(**kwargs)


def test_expect_success_matches():
    check = expect_success({"available_balance": {"amount": "1", "currency": "USD"}})
    ok, detail = check(
        None,
        None,
        result(status="success", outputs={"available_balance": {"amount": "1", "currency": "USD"}}),
    )
    assert ok is True and detail == "success"


def test_expect_success_rejects_non_success_status():
    check = expect_success({"a": 1})
    ok, detail = check(None, None, result(status="business_outcome", outputs=None, code="x"))
    assert ok is False and "expected success" in detail


def test_expect_success_rejects_wrong_outputs():
    check = expect_success({"a": 1})
    ok, detail = check(None, None, result(status="success", outputs={"a": 2}))
    assert ok is False and detail == "unexpected outputs"


def test_expect_outcome_matches():
    check = expect_outcome("member_not_found")
    ok, detail = check(None, None, result(status="business_outcome", code="member_not_found"))
    assert ok is True and "member_not_found" in detail


def test_expect_outcome_rejects_wrong_status_or_code():
    check = expect_outcome("member_not_found")
    ok, detail = check(None, None, result(status="success", code=None))
    assert ok is False and "expected business_outcome" in detail
    ok, detail = check(None, None, result(status="business_outcome", code="other"))
    assert ok is False


def test_expect_drift_rejects_non_success():
    check = expect_drift({"a": 1})
    ok, detail = check(None, None, result(status="failure", outputs=None))
    assert ok is False and "expected success" in detail


def test_expect_drift_requires_exactly_one_drift_event(tmp_path):
    directory = tmp_path / "run"
    directory.mkdir()
    (directory / "events.jsonl").write_text("")
    ex = SimpleNamespace(evidence=SimpleNamespace(directory=directory))
    check = expect_drift({"a": 1})
    ok, detail = check(ex, None, result(status="success", outputs={"a": 1}))
    assert ok is False and "presentation_drift" in detail


def test_expect_drift_matches_with_a_logged_event(tmp_path):
    import json

    directory = tmp_path / "run"
    directory.mkdir()
    (directory / "events.jsonl").write_text(
        json.dumps({"event": "presentation_drift", "targets": ["search"], "count": 1}) + "\n"
    )
    ex = SimpleNamespace(evidence=SimpleNamespace(directory=directory))
    check = expect_drift({"a": 1})
    ok, detail = check(ex, None, result(status="success", outputs={"a": 1}))
    assert ok is True and "presentation_drift" in detail


def test_expect_fallback_rejects_non_success():
    check = expect_fallback({"a": 1})
    ok, detail = check(None, None, result(status="failure", outputs=None))
    assert ok is False and "expected success" in detail


def test_expect_fallback_requires_exactly_one_event_and_a_warning(tmp_path):
    directory = tmp_path / "run"
    directory.mkdir()
    (directory / "events.jsonl").write_text("")
    ex = SimpleNamespace(evidence=SimpleNamespace(directory=directory))
    check = expect_fallback({"a": 1})
    ok, detail = check(ex, None, result(status="success", outputs={"a": 1}, warnings=()))
    assert ok is False and "fallback_resolved" in detail


def test_expect_fallback_matches_with_a_logged_event(tmp_path):
    import json

    directory = tmp_path / "run"
    directory.mkdir()
    (directory / "events.jsonl").write_text(
        json.dumps({"event": "fallback_resolved", "target": "search", "rung": "normalized"}) + "\n"
    )
    ex = SimpleNamespace(evidence=SimpleNamespace(directory=directory))
    check = expect_fallback({"a": 1})
    ok, detail = check(
        ex,
        None,
        result(
            status="success",
            outputs={"a": 1},
            warnings=("fallback_resolved:search:normalized",),
        ),
    )
    assert ok is True and "fallback_resolved" in detail
