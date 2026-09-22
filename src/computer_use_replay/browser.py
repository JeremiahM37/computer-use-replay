"""Frame-aware, unique semantic targeting. No positional fallback or blind action retry."""

from __future__ import annotations

import asyncio
import json
import os
import re
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright

from computer_use_replay import fallback
from computer_use_replay.browser_perception import LivePerception
from computer_use_replay.contracts import Click, Condition, Fill, Read, Target
from computer_use_replay.control import Ownership
from computer_use_replay.evidence import Event, Evidence, Node, Snapshot
from computer_use_replay.policy import Policy, Stop

# Per fallback-candidate kind: how to enumerate visible elements of that kind in
# a frame, and how to read each one's approximate accessible name/label/header.
# `css` and `screen` targets never reach here (see find_fallback) -- css has no
# accessible-name convention to enumerate, and screen is the terminal surface's
# own kind.
_FALLBACK_NAME_JS = {
    "role": "el => (el.getAttribute('aria-label') || el.textContent || '').trim()",
    "label": "el => (el.labels?.[0]?.textContent || el.getAttribute('aria-label') || '').trim()",
    "row_value": "el => (el.textContent || '').trim()",
    "row_input": "el => (el.textContent || '').trim()",
}


def _is_navigation_race(exc: BrowserError) -> bool:
    """A concurrent document navigation can invalidate a locator mid-read; this is not
    a structural adapter fault, and callers already re-observe on their own bounded budget.
    """
    return "Execution context was destroyed" in str(exc) or "Frame was detached" in str(exc)


# condition() calls this after count() has already confirmed the control is present.
# A concurrent navigation can still invalidate the locator in the gap between those two
# calls; waiting the FULL step_timeout on that stale read would burn the caller's whole
# polling budget in a single settle() iteration, since settle()'s own deadline is that
# same step_timeout. Bounding the read well below it lets the caller's loop re-observe
# and succeed on a later poll instead of timing out on one unlucky read.
STALE_READ_TIMEOUT = 0.5


class BrowserSurface:
    def __init__(
        self,
        policy: Policy,
        ownership: Ownership,
        evidence: Evidence,
        *,
        headed=False,
        present=False,
        pace=0.9,
    ):
        self.policy, self.ownership, self.evidence = policy, ownership, evidence
        self.headed = headed
        # Presentation mode: a top-level-document-only highlight + caption overlay,
        # never a control and never part of any observation. See presentation_context()/
        # presentation_step()/note_decision()/present_outcome() below and REPORT.md.
        self.present = present
        self.reviewed_targets = {}
        self.pace = pace
        self._tour = {"capability": None, "total": None, "index": None}
        self._decision_note = None
        self._arguments = {}
        self._live_controls = {}
        self._live_scopes = {}
        self._live_history = {}
        self._live_pinned_handles = {}
        self._live_current_ids = set()
        self._live_epoch = 0
        self._replay_groundings = {}
        self.blocked = None
        self.generation = 0
        self.last_snapshot = Snapshot(controls=(), states=())
        self.pw = self.browser = self.context = self.page = None

    async def __aenter__(self):
        self.pw = await async_playwright().start()
        try:
            # COMPUTER_USE_REPLAY_HEADLESS=1 keeps the overlay and pacing but forces a
            # headless launch, so presentation e2e tests run without a display.
            headless = not self.headed or os.getenv("COMPUTER_USE_REPLAY_HEADLESS") == "1"
            self.browser = await self.pw.chromium.launch(
                headless=headless, executable_path=os.getenv("COMPUTER_USE_REPLAY_CHROMIUM") or None
            )
            context_kwargs = {"service_workers": "block", "accept_downloads": False}
            # Opt-in Playwright video recording for walkthroughs/demos, never on by
            # default: COMPUTER_USE_REPLAY_RECORD_VIDEO=<dir> writes one .webm per context at
            # a fixed size, so the frame stays stable across headed and headless runs.
            record_video_dir = os.getenv("COMPUTER_USE_REPLAY_RECORD_VIDEO")
            if record_video_dir:
                context_kwargs["record_video_dir"] = record_video_dir
                context_kwargs["record_video_size"] = {"width": 1280, "height": 800}
                context_kwargs["viewport"] = {"width": 1280, "height": 800}
            self.context = await self.browser.new_context(**context_kwargs)
            self.context.set_default_timeout(
                self.policy.binding.step_timeout
                * (3 if self.policy.binding.live_mode == "forms" else 1)
                * 1000
            )
            await self.context.route("**/*", self._request)
            await self.context.route_web_socket("**/*", self._websocket)
            await self.context.expose_binding("__computer_use_replay_event", self._human_event)
            capture_names = {
                control.target.name: key
                for key, control in self.policy.binding.controls.items()
                if control.operations
            }
            await self.context.add_init_script(
                """(() => {
              const known = """
                + json.dumps(capture_names)
                + """;
              for (const kind of ['click','input','submit']) {
                document.addEventListener(kind, e => {
                  const el = e.target.closest('button,input,select,textarea,a') || e.submitter;
                  const label = el ? (el.labels?.[0]?.textContent || el.getAttribute('aria-label') || el.textContent || '').trim() : '';
                  if (e.isTrusted || kind === 'submit') window.__computer_use_replay_event(kind, known[label] || null);
                }, true);
              }
            })();"""
            )
            self.page = await self.context.new_page()
            self.page.on("dialog", self._dialog)
            self.page.on("popup", self._popup)
            self.page.on("download", self._download)
            self.page.on("framenavigated", self._navigation)
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *_):
        try:
            if self.context:
                await self.context.close()
        finally:
            try:
                if self.browser:
                    await self.browser.close()
            finally:
                if self.pw:
                    await self.pw.stop()

    async def _request(self, route):
        try:
            if not self.policy.check_network_request(
                route.request.url, route.request.method, route.request.post_data
            ):
                await self._abort(route, "blockedbyclient")
                return
        except Stop:
            self.blocked = "network_policy"
            await self._abort(route, "blockedbyclient")
        else:
            # Browser routing does not intercept every hop of a redirect chain, so
            # letting Chromium follow one itself would reach unreviewed content
            # outside policy. A redirect is refused unless the exact rule matched
            # for this request explicitly reviews that destination path; SPA-style
            # bindings that grant no redirect_to keep failing closed exactly as
            # before this existed. A granted redirect is followed here, not by
            # Chromium: this same route fetches the one reviewed destination as a
            # plain same-origin GET (still checked against full policy, same as
            # any other allowed navigation) and fulfills the original request
            # with its response, so every later action still only ever observes
            # content this policy already checked.
            try:
                response = await route.fetch(
                    max_redirects=0, timeout=self.policy.binding.step_timeout * 1000
                )
                if 300 <= response.status < 400 and response.headers.get("location"):
                    destination = urljoin(route.request.url, response.headers["location"])
                    # Cross-origin or otherwise unlisted destinations keep raising the
                    # same general "network_policy" they always did, checked first and
                    # unchanged. Only once a destination independently clears full policy
                    # does the narrower question "did this exact rule review a redirect
                    # to it" apply, so a same-origin allowed route that simply was not
                    # granted still refuses, distinctly, as "redirect_not_allowed".
                    self.policy.check_url(destination, "GET")
                    rule = self.policy.check_url(route.request.url, route.request.method)
                    if not rule or not rule.permits_redirect(urlsplit(destination).path):
                        raise Stop("redirect_not_allowed")
                    response = await route.fetch(
                        url=destination,
                        method="GET",
                        post_data=None,
                        max_redirects=0,
                        timeout=self.policy.binding.step_timeout * 1000,
                    )
                    if 300 <= response.status < 400:
                        raise Stop("redirect_not_allowed")
                await route.fulfill(response=response)
            except Stop as exc:
                self.blocked = exc.code
                await self._abort(route, "blockedbyclient")
            except Exception:
                self.blocked = "load_failed"
                await self._abort(route, "failed")

    async def _abort(self, route, reason):
        try:
            await route.abort(reason)
        except BrowserError:
            # Cancellation/closure may settle the request before our deny response.
            # The stored blocked code still prevents further automation.
            pass

    async def _websocket(self, socket):
        if not self.policy.discard_socket(socket.url):
            self.blocked = "network_policy"
        await socket.close()

    async def _dialog(self, dialog):
        self.blocked = "native_dialog"
        await dialog.dismiss()  # Cancel rather than accepting an unrecognized native confirmation.

    async def _popup(self, page):
        self.blocked = "unexpected_window"
        await page.close()

    async def _download(self, download):
        self.blocked = "unexpected_download"
        await download.cancel()

    async def _human_event(self, source, kind, target=None):
        if self.ownership.owner == "human" and kind in {"click", "input", "submit"}:
            if kind in {"click", "input"}:
                if target in self.policy.binding.controls:
                    self.ownership.manual_targets.add(target)
                else:
                    self.ownership.unmapped_manual_actions += 1
            self.evidence.emit(
                Event(
                    event="operator_action",
                    action_kind=kind,
                    target=target if target in self.policy.binding.controls else None,
                    owner="human",
                    session_id=self.ownership.session_id,
                )
            )

    async def _navigation(self, frame):
        self.generation += 1
        if self.ownership.owner == "human":
            self.evidence.emit(
                Event(
                    event="operator_action",
                    action_kind="navigation",
                    owner="human",
                    session_id=self.ownership.session_id,
                )
            )

    def check_health(self):
        if self.blocked:
            raise Stop(self.blocked, "permitted surface", "browser event blocked")
        if not self.page or self.page.is_closed():
            raise Stop("session_closed")

    def _frame(self, target: Target):
        frame = self.page.main_frame
        for name in target.frames:
            candidates = [f for f in frame.child_frames if f.name == name and not f.is_detached()]
            if len(candidates) > 1:
                raise Stop("ambiguous_frame", "one named frame", "multiple matches")
            if not candidates:
                return None
            frame = candidates[0]
        return frame

    def bind_arguments(self, arguments):
        self._arguments = dict(arguments)

    def _locator(self, target, match_input=None):
        frame = self._frame(target)
        if frame is None:
            return None
        if target.kind == "role":
            return frame.get_by_role(target.role, name=target.name, exact=True).filter(visible=True)
        if target.kind == "css":
            loc = frame.locator(target.name).filter(visible=True)
            if match_input:
                if match_input not in self._arguments:
                    return None
                value = str(self._arguments[match_input])
                if target.scope is not None:
                    scope = target.scope
                    if scope.attribute is not None:
                        if "\0" in value:
                            return None
                        # Hex-escape every code point: invocation text cannot become CSS syntax.
                        escaped = "".join(f"\\{ord(char):x} " for char in value)
                        anchor = frame.locator(
                            f':is({scope.anchor})[{scope.attribute}="{escaped}"]'
                        )
                    else:
                        anchor = frame.locator(scope.anchor).filter(
                            has_text=re.compile("^" + re.escape(value) + "$")
                        )
                    loc = (
                        frame.locator(scope.container)
                        .filter(has=anchor.filter(visible=True))
                        .locator(target.name)
                        .filter(visible=True)
                    )
                else:
                    loc = loc.filter(has_text=re.compile("^" + re.escape(value) + "$"))
            return loc
        if target.kind == "label":
            return frame.get_by_label(target.name, exact=True).filter(visible=True)
        # A header identifies a row, and that row must contain exactly one visible value cell.
        header = (
            frame.locator("th")
            .filter(has_text=re.compile("^" + re.escape(target.name) + "$"))
            .filter(visible=True)
        )
        # Only the header's own row: enclosing layout-table rows contain the
        # same header too, but their other descendants are not its value.
        row = header.locator("xpath=..")
        selector = ":scope > td input" if target.kind == "row_input" else ":scope > td"
        return row.locator(selector).filter(visible=True)

    def _control(self, key):
        return self._live_controls.get(key) or self.policy.binding.controls.get(key)

    async def _resolve(self, key):
        """Try the reviewed primary target for `key`; only on ZERO visible matches,
        try each reviewed alternate in order (never positional, never fuzzy). Any
        rung -- primary or alternate -- with more than one visible match stops the
        ladder immediately as ambiguous_target rather than trying further rungs.
        Returns (locator_or_None, count, rank) for the rung actually used, where
        rank 0 is the primary and rank N is the Nth reviewed alternate.
        """
        control = self._control(key)
        if control is None:
            return None, 0, None
        for rank, target in enumerate((control.target, *control.alternates)):
            loc = self._locator(target, control.match_input)
            count = await loc.count() if loc is not None else 0
            if count == 1:
                return loc, count, rank
            if count > 1:
                raise Stop(
                    "ambiguous_target", "exactly one visible " + key, f"{count} matches", target=key
                )
        return None, 0, None

    async def count(self, key):
        _, count, _ = await self._resolve(key)
        return count

    async def _unique(self, key, *, log_alternate=False):
        loc, count, rank = await self._resolve(key)
        if count != 1:
            raise Stop(
                "target_missing", "exactly one visible " + key, f"{count} matches", target=key
            )
        if log_alternate and rank:
            self.evidence.emit(Event(event="alternate_resolved", target=key, rank=rank))
        return loc

    def _fallback_query(self, frame, kind, role):
        if kind == "role":
            return frame.get_by_role(role).filter(visible=True)
        if kind == "label":
            return frame.locator(":is(input,textarea,select)").filter(visible=True)
        if kind in ("row_value", "row_input"):
            return frame.locator("th").filter(visible=True)
        return None

    async def _claimed_by_another_control(self, key, loc):
        """Never steal another reviewed control's element. Identity, not geometry:
        the candidate element is marked with a transient JS property (no attribute,
        so nothing in the DOM or accessibility tree changes), every OTHER control's
        primary and reviewed alternates are asked whether they resolve to that very
        element, and the mark is always removed. Elements in other frames can never
        carry the mark, so they can never match.
        """
        if await loc.count() != 1:
            return False
        await loc.evaluate("el => { el.__computerUseReplayCandidate = true; }")
        try:
            for other_key, other in self.policy.binding.controls.items():
                if other_key == key:
                    continue
                for target in (other.target, *other.alternates):
                    other_loc = self._locator(target, other.match_input)
                    if other_loc is not None and await other_loc.evaluate_all(
                        "els => els.some(el => el.__computerUseReplayCandidate === true)"
                    ):
                        return True
            return False
        finally:
            await loc.evaluate("el => { delete el.__computerUseReplayCandidate; }")

    async def find_fallback(self, key, op):
        """The verified locator fallback ladder for `key`'s OWN acting target.
        Only ever called by Execution._rescue(), itself only reached once the
        primary target and every reviewed alternate have zero visible matches
        for the step currently being performed (see engine.py's `acting_op`
        gate) -- never by observe(), condition() checks, a click's own
        postcondition, or a checkpoint.

        Tries ONLY the `normalized` rung (case/Unicode/whitespace/decoration
        -insensitive exact match, any op -- see fallback.names_equal) against
        candidates of the SAME kind/role within the SAME reviewed frame
        lineage as the primary target. A broader, fuzzy match is never acted
        on here (docs/DESIGN_CHOICES.md). More than one visible match refuses
        the whole ladder as ambiguous_target, same code a reviewed alternate
        collision already uses. Returns (Target, "normalized") or None.
        """
        control = self.policy.binding.controls[key]
        primary = control.target
        if primary.kind in ("css", "screen"):
            return None
        frame = self._frame(primary)
        query = self._fallback_query(frame, primary.kind, primary.role) if frame else None
        if query is None:
            return None
        names = await query.evaluate_all(f"els => els.map({_FALLBACK_NAME_JS[primary.kind]})")
        matches = [name for name in names if name and fallback.names_equal(name, primary.name)]
        if len(matches) > 1:
            raise Stop(
                "ambiguous_target",
                "exactly one visible " + key,
                f"{len(matches)} matches",
                target=key,
            )
        if len(matches) != 1:
            return None
        candidate = Target(
            kind=primary.kind, role=primary.role, frames=primary.frames, name=matches[0]
        )
        loc = self._locator(candidate, control.match_input)
        if loc is None or await loc.count() != 1:
            return None
        if await self._claimed_by_another_control(key, loc):
            return None
        return candidate, "normalized"

    async def _ready(self, loc):
        return await loc.evaluate_all(
            "els => els.length === 1 && !els[0].disabled && Array.from(els[0].form?.elements || []).every(field => !field.willValidate || field.validity.valid)"
        )

    async def navigate(self):
        async with self.ownership.lock:
            self.ownership.require_automation()
            url = self.policy.origin + self.policy.binding.entry
            self.policy.check_url(url)
            try:
                await self.page.goto(
                    url, wait_until="load", timeout=self.policy.binding.step_timeout * 1000
                )
            except Exception:
                self.check_health()
                raise Stop(
                    "navigation_failed", "entry screen loaded", "navigation failed"
                ) from None
            self.check_health()

    async def observe(self):
        # Reads may restart across document navigation; writes never do.
        try:
            async with asyncio.timeout(self.policy.binding.step_timeout):
                while True:
                    generation = self.generation
                    try:
                        snapshot = await self._observe_once()
                        if generation == self.generation:
                            self.last_snapshot = snapshot
                            return snapshot
                    except BrowserError as exc:
                        if not _is_navigation_race(exc):
                            raise Stop("observation_failed") from None
                    await asyncio.sleep(0.01)
        except TimeoutError:
            raise Stop("observation_timeout") from None

    async def _observe_once(self):
        self.check_health()
        for control in self.policy.binding.controls.values():
            # Absent only if EVERY reviewed rung's frame lineage is unreachable; a
            # relabeled control's alternate may live in a different reviewed frame.
            if all(self._frame(target) is None for target in (control.target, *control.alternates)):
                raise Stop("frame_missing", "configured frame lineage", "frame absent")

        async def inspect(key, control):
            boxes, loc = [], None
            # Same ladder as _resolve(): try the primary rung, then each reviewed
            # alternate in order, stopping at the first rung with ANY visible match.
            # Passive observation never raises on zero or multiple matches here --
            # only the action-time _resolve() ladder enforces the one-match rule.
            for target in (control.target, *control.alternates):
                loc = self._locator(target, control.match_input)
                boxes = (
                    await loc.evaluate_all(
                        "els => els.map(el => { const r=el.getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height}; })"
                    )
                    if loc is not None
                    else []
                )
                if boxes:
                    break
            if boxes:
                return Node(
                    target=key,
                    count=len(boxes),
                    # Always the reviewed locator, even while a rescued one is active.
                    locator=self.reviewed_targets.get(key, control.target),
                    ready=await self._ready(loc)
                    if len(boxes) == 1 and "click" in control.operations
                    else True,
                    box=boxes[0] if len(boxes) == 1 else None,
                )
            return None

        inspected = await asyncio.gather(
            *(inspect(key, control) for key, control in self.policy.binding.controls.items())
        )
        nodes = [node for node in inspected if node is not None]
        present = {n.target for n in nodes}
        states = tuple(
            key for key, state in self.policy.binding.states.items() if state.target in present
        )
        live = await self._observe_live() if self.policy.binding.live_mode == "forms" else ()
        return Snapshot(
            controls=tuple(nodes),
            states=states,
            unknown_dialogs=await self._unknown_dialogs(nodes),
            live_candidates=tuple(live),
        )

    async def _observe_live(self):
        """Export only bounded, scope-authorized form structure and safe chrome text."""
        return await LivePerception(self).observe()

    def install_groundings(self, groundings):
        self._replay_groundings = dict(groundings)

    async def pin_live_candidate(self, key):
        """Retain only a selected live target across the next observation."""
        if (
            key not in self._live_current_ids
            and key not in self._replay_groundings
            and key not in self._live_pinned_handles
        ) or key not in self._live_controls:
            raise Stop(
                "live_candidate_stale", "current live candidate", "candidate expired", target=key
            )
        if (
            len(self._live_history) >= self.policy.binding.max_steps
            and key not in self._live_history
        ):
            raise Stop(
                "live_candidate_limit", "bounded selected candidates", "selection budget exceeded"
            )
        self._live_history[key] = (self._live_controls[key], self._live_scopes[key])
        if key not in self._live_pinned_handles:
            loc = self._locator(self._live_controls[key].target)
            if loc is None or await loc.count() != 1:
                raise Stop(
                    "live_candidate_stale",
                    "current live candidate",
                    "candidate expired",
                    target=key,
                )
            handle = await loc.element_handle()
            if handle is None:
                raise Stop(
                    "live_candidate_stale", "current live candidate", "element expired", target=key
                )
            self._live_pinned_handles[key] = handle

    async def unpin_live_candidate(self, key):
        handle = self._live_pinned_handles.pop(key, None)
        if handle is not None:
            await handle.dispose()

    async def _unknown_dialogs(self, nodes):
        # Count actual visible dialogs, not control aliases. A reviewed CSS scope
        # may identify an unnamed modal; two aliases must not hide another modal.
        # Deliberately checked against the PRIMARY target only, even for a node whose
        # count came from a reviewed alternate: dialog recognition is a safety
        # backstop, and treating an alternate-only match as still-unrecognized is the
        # conservative (fail-closed) choice, not a gap in the alternates ladder.
        unknown = 0
        for frame in self.page.frames:
            dialogs = (
                await frame.get_by_role("dialog")
                .or_(frame.get_by_role("alertdialog"))
                .filter(visible=True)
                .element_handles()
            )
            try:
                for dialog in dialogs:
                    recognized = False
                    for node in nodes:
                        control = self.policy.binding.controls[node.target]
                        if node.count != 1 or self._frame(control.target) is not frame:
                            continue
                        loc = self._locator(control.target, control.match_input)
                        if loc is not None and await loc.evaluate_all(
                            "(els, dialog) => els.length === 1 && els[0] === dialog", dialog
                        ):
                            recognized = True
                            break
                    unknown += not recognized
            finally:
                for dialog in dialogs:
                    await dialog.dispose()
        return unknown

    def _paragraphs(self, target):
        return self._control(target).text_mode == "paragraphs"

    async def _text(self, loc, *, paragraphs=False, evaluate_timeout_ms=None):
        evaluate_timeout_ms = (
            self.policy.binding.step_timeout * 1000
            if evaluate_timeout_ms is None
            else evaluate_timeout_ms
        )
        if paragraphs:
            value = await loc.evaluate(
                """el => {
                    if (!el.isContentEditable || Array.from(el.childNodes).some(n =>
                        n.nodeType === 3 ? n.textContent.trim() !== '' :
                        n.nodeType !== 1 || n.tagName !== 'P')) return null;
                    return Array.from(el.children, p => p.textContent === '' ? '' : p.innerText).join('\\n');
                }""",
                timeout=evaluate_timeout_ms,
            )
            if value is None:
                raise Stop("unsupported_control", "editable paragraphs", "unsupported structure")
            return value
        # Read what the form displays, rather than an input's empty textContent or
        # a select's entire option catalog. No page values enter observations.
        return await loc.evaluate(
            """el => {
                if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return el.value;
                if (el.tagName === 'SELECT')
                    return Array.from(el.selectedOptions, option => option.label).join('\\n');
                return el.innerText;
            }""",
            timeout=evaluate_timeout_ms,
        )

    async def _checked(self, loc, *, evaluate_timeout_ms=None):
        evaluate_timeout_ms = (
            self.policy.binding.step_timeout * 1000
            if evaluate_timeout_ms is None
            else evaluate_timeout_ms
        )
        return await loc.evaluate(
            "el => el.tagName === 'INPUT' && el.type === 'checkbox' && !el.indeterminate ? el.checked : null",
            timeout=evaluate_timeout_ms,
        )

    async def condition(self, condition: Condition, arguments: dict):
        if condition.kind == "count_equals_input":
            # Deliberately primary-only: this measures a repeated-row container's own
            # cardinality against a declared count, not "does this logical control
            # resolve". Consulting alternates here would leave it ambiguous which
            # rung's row count the caller's input is actually being checked against.
            control = self.policy.binding.controls[condition.target]
            loc = self._locator(control.target, control.match_input)
            expected = arguments[condition.input]
            return loc is not None and type(expected) is int and await loc.count() == expected
        # count() (via _resolve()) already raises ambiguous_target for any rung with
        # more than one visible match, so no separate check is needed here.
        count = await self.count(condition.target)
        if condition.kind == "absent":
            return count == 0
        if count != 1:
            return False
        if condition.kind == "visible":
            return True
        try:
            loc = await self._unique(condition.target)
            # See STALE_READ_TIMEOUT: bounded well below step_timeout so a locator that
            # went stale between count() and here fails fast instead of stalling the poll.
            read_timeout = min(self.policy.binding.step_timeout, STALE_READ_TIMEOUT) * 1000
            value = (
                await self._checked(loc, evaluate_timeout_ms=read_timeout)
                if type(arguments[condition.input]) is bool
                else await self._text(
                    loc,
                    paragraphs=self._paragraphs(condition.target),
                    evaluate_timeout_ms=read_timeout,
                )
            )
        except Stop as exc:
            if exc.code != "target_missing":
                raise
            return False
        except BrowserTimeout:
            # The count() above can observe a control an instant before a concurrent
            # navigation removes it; reading it then times out waiting on a locator that
            # no longer resolves. Not yet satisfied, not a fault: the caller re-observes.
            return False
        except BrowserError as exc:
            if not _is_navigation_race(exc):
                raise
            return False
        spec = self.policy.binding.input_types.get(condition.input)
        if spec is not None and spec.kind == "multiline":
            return value == arguments[condition.input]
        return condition.matches_value(value, arguments)

    async def _select(self, loc, value, *, action_target):
        options = await loc.evaluate(
            """(el, label) => ({
                multiple: el.multiple,
                options: Array.from(el.options).filter(o => o.label === label)
                    .map(o => ({disabled: o.disabled || !!o.closest('optgroup')?.disabled}))
            })""",
            value,
        )
        if options["multiple"] or len(options["options"]) != 1:
            raise Stop(
                "selection_unavailable", "one matching option", "unsupported or nonunique option"
            )
        if not await loc.is_enabled() or options["options"][0]["disabled"]:
            raise Stop("control_not_ready", "enabled option", "option unavailable")
        await action_target.select_option(
            label=value, timeout=self.policy.binding.step_timeout * 1000
        )
        if await self._text(loc) != value:
            raise Stop("fill_mismatch")

    def presentation_context(self, capability, total=None):
        """Set once per run: the capability name and, for a replay/invoke with a
        known step count, the total for "step n/N". Discovery leaves total unset
        since the step count isn't known ahead of time.
        """
        self._tour["capability"], self._tour["total"] = capability, total

    def presentation_step(self, index):
        self._tour["index"] = index

    def note_decision(self, text):
        """One-shot caption line consumed by the next presented action, e.g.
        "model chose: click View accounts (call 4)" during discovery.
        """
        self._decision_note = text

    async def _overlay(self, box, lines):
        # Only ever called once a caller has already confirmed self.present and a
        # live page (see present_outcome()/_present_before()/_overlay_hide()), so
        # this stays a single defensive try around the one thing that can race: a
        # concurrent navigation invalidating the evaluate() call mid-flight.
        try:
            await self.page.evaluate(
                """(state) => {
                    let root = document.getElementById('__computer_use_replay_present');
                    if (!root) {
                        root = document.createElement('div');
                        root.id = '__computer_use_replay_present';
                        root.setAttribute('aria-hidden', 'true');
                        root.style.cssText =
                          'position:fixed;inset:0;pointer-events:none;z-index:2147483647;' +
                          'font-family:system-ui,sans-serif;';
                        const hl = document.createElement('div');
                        hl.id = '__computer_use_replay_present_hl';
                        hl.style.cssText =
                          'position:fixed;display:none;border:3px solid #ff5f1f;' +
                          'border-radius:4px;box-shadow:0 0 0 2px rgba(255,95,31,.35);';
                        const cap = document.createElement('div');
                        cap.id = '__computer_use_replay_present_cap';
                        cap.style.cssText =
                          'position:fixed;left:16px;bottom:16px;max-width:70vw;' +
                          'background:rgba(20,20,20,.92);color:#fff;padding:10px 14px;' +
                          'border-radius:8px;font-size:14px;line-height:1.4;white-space:pre-line;';
                        root.appendChild(hl);
                        root.appendChild(cap);
                        document.body.appendChild(root);
                    }
                    const hl = document.getElementById('__computer_use_replay_present_hl');
                    // A caption-only update (verified effect, outcome, handoff notice)
                    // has no control to point at: never leave the box where the
                    // previous control used to be.
                    hl.style.display = 'none';
                    if (state.box) {
                        hl.style.left = state.box.x + 'px';
                        hl.style.top = state.box.y + 'px';
                        hl.style.width = state.box.width + 'px';
                        hl.style.height = state.box.height + 'px';
                        hl.style.display = 'block';
                    }
                    if (state.lines) {
                        document.getElementById('__computer_use_replay_present_cap').textContent =
                            state.lines.join('\\n');
                    }
                }""",
                {"box": box, "lines": lines},
            )
        except (BrowserError, BrowserTimeout):
            # Purely cosmetic: a concurrent navigation losing the overlay update
            # must never affect the real action or its evidence.
            pass

    async def _overlay_hide(self):
        if not self.present:
            return
        try:
            await self.page.evaluate(
                "() => { const el = document.getElementById('__computer_use_replay_present');"
                " if (el) el.remove(); }"
            )
        except (BrowserError, BrowserTimeout):
            pass

    def _caption_prefix(self):
        capability = self._tour["capability"] or ""
        index, total = self._tour["index"], self._tour["total"]
        if index is None:
            return [capability]
        if total:
            return [capability, f"step {index + 1}/{total}"]
        return [capability, f"step {index + 1}"]

    async def _present_before(self, step, loc):
        control = self._control(step.target)
        lines = [*self._caption_prefix(), f"{step.op}: {control.target.name}"]
        if isinstance(step, Fill):
            lines.append(f"param: {step.input}")
        if self._decision_note:
            lines.append(self._decision_note)
            self._decision_note = None
        try:
            box = await loc.bounding_box()
        except (BrowserError, BrowserTimeout):
            box = None
        await self._overlay(box, lines)
        if self.pace:
            await asyncio.sleep(self.pace)

    async def present_outcome(self, text):
        """Caption-only update after an action settles: a verified postcondition,
        a business outcome, or an escalation/resume notice. Never a new evidence
        event -- see engine.py's presentation hooks.
        """
        if not self.present:
            return
        await self._overlay(None, [*self._caption_prefix(), text])
        if self.pace:
            await asyncio.sleep(self.pace)

    async def perform(self, step, arguments):
        async with self.ownership.lock:
            try:
                return await self._perform(step, arguments)
            finally:
                # A live candidate is pinned for at most this action, including
                # policy, readiness, presentation, and dispatch failures.
                if step.target in self._live_pinned_handles:
                    await self.unpin_live_candidate(step.target)

    async def _perform(self, step, arguments):
        self.ownership.require_automation()
        self.check_health()
        if step.target in self._live_controls:
            scope = self._live_scopes[step.target][0]
            self.policy.check_live_action(step.op, scope)
            if step.target in self._live_current_ids or step.target in self._replay_groundings:
                await self.pin_live_candidate(step.target)
            await LivePerception(self).validate_action(step.target, step.op)
            target = self._live_controls[step.target].target
            duplicates = [
                key
                for key, control in self._live_controls.items()
                if key != step.target and key in self._live_current_ids and control.target == target
            ]
            # A discovery decision may carry the prior observation's
            # logical id; one current candidate with the same reviewed
            # target is the safe alias case. Historical aliases must not
            # count as a second visible control.
            current_matches = [
                key for key in self._live_current_ids if self._live_controls[key].target == target
            ]
            if len(current_matches) > 1 or (step.target in self._live_current_ids and duplicates):
                raise Stop(
                    "ambiguous_target",
                    "exactly one visible live control",
                    "multiple matches",
                    target=step.target,
                )
            if (
                isinstance(step, Fill)
                and step.input not in self._live_controls[step.target].allowed_inputs
            ):
                raise Stop("input_target_mismatch")
        else:
            self.policy.check_action(step.op, step.target)
            if isinstance(step, Fill):
                self.policy.check_fill(step.target, step.input)
        loc = await self._unique(step.target, log_alternate=True)
        if isinstance(step, Click) and not await self._ready(loc):
            raise Stop(
                "control_not_ready",
                "enabled control with valid required fields",
                "control unavailable",
                target=step.target,
            )
        if self.present:
            await self._present_before(step, loc)
        action_target = loc
        if step.target in self._live_controls:
            # Resolve once, then act on that exact element. A lazy locator could
            # otherwise select a replacement after validation or presentation.
            await LivePerception(self).validate_action(step.target, step.op)
            action_target = self._live_pinned_handles[step.target]
        self.evidence.emit(
            Event(
                event="action_started",
                op=step.op,
                target=step.target,
                parameter=step.input if isinstance(step, Fill) else None,
            )
        )
        try:
            if isinstance(step, Click):
                await action_target.click(timeout=self.policy.binding.step_timeout * 1000)
                if self.present:
                    # The click may navigate: drop the box at once, keep the caption,
                    # so nothing stays outlined where the control used to be.
                    await self._overlay(None, None)
            elif isinstance(step, Fill):
                value = arguments[step.input]
                if type(value) is bool:
                    if await self._checked(loc) is None:
                        raise Stop(
                            "unsupported_control", "native binary checkbox", "unsupported state"
                        )
                    await action_target.set_checked(
                        value, timeout=self.policy.binding.step_timeout * 1000
                    )
                    if await self._checked(loc) is not value:
                        raise Stop("fill_mismatch")
                elif await loc.evaluate("el => el.tagName === 'SELECT'"):
                    await self._select(loc, str(value), action_target=action_target)
                else:
                    editable = await loc.evaluate("el => el.isContentEditable")
                    text = str(value)
                    lines = text.split("\n") if editable else [text]
                    await action_target.fill(
                        lines[0], timeout=self.policy.binding.step_timeout * 1000
                    )
                    for line in lines[1:]:
                        await action_target.press(
                            "Shift+Enter", timeout=self.policy.binding.step_timeout * 1000
                        )
                        if line:
                            # ElementHandle exposes sequential keyboard input as
                            # type(); Locator calls it press_sequentially().
                            type_line = getattr(
                                action_target, "press_sequentially", action_target.type
                            )
                            await type_line(line, timeout=self.policy.binding.step_timeout * 1000)
                    observed = (
                        await self._text(loc, paragraphs=self._paragraphs(step.target))
                        if editable
                        else await loc.input_value()
                    )
                    if observed != text:
                        raise Stop("fill_mismatch")
                commit_key = self._control(step.target).commit_key
                if commit_key is not None:
                    await action_target.press(
                        commit_key, timeout=self.policy.binding.step_timeout * 1000
                    )
            elif isinstance(step, Read):
                value = await self._text(loc, paragraphs=self._paragraphs(step.target))
                self.check_health()
                self.evidence.emit(Event(event="action_completed", op=step.op, target=step.target))
                return value.strip()
            else:
                raise Stop("unsupported_action")
        except BrowserTimeout:
            # A click can have reached the application even if its navigation did not settle.
            # The engine checks the postcondition; it never dispatches this action twice.
            raise Stop("effect_uncertain", "verified effect", "action timed out") from None
        self.check_health()
        self.evidence.emit(Event(event="action_completed", op=step.op, target=step.target))
        return None

    async def failure_screenshot(self):
        """Structure-preserving failure image. Every glyph, value, placeholder and
        generated/media asset is concealed -- but the elements that carry them stay
        painted as solid neutral blocks in their real layout boxes, across frames, so
        a reviewer can still see the page's structure (headings, table rows, buttons)
        instead of a nearly blank page.

        Screenshot-scoped CSS is removed by Playwright even when capture fails. No live
        DOM values are rewritten; a human can continue using the same session afterward.
        """
        await self._overlay_hide()
        name = "failure-masked.png"
        image = await self.page.screenshot(
            animations="disabled",
            style="""
                *, *::before, *::after { color: transparent !important;
                  -webkit-text-fill-color: transparent !important; text-shadow: none !important;
                  background-image: none !important; border-image: none !important;
                  list-style: none !important; caret-color: transparent !important; }
                *::before, *::after { content: none !important; }
                img, svg, canvas, video, object, embed, picture { visibility: hidden !important; }
                /* Elements that typically carry readable text get a flat neutral fill
                   sized to their own layout box, so headings/rows/buttons/links remain
                   visible as structure -- no glyph is legible, only the shape is. */
                h1, h2, h3, h4, h5, h6, p, span, td, li, label, a, small, strong, em, b,
                i, legend, caption, dt, dd, figcaption, blockquote, code, pre, summary {
                  background-color: #9fb0b0 !important;
                  border-radius: 2px !important;
                }
                /* Header cells get a distinguishable shade from value cells, so a table
                   row still reads as "header row" vs. "data row" without any text. */
                th { background-color: #7f9494 !important; border-radius: 2px !important; }
                /* Empty outlined boxes, not filled: an input's own value/placeholder is
                   never rendered, but its presence and shape stay visible. */
                input, textarea, select { color: transparent !important;
                  background-color: transparent !important; border: 1px solid #6b7a7a !important;
                  box-shadow: none !important; appearance: none !important; }
            """,
        )
        path = self.evidence.directory / name
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(image)
        return name
