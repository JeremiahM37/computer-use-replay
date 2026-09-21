"""A second, deliberately basic Surface: a text-mode rendition of the same
fictional Juniper branch workstation demo.py serves over HTTP, and a Surface
implementation over its screen buffer instead of a DOM. See REPORT.md §4 for
why this exists -- it is the seam proof, not a real terminal-protocol client.

TextWorkstation knows nothing about capabilities, policy or the Surface
protocol: it only exposes what a terminal emulator API already exposes --
read the current screen buffer, set a labelled field, activate a named
command. ScreenSurface is the adapter that turns that into `Surface`.

No network of any kind touches this module. `Policy.check_network_request`/
`discard_socket` exist for a browser transport and are never called here, so
route policy never triggers on this surface -- there is nothing for it to see.
"""

from __future__ import annotations

from dataclasses import dataclass

from computer_use_replay import fallback
from computer_use_replay.contracts import Click, Condition, Fill, Read, Target
from computer_use_replay.control import Ownership
from computer_use_replay.demo import MEMBERS
from computer_use_replay.evidence import Event, Evidence, Node, Snapshot
from computer_use_replay.policy import Policy, Stop

FIELD_WIDTH = 11
SCENARIOS = ("normal", "expired", "wrong_member", "duplicate")


@dataclass
class Entry:
    role: str  # heading | field | command | value
    name: str
    text: str = ""


class TextWorkstation:
    """Member search -> search results -> member summary -> account directory
    -> savings ledger, rendered as a fixed-width screen buffer instead of HTML.
    Same members/balances as the web fixture (see demo.MEMBERS) -- imported,
    never duplicated.
    """

    def __init__(self, scenario="normal"):
        if scenario not in SCENARIOS:
            raise ValueError("unknown terminal scenario")
        self.scenario = scenario
        self.member = ""
        self.resolved = False
        self.screen = "search"

    def open(self):
        self.screen, self.member, self.resolved = "search", "", False

    def entries(self) -> list[Entry]:
        if self.screen == "search":
            commands = [Entry("command", "Find member")]
            if self.scenario == "duplicate":
                commands.append(Entry("command", "Find member"))
            return [
                Entry("heading", "Member search"),
                Entry("field", "Member identifier", self.member),
                *commands,
            ]
        if self.screen == "results":
            return [Entry("heading", "Search results"), Entry("command", "Open member")]
        if self.screen == "summary":
            return [
                Entry("heading", "Member summary"),
                Entry("value", "Member identifier", self.member),
                Entry("command", "View accounts"),
            ]
        if self.screen == "expired":
            return [Entry("heading", "Session expired"), Entry("command", "Renew session")]
        if self.screen == "accounts":
            return [Entry("heading", "Account directory"), Entry("command", "Open savings ledger")]
        if self.screen == "savings":
            shown_member = "00777" if self.scenario == "wrong_member" else self.member
            return [
                Entry("heading", "Savings ledger"),
                Entry("value", "Member identifier", shown_member),
                Entry("value", "Available balance", MEMBERS.get(self.member, "")),
            ]
        headings = {
            "not_found": "Member not found",
            "validation": "Validation error",
            "denied": "Permission denied",
        }
        return [Entry("heading", headings[self.screen])]

    def lines(self) -> list[str]:
        rendered, commands = [], []
        for entry in self.entries():
            if entry.role == "heading":
                rendered.append(entry.name)
            elif entry.role == "field":
                box = entry.text.ljust(FIELD_WIDTH)[:FIELD_WIDTH]
                rendered.append(f"{entry.name}: [{box}]")
            elif entry.role == "value":
                rendered.append(f"{entry.name}: {entry.text}")
            else:
                commands.append(f"[ {entry.name} ]")
        if commands:
            rendered.append("  ".join(commands))
        return rendered

    def set_field(self, label, text):
        """Type into the labelled field on the current screen, as an emulator API does.
        A label that is not an editable field right now is refused, never guessed."""
        if not any(e.role == "field" and e.name == label for e in self.entries()):
            raise KeyError(label)
        self.member = text

    def activate(self, name):
        if name == "Find member":
            if self.member == "00000":
                self.screen = "validation"
            elif self.member == "00888":
                self.screen = "denied"
            elif self.member not in MEMBERS:
                self.screen = "not_found"
            else:
                self.screen = "results"
        elif name == "Open member":
            self.screen = "summary"
        elif name == "View accounts":
            expired = self.scenario == "expired" and not self.resolved
            self.screen = "expired" if expired else "accounts"
        elif name == "Renew session":
            self.resolved, self.screen = True, "accounts"
        elif name == "Open savings ledger":
            self.screen = "savings"


class ScreenSurface:
    """A `Surface` over `TextWorkstation`'s screen buffer instead of a page.
    Same strictness as `BrowserSurface`: a reviewed `screen` target resolves
    only against the current screen's entries, exactly one match is required
    for an action, and zero/multiple matches raise the same Stop codes.
    """

    def __init__(
        self, policy: Policy, ownership: Ownership, evidence: Evidence, workstation: TextWorkstation
    ):
        self.policy, self.ownership, self.evidence = policy, ownership, evidence
        self.workstation = workstation
        self._arguments = {}
        self.last_snapshot = Snapshot(controls=(), states=())
        self._closed = False

    def bind_arguments(self, arguments):
        self._arguments = dict(arguments)

    def check_health(self):
        if self._closed:
            raise Stop("session_closed")

    def _matches(self, target: Target) -> list[Entry]:
        # A control the terminal overlay never touched keeps its browser-shaped
        # target (kind != "screen"); it must never accidentally resolve here just
        # because its role/name string happens to coincide with a screen entry.
        if target.kind != "screen":
            return []
        return [
            e for e in self.workstation.entries() if e.role == target.role and e.name == target.name
        ]

    def _resolve(self, key):
        control = self.policy.binding.controls[key]
        for rank, target in enumerate((control.target, *control.alternates)):
            matches = self._matches(target)
            if len(matches) == 1:
                return matches[0], 1, rank
            if len(matches) > 1:
                raise Stop(
                    "ambiguous_target",
                    "exactly one visible " + key,
                    f"{len(matches)} matches",
                    target=key,
                )
        return None, 0, None

    def count(self, key):
        _, count, _ = self._resolve(key)
        return count

    def _unique(self, key, *, log_alternate=False):
        entry, count, rank = self._resolve(key)
        if count != 1:
            raise Stop(
                "target_missing", "exactly one visible " + key, f"{count} matches", target=key
            )
        if log_alternate and rank:
            self.evidence.emit(Event(event="alternate_resolved", target=key, rank=rank))
        return entry

    async def find_fallback(self, key, op):
        """Same verified ladder as BrowserSurface.find_fallback (see its
        docstring and engine.py's `acting_op` gate) over `TextWorkstation`
        entries instead of a DOM: ONLY the `normalized` rung may act (any
        op), refusing more than one match and any single candidate another
        reviewed control's own target already resolves to.
        """
        control = self.policy.binding.controls[key]
        primary = control.target
        candidates = [e for e in self.workstation.entries() if e.role == primary.role]
        claimed = {
            e.name
            for other_key, other in self.policy.binding.controls.items()
            if other_key != key
            for target in (other.target, *other.alternates)
            for e in self._matches(target)
        }
        matches = [e for e in candidates if fallback.names_equal(e.name, primary.name)]
        if len(matches) > 1:
            raise Stop(
                "ambiguous_target",
                "exactly one visible " + key,
                f"{len(matches)} matches",
                target=key,
            )
        if len(matches) == 1 and matches[0].name not in claimed:
            return Target(kind="screen", role=primary.role, name=matches[0].name), "normalized"
        return None

    async def navigate(self):
        async with self.ownership.lock:
            self.ownership.require_automation()
            self.workstation.open()
            self.check_health()

    async def observe(self):
        self.check_health()
        nodes = []
        for key, control in self.policy.binding.controls.items():
            matches = []
            for target in (control.target, *control.alternates):
                matches = self._matches(target)
                if matches:
                    break
            if matches:
                nodes.append(Node(target=key, count=len(matches)))
        present = {node.target for node in nodes}
        states = tuple(
            key for key, state in self.policy.binding.states.items() if state.target in present
        )
        snapshot = Snapshot(controls=tuple(nodes), states=states, unknown_dialogs=0)
        self.last_snapshot = snapshot
        return snapshot

    async def condition(self, condition: Condition, arguments: dict):
        count = self.count(condition.target)
        if condition.kind == "absent":
            return count == 0
        if count != 1:
            return False
        if condition.kind == "visible":
            return True
        entry = self._unique(condition.target)
        return condition.matches_value(entry.text, arguments)

    async def perform(self, step, arguments):
        async with self.ownership.lock:
            self.ownership.require_automation()
            self.check_health()
            self.policy.check_action(step.op, step.target)
            if isinstance(step, Fill):
                self.policy.check_fill(step.target, step.input)
            entry = self._unique(step.target, log_alternate=True)
            self.evidence.emit(
                Event(
                    event="action_started",
                    op=step.op,
                    target=step.target,
                    parameter=step.input if isinstance(step, Fill) else None,
                )
            )
            if isinstance(step, Click):
                self.workstation.activate(entry.name)
            elif isinstance(step, Fill):
                self.workstation.set_field(entry.name, str(arguments[step.input]))
            elif isinstance(step, Read):
                self.check_health()
                self.evidence.emit(Event(event="action_completed", op=step.op, target=step.target))
                return entry.text.strip()
            else:
                raise Stop("unsupported_action")
            self.check_health()
            self.evidence.emit(Event(event="action_completed", op=step.op, target=step.target))
            return None

    async def failure_screenshot(self):
        """Structure-only masked snapshot: every letter/digit becomes a solid
        block, punctuation and layout stay -- the text equivalent of
        `BrowserSurface.failure_screenshot`'s masked image.
        """
        lines = [
            "".join("█" if ch.isalnum() else ch for ch in line) for line in self.workstation.lines()
        ]
        name = "failure-masked.txt"
        (self.evidence.directory / name).write_text("\n".join(lines) + "\n")
        return name
