"""Independent vendor-conformance fixture, authored from the callable task contracts.

No import of the demo, driver, policy, or locator implementation.
Two layouts share this vendor's flow; faults alter server output, not runner checks.
"""

import asyncio
import html
import socket
from contextlib import asynccontextmanager
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

BALANCES = {"00123": "$2,701.32", "00456": "$5,306.49"}


def create_vendor(layout="framed", fault="none"):
    if layout not in {"framed", "inline"}:
        raise ValueError("unsupported vendor layout")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.finalizations = 0
    app.state.posts = []
    sessions = {}

    def document(body):
        # Nested layout tables intentionally contain the same descendants as
        # inner data rows. Data cells precede headers in the inline build.
        return HTMLResponse(
            "<!doctype html><html><head><title>Vendor conformance sandbox</title></head><body><p>SYNTHETIC TEST SYSTEM</p><table><tr><td><table><tr><td>"
            + body
            + "</td></tr></table></td></tr></table></body></html>"
        )

    def row(label, value):
        cells = "<th>" + label + "</th><td>" + value + "</td>"
        if layout == "inline":
            cells = "<td>" + value + "</td><th>" + label + "</th>"
        return "<tr>" + cells + "</tr>"

    def data(rows):
        # Reorder complete rows too; no index remains stable between builds.
        if layout == "inline":
            rows = list(reversed(rows))
        return "<table>" + "".join(row(k, html.escape(v)) for k, v in rows) + "</table>"

    def action(label, path):
        return '<form method="post" action="' + path + '"><button>' + label + "</button></form>"

    def field(caption, name, default=""):
        # Deliberately no associated label, id, ARIA, or test hook.
        return (
            "<table>"
            + row(
                caption,
                '<input name="'
                + name
                + '" required value="'
                + html.escape(default, quote=True)
                + '">',
            )
            + "</table>"
        )

    def search():
        return (
            '<h2>Member search</h2><form method="post" action="/desk/search">'
            + field("Member reference", "member_key")
            + '<button>Locate record</button></form><fieldset><legend>Unrelated archive tools</legend><label>Member identifier<input value="00888"></label>'
            + action("Find member", "/desk/finalize")
            + "</fieldset>"
        )

    def identity(key):
        return data([("Member identifier", key), ("Branch", "TRAINING")])

    @app.get("/")
    async def home():
        sid = uuid4().hex
        sessions[sid] = {}
        body = (
            '<iframe name="legacy_panel" title="Legacy workspace" src="/desk/search"></iframe>'
            if layout == "framed"
            else search()
        )
        response = document(body)
        response.set_cookie("vendor_session", sid, httponly=True, samesite="strict")
        return response

    @app.get("/desk/search")
    async def entry():
        return document(search())

    @app.post("/desk/{path:path}")
    async def navigate(path: str, request: Request):
        state = sessions[request.cookies["vendor_session"]]
        values = dict(await request.form())
        app.state.posts.append(path)
        if path in {"finalize", "subaccount/create"}:
            app.state.finalizations += 1
            return document("<h2>UNEXPECTED COMMIT</h2>")
        if path == "search":
            state["member"] = str(values["member_key"])
            key = state["member"]
            if key == "00888":
                return document("<h2>Permission denied</h2>")
            if key == "00000":
                return document("<h2>Validation error</h2>")
            if key not in BALANCES:
                return document("<h2>Member not found</h2>")
            return document("<h2>Search results</h2>" + action("Open member", "/desk/member"))
        key = state["member"]
        if path == "member":
            return document(
                "<h2>Member summary</h2>"
                + identity(key)
                + action("Account catalogue", "/desk/accounts")
            )
        if path == "accounts":
            return document(
                "<h2>Account directory</h2>"
                + data([("Checking", "NOT SAVINGS"), ("Savings", "Requested product")])
                + action("Open savings ledger", "/desk/savings")
                + action("Start sub-account", "/desk/subaccount")
            )
        if path == "savings":
            return document(
                "<h2>Savings account</h2>"
                + identity(key)
                + data([("Available balance", BALANCES[key]), ("Current balance", "$9,999.99")])
            )
        if path == "subaccount":
            return document(
                '<h2>Sub-account setup</h2><form method="post" action="/desk/subaccount/review">'
                + field("Account alias", "nickname", "OLD DEFAULT")
                + "<button>Review sub-account</button></form>"
            )
        if path == "subaccount/review":
            nickname = str(values["nickname"])
            if fault == "nickname":
                nickname = "OLD DEFAULT"
            rows = [
                ("Member identifier", key),
                ("Nickname", nickname),
                ("Review status", "Rejected" if fault == "status" else "Ready for confirmation"),
            ]
            if fault == "duplicate":
                rows.append(("Nickname", nickname))
            return document(
                "<h2>Sub-account confirmation</h2>"
                + data(rows)
                + action("Create sub-account", "/desk/subaccount/create")
            )
        return HTMLResponse("Unknown route", status_code=404)

    return app


@asynccontextmanager
async def serve_vendor(layout="framed", fault="none"):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    app = create_vendor(layout, fault)
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", access_log=False, lifespan="off")
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if task.done():
                await task
            await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}", app
    finally:
        server.should_exit = True
        await task
        sock.close()
