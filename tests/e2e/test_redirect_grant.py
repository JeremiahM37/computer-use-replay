"""A reviewed same-origin redirect grant is followed for exactly one hop;
anything not explicitly reviewed still fails closed, same as before the grant
existed (see test_allowed_destination_redirect_still_not_followed)."""

import json
from pathlib import Path

import pytest
from fastapi.responses import RedirectResponse

from computer_use_replay.policy import Binding, Stop


@pytest.fixture
def binding():
    # Same product/controls as the default fixture; only request_rules differ,
    # granting the entry route ("/") a reviewed one-hop redirect to the real
    # search screen. Loaded fresh so the grant re-validates against routes.
    raw = json.loads(Path("profiles/juniper.json").read_text())
    raw["request_rules"] = [{"path": "/", "methods": ["GET"], "redirect_to": ["/desk/search"]}]
    return Binding.model_validate(raw)


async def test_reviewed_redirect_is_followed_to_its_exact_grant(live):
    async with live() as (ex, app):
        app.router.routes[:] = [r for r in app.router.routes if r.path != "/"]

        @app.get("/")
        async def redirect():
            return RedirectResponse("/desk/search")

        await ex.surface.navigate()
        assert ex.surface.blocked is None
        assert "Member search" in await ex.surface.page.content()


async def test_redirect_to_an_ungranted_destination_still_fails_closed(live):
    async with live() as (ex, app):
        app.router.routes[:] = [r for r in app.router.routes if r.path != "/"]

        @app.get("/")
        async def redirect():
            # "/desk/ready" is a real allowed GET route, but this binding's
            # grant only reviews "/desk/search" — an unreviewed destination
            # must still refuse, even though it is independently permitted.
            return RedirectResponse("/desk/ready")

        with pytest.raises(Stop, match="redirect_not_allowed"):
            await ex.surface.navigate()


async def test_a_second_hop_is_never_followed(live):
    async with live() as (ex, app):
        app.router.routes[:] = [r for r in app.router.routes if r.path not in {"/", "/desk/search"}]

        @app.get("/")
        async def redirect():
            return RedirectResponse("/desk/search")

        @app.get("/desk/search")
        async def chained():
            # The granted destination itself redirects again. Only one hop is
            # ever reviewed and fetched; a further hop is refused, not chased.
            return RedirectResponse("/desk/ready")

        with pytest.raises(Stop, match="redirect_not_allowed"):
            await ex.surface.navigate()
