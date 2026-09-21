"""Local CLI; the headed browser plus terminal is the minimal operator surface."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from computer_use_replay.browser import BrowserSurface
from computer_use_replay.console import console_input
from computer_use_replay.contracts import Capability, Failed, Failure, GoalRequest
from computer_use_replay.control import Handoff, Ownership
from computer_use_replay.engine import Execution, Replay
from computer_use_replay.evidence import Event, Evidence, atomic_json
from computer_use_replay.policy import Binding, Policy, Stop


class MissingEnvironment(Exception):
    """A --input-env variable was not set. Names the variable, never a value."""

    def __init__(self, variable):
        self.variable = variable
        super().__init__(variable)


def _kv_pair(raw):
    key, sep, value = raw.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError("expected key=value")
    return key, value


def assemble_inputs(args):
    """Merge --inputs/--args JSON with repeatable --input (literal string) and
    --input-env (read from the named environment variable, so a credential value
    never sits in shell history or process args) pairs. The same key may only be
    declared once, across all three sources.
    """
    try:
        values = json.loads(args.inputs)
    except json.JSONDecodeError:
        raise ValueError("invalid_input_json") from None
    if not isinstance(values, dict):
        raise ValueError("invalid_input_json")
    for key, value in getattr(args, "input", None) or ():
        if key in values:
            raise ValueError("duplicate_input")
        values[key] = value
    for key, variable in getattr(args, "input_env", None) or ():
        if key in values:
            raise ValueError("duplicate_input")
        value = os.environ.get(variable)
        if value is None:
            raise MissingEnvironment(variable)
        values[key] = value
    return values


async def terminal_operator(owner, request, validate):
    lease = await owner.claim(request)
    print(
        f"Intervention {request} ({owner.reason}, step {owner.step}): automation paused. Use the SAME browser window.",
        file=sys.stderr,
        flush=True,
    )
    while True:
        print("Resolve the blocking state; enter resume or cancel: ", file=sys.stderr, flush=True)
        answer = await console_input()
        if answer.strip() == "cancel":
            await owner.cancel()
            raise Stop("operator_cancelled")
        if answer.strip() == "resume":
            try:
                await owner.resume(lease, validate)
                return
            except Stop as exc:
                if exc.code != "resume_condition_unmet":
                    raise
                print(
                    "The required state is still blocked. Browser remains under your control.",
                    file=sys.stderr,
                    flush=True,
                )


async def run(args):
    if args.command == "invoke":
        from computer_use_replay.catalog import resolve_capability

        # Same lookup-by-name that `catalog` lists; everything after this line is
        # the ordinary replay path, unmodified -- invoke never forks the engine.
        args.artifact = str(
            await asyncio.to_thread(resolve_capability, Path(args.capabilities), args.name)
        )
    if args.command == "discover" and not args.request and not args.goal:
        raise ValueError("goal-only discovery requires --goal")
    binding = Binding.load(Path(args.binding))
    if args.presentation:
        binding = binding.overlay(Path(args.presentation))
    if getattr(args, "fallback", None):
        binding = binding.model_copy(update={"fallback": args.fallback})
    policy = Policy(binding, args.target)
    evidence = Evidence(Path(args.evidence))
    request = None
    artifact = None
    if args.command in {"replay", "invoke"}:
        artifact = Capability.model_validate_json(
            await asyncio.to_thread(Path(args.artifact).read_text)
        )
        policy.check_artifact(artifact)
        contract = artifact
    elif args.command == "discover" and not args.request:
        # Goal-only onboarding: propose a draft contract from ONLY the reviewed
        # vocabulary and the goal sentence -- no page content, no --request file.
        from computer_use_replay.onboarding import propose
        from computer_use_replay.providers import ProviderConfig

        proposal_config = ProviderConfig.from_env(
            args.provider,
            args.model,
            args.model_url,
            timeout=args.model_timeout,
            retries=args.model_retries,
        )
        request, _ = await propose(proposal_config, evidence, binding, args.goal)
        policy.check_request(request)
        contract = request
    else:
        request = GoalRequest.load(Path(args.request))
        if args.goal:
            request = request.model_copy(update={"goal": args.goal})
        policy.check_request(request)
        contract = request
    # Fail-closed acceptance gate: applies uniformly to whatever `contract` is.
    # Capability has no `review` attribute, so this is always a no-op for
    # replay/invoke -- there is no such thing as a draft artifact, only a draft
    # contract, and discover() below never runs against one without this.
    accepted_by = None
    review = getattr(contract, "review", None)
    if review is not None and review.status == "draft":
        from computer_use_replay.onboarding import confirm_draft

        if args.accept_draft:
            accepted_by = "flag"
        elif await confirm_draft(contract):
            accepted_by = "tty"
        else:
            raise Stop(
                "draft_not_accepted",
                "explicit --accept-draft or interactive TTY acceptance",
                "no acceptance recorded",
            )
        evidence.emit(Event(event="contract_accepted", capability=contract.name, code=accepted_by))
    try:
        arguments = contract.arguments(assemble_inputs(args))
    except ValueError:
        result = Failed(
            failure=Failure(
                code="invalid_input", expected="declared typed input", observed="input rejected"
            )
        )
        evidence.result(result)
        print(json.dumps(result.model_dump(mode="json"), indent=2))
        return 1
    # Validate credentials/model settings before opening a browser. Replay imports neither module.
    if args.command == "discover":
        from computer_use_replay.planner import ModelPlanner
        from computer_use_replay.providers import ProviderConfig

        planner = ModelPlanner(
            ProviderConfig.from_env(
                args.provider,
                args.model,
                args.model_url,
                timeout=args.model_timeout,
                retries=args.model_retries,
                decision_retries=args.model_decision_retries,
                max_tokens=args.model_max_tokens,
            ),
            evidence,
        )
    ownership = Ownership(evidence)
    present = getattr(args, "present", False)
    async with BrowserSurface(
        policy,
        ownership,
        evidence,
        headed=args.human or present,
        present=present,
        pace=getattr(args, "pace", 0.9),
    ) as surface:
        if present and accepted_by is not None:
            # Goal-only (or draft-request) onboarding: show what was proposed and
            # accepted before the ordinary per-step "model chose: ..." captions.
            surface.presentation_context(contract.name)
            await surface.present_outcome(
                f"proposed: {contract.name} -- inputs {', '.join(contract.inputs)}, "
                f"outputs {', '.join(contract.outputs)} (accepted: {accepted_by})"
            )
        operator = terminal_operator if args.human else None
        handoff = Handoff(ownership, operator)
        execution = Execution(surface, policy, evidence, handoff)
        if args.command in {"replay", "invoke"}:
            result = await Replay(execution).run(artifact, arguments)
        else:
            from computer_use_replay.discovery import discover

            _, result = await discover(
                execution,
                planner,
                request,
                arguments,
                Path(args.artifact),
                accepted_by=accepted_by,
            )
        # stdout is the caller response channel. Persistent result.json withholds output values.
        print(json.dumps(result.model_dump(mode="json"), indent=2))
        print(f"Evidence: {evidence.directory}", file=sys.stderr)
        return 1 if result.status == "failure" else 0


# Factory defaults an --integration descriptor is allowed to override. An explicit
# flag value equal to the factory default is indistinguishable from "not given" --
# an accepted, narrow edge case in exchange for not restructuring every default.
_INTEGRATION_DEFAULTS = {
    "target": "http://127.0.0.1:8765",
    "binding": "profiles/juniper.json",
    "capabilities": "capabilities",
    "request": "requests/read_savings.json",
    "artifact": "runs/read_savings.json",
}


def _apply_integration(args):
    name = getattr(args, "integration", None)
    if not name:
        return
    path = Path("integrations") / name / "integration.json"
    try:
        descriptor = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        raise ValueError("unknown_integration") from None
    if not isinstance(descriptor, dict):
        raise ValueError("unknown_integration")
    for field in ("target", "binding", "capabilities", "request"):
        if not hasattr(args, field):
            continue
        current = getattr(args, field)
        # discover's --request has no factory-default string (None means
        # goal-only, unset) -- None is "not given" here exactly like the
        # factory default is for every other field. But an explicit --goal
        # with no --request is a deliberate goal-only choice; don't let an
        # integration's fixed request.json silently turn that into
        # file-based discovery.
        if field == "request" and current is None and getattr(args, "goal", None):
            continue
        if current == _INTEGRATION_DEFAULTS[field] or current is None:
            if field in descriptor:
                setattr(args, field, descriptor[field])
    if (
        hasattr(args, "artifact")
        and args.artifact == _INTEGRATION_DEFAULTS["artifact"]
        and "capabilities" in descriptor
        and "capability" in descriptor
    ):
        args.artifact = f"{descriptor['capabilities']}/{descriptor['capability']}.json"


async def run_propose(args):
    from computer_use_replay.onboarding import propose
    from computer_use_replay.providers import ProviderConfig

    binding = Binding.load(Path(args.binding))
    evidence = Evidence(Path(args.evidence))
    config = ProviderConfig.from_env(
        args.provider,
        args.model,
        args.model_url,
        timeout=args.model_timeout,
        retries=args.model_retries,
    )
    try:
        request, _ = await propose(config, evidence, binding, args.goal, name=args.name)
    except Stop as exc:
        result = Failed(
            failure=Failure(code=exc.code, expected=exc.expected, observed=exc.observed)
        )
        evidence.result(result)
        print(json.dumps(result.model_dump(mode="json"), indent=2))
        print(f"Evidence: {evidence.directory}", file=sys.stderr)
        return 1
    atomic_json(Path(args.out), request.model_dump(mode="json"))
    print(request.model_dump_json(indent=2))
    print(
        f"Draft written to {args.out}. Review it, then pass --request {args.out} "
        "--accept-draft (or accept the interactive prompt) to `discover` it.",
        file=sys.stderr,
    )
    print(f"Evidence: {evidence.directory}", file=sys.stderr)
    return 0


def main():
    parser = argparse.ArgumentParser(description="Discover and replay reviewed UI capabilities")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Run the fictional workstation")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--scenario", default="normal")
    propose = commands.add_parser(
        "propose",
        help="Draft a task contract from a goal sentence and the reviewed product "
        "vocabulary, for human review",
    )
    propose.add_argument("--goal", required=True)
    propose.add_argument("--binding", default="profiles/juniper.json")
    propose.add_argument("--name", help="Override the model's proposed capability slug")
    propose.add_argument("--out", required=True, help="Where to write the draft request JSON")
    propose.add_argument("--evidence", default="runs")
    propose.add_argument("--provider", choices=["ollama", "openai"], default="ollama")
    propose.add_argument("--model-url", help="Base URL for Ollama only")
    propose.add_argument("--model", help="Explicit model ID (Ollama also reads OLLAMA_MODEL)")
    propose.add_argument("--model-timeout", type=float, default=90, help="Total seconds")
    propose.add_argument("--model-retries", type=int, default=2, help="Transient retries, 0-3")
    catalog = commands.add_parser(
        "catalog", help="Emit an agent-facing catalog of saved capabilities"
    )
    catalog.add_argument(
        "--capabilities", default="capabilities", help="Directory of saved capability artifacts"
    )
    catalog.add_argument("--binding", default="profiles/juniper.json")
    catalog.add_argument("--presentation", help="Reviewed tenant locator overlay")
    catalog.add_argument(
        "--integration",
        help="Fill --binding/--capabilities from integrations/<name>/integration.json",
    )
    invoke = commands.add_parser(
        "invoke", help="Look up a saved capability by name and replay it, zero model calls"
    )
    invoke.add_argument("name", help="Capability name, as saved in --capabilities")
    invoke.add_argument(
        "--capabilities", default="capabilities", help="Directory of saved capability artifacts"
    )
    invoke.add_argument("--target", default="http://127.0.0.1:8765")
    invoke.add_argument("--binding", default="profiles/juniper.json")
    invoke.add_argument("--presentation", help="Reviewed tenant locator overlay")
    invoke.add_argument(
        "--integration",
        help="Fill --target/--binding/--capabilities from integrations/<name>/integration.json",
    )
    invoke.add_argument(
        "--args", dest="inputs", default="{}", help="Typed JSON object; member IDs are strings"
    )
    invoke.add_argument(
        "--input",
        action="append",
        type=_kv_pair,
        metavar="key=value",
        help="Repeatable string-valued input, merged with --args (no key overlap)",
    )
    invoke.add_argument(
        "--input-env",
        dest="input_env",
        action="append",
        type=_kv_pair,
        metavar="key=ENVVAR",
        help="Repeatable input read from an environment variable at run time",
    )
    invoke.add_argument("--evidence", default="runs")
    invoke.add_argument(
        "--human", action="store_true", help="Headed browser with explicit terminal claim/resume"
    )
    invoke.add_argument(
        "--present",
        action="store_true",
        help="Headed browser with a highlight/caption overlay and paced actions",
    )
    invoke.add_argument("--pace", type=float, default=0.9, help="Seconds paused per presented step")
    invoke.add_argument(
        "--fallback",
        choices=["verified", "off"],
        help="Override the profile's verified locator fallback ladder ('off' reproduces "
        "today's strict target_drift on a zero-match acting target)",
    )
    for name in ["discover", "replay"]:
        sub = commands.add_parser(name)
        sub.add_argument("--target", default="http://127.0.0.1:8765")
        sub.add_argument("--binding", default="profiles/juniper.json")
        sub.add_argument("--presentation", help="Reviewed tenant locator overlay")
        sub.add_argument("--artifact", default="runs/read_savings.json")
        sub.add_argument("--inputs", default="{}", help="Typed JSON object; member IDs are strings")
        sub.add_argument("--evidence", default="runs")
        sub.add_argument(
            "--human",
            action="store_true",
            help="Headed browser with explicit terminal claim/resume",
        )
        sub.add_argument(
            "--accept-draft",
            action="store_true",
            help="Accept a model-proposed draft contract without an interactive TTY prompt",
        )
        sub.add_argument(
            "--input",
            action="append",
            type=_kv_pair,
            metavar="key=value",
            help="Repeatable string-valued input, merged with --inputs (no key overlap)",
        )
        sub.add_argument(
            "--input-env",
            dest="input_env",
            action="append",
            type=_kv_pair,
            metavar="key=ENVVAR",
            help="Repeatable input read from an environment variable at run time",
        )
        sub.add_argument(
            "--integration",
            help="Fill --target/--binding/--request/--artifact from"
            " integrations/<name>/integration.json",
        )
        sub.add_argument(
            "--present",
            action="store_true",
            help="Headed browser with a highlight/caption overlay and paced actions",
        )
        sub.add_argument(
            "--pace", type=float, default=0.9, help="Seconds paused per presented step"
        )
        sub.add_argument(
            "--fallback",
            choices=["verified", "off"],
            help="Override the profile's verified locator fallback ladder ('off' reproduces "
            "today's strict target_drift on a zero-match acting target)",
        )
        if name == "discover":
            sub.add_argument(
                "--request",
                help="Named task contract JSON. Omit together with --goal for goal-only "
                "onboarding: the system proposes a draft contract, which still requires "
                "acceptance (--accept-draft or an interactive TTY prompt) before it runs.",
            )
            sub.add_argument(
                "--goal",
                help="Override the request's natural-language goal, or (with no --request) "
                "the goal a draft contract is proposed for",
            )
            sub.add_argument(
                "--provider",
                choices=["ollama", "openai"],
                default="ollama",
            )
            sub.add_argument("--model-url", help="Base URL for Ollama only")
            sub.add_argument("--model", help="Explicit model ID (Ollama also reads OLLAMA_MODEL)")
            sub.add_argument(
                "--model-timeout", type=float, default=90, help="Total seconds per decision"
            )
            sub.add_argument("--model-retries", type=int, default=2, help="Transient retries, 0–3")
            sub.add_argument(
                "--model-decision-retries",
                type=int,
                default=1,
                help="Rejected native decisions to retry without UI actions, 0–2",
            )
            sub.add_argument(
                "--model-max-tokens", type=int, default=2048, help="Output token budget"
            )
    demo = commands.add_parser(
        "demo", help="Zero-setup guided tour of the learned capabilities, zero model calls"
    )
    demo.add_argument(
        "--discover",
        action="store_true",
        help="Genuinely discover read_savings first, then use it for the tour",
    )
    demo.add_argument(
        "--model", help="Explicit model ID (OpenAI requires this; Ollama also reads OLLAMA_MODEL)"
    )
    demo.add_argument("--model-url", help="Base URL for Ollama only")
    demo.add_argument(
        "--present",
        action="store_true",
        help="Headed browser with a highlight/caption overlay and paced actions",
    )
    demo.add_argument("--pace", type=float, default=0.9, help="Seconds paused per presented step")
    demo.add_argument("--evidence", default="runs/demo", help="Private evidence root for the tour")
    demo.add_argument(
        "--fallback",
        choices=["verified", "off"],
        help="Override the verified locator fallback ladder for every tour scene "
        "('off' reproduces today's strict target_drift on the minor-relabel scene)",
    )
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        from computer_use_replay.demo import create_app

        # Access logs can carry route data; keep them off even for the fictional fixture.
        uvicorn.run(create_app(args.scenario), host="127.0.0.1", port=args.port, access_log=False)
        return
    try:
        _apply_integration(args)
        if args.command == "demo":
            from computer_use_replay.tour import run_demo_cli

            code = asyncio.run(run_demo_cli(args))
        elif args.command == "catalog":
            from computer_use_replay.catalog import build_catalog, unavailable_capabilities

            binding = Binding.load(Path(args.binding))
            if args.presentation:
                binding = binding.overlay(Path(args.presentation))
            print(json.dumps(build_catalog(Path(args.capabilities), binding), indent=2))
            for entry in unavailable_capabilities(Path(args.capabilities), binding):
                print(
                    f"not listed: {entry['file']} ({entry['name']}): {entry['reason']}",
                    file=sys.stderr,
                )
            code = 0
        elif args.command == "propose":
            code = asyncio.run(run_propose(args))
        else:
            code = asyncio.run(run(args))
    except MissingEnvironment as exc:
        parser.exit(2, f"Missing required environment variable: {exc.variable}\n")
    except (ValueError, OSError, Stop):
        parser.exit(
            2,
            "Invalid configuration or unavailable local resource. Check the supplied paths and settings.\n",
        )
    raise SystemExit(code)


if __name__ == "__main__":
    main()
