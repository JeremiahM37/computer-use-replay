"""Raw fixture UI: lookup visibility, responsive layout, and keyboard restart."""

import pytest
from playwright.async_api import async_playwright, expect

from computer_use_replay.demo import serve_demo


@pytest.mark.parametrize("width", [390, 1280])
async def test_lookup_is_visible_and_can_restart_with_keyboard(width):
    async with serve_demo() as (origin, _), async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": width, "height": 800})
        await page.goto(origin)
        desk = page.frame(name="workbench")
        field = desk.get_by_label("Member identifier")
        await expect(field).to_be_visible()
        assert await desk.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await field.fill("00123")
        await field.press("Tab")
        await desk.get_by_role("button", name="Find member", exact=True).press("Enter")
        await desk.get_by_role("button", name="Open member", exact=True).click()
        await desk.get_by_role("button", name="View accounts", exact=True).click()
        await desk.get_by_role("button", name="Open savings ledger", exact=True).click()
        await expect(desk.get_by_role("heading", name="Savings ledger")).to_be_visible()
        assert await desk.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await desk.get_by_role("link", name="New member search").click()
        await expect(desk.get_by_label("Member identifier")).to_have_value("")
        for identifier, heading in [
            ("00999", "Member not found"),
            ("00000", "Validation error"),
            ("00888", "Permission denied"),
        ]:
            await desk.get_by_label("Member identifier").fill(identifier)
            await desk.get_by_role("button", name="Find member", exact=True).click()
            await expect(desk.get_by_role("heading", name=heading)).to_be_visible()
            await desk.get_by_role("link", name="New member search").click()
            await expect(desk.get_by_label("Member identifier")).to_have_value("")
        await browser.close()
