"""Tests for the panel's /manual-login routes: the page, the start/status/
cancel JSON endpoints, the auth-gated noVNC static file server, and the
authenticated WebSocket proxy to the real x11vnc it starts. Driven against
tests/fake_site through the actual panel routes, the same way
tests/test_panel_session.py drives /session/check — real Xvfb, real
non-headless Chromium, real x11vnc, real RFB bytes over the proxy.
"""
from __future__ import annotations

import asyncio
import socket

import pytest
from fastapi.testclient import TestClient

from bot.broker import mofid_playwright
from conftest import PASSWORD, USERNAME, FakeSite


def _point_at(monkeypatch: pytest.MonkeyPatch, site: FakeSite, **scenario) -> None:
    monkeypatch.setattr(mofid_playwright, "LOGIN_URL", site.url(**scenario))


async def _wait_until(condition, timeout: float = 6.0, interval: float = 0.1) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(interval)
    return False


# --------------------------------------------------------------------------
# auth gates
# --------------------------------------------------------------------------

async def test_the_page_requires_auth(panel_main):
    anonymous = TestClient(panel_main.app)

    response = anonymous.get("/manual-login", follow_redirects=False)

    assert response.status_code == 303 and response.headers["location"] == "/login"


async def test_start_requires_auth(panel_main):
    anonymous = TestClient(panel_main.app)

    response = anonymous.post("/manual-login/start")

    assert response.status_code == 401


async def test_status_requires_auth(panel_main):
    anonymous = TestClient(panel_main.app)

    response = anonymous.get("/manual-login/status")

    assert response.status_code == 401


async def test_cancel_requires_auth(panel_main):
    anonymous = TestClient(panel_main.app)

    response = anonymous.post("/manual-login/cancel")

    assert response.status_code == 401


async def test_novnc_asset_requires_auth(panel_main):
    anonymous = TestClient(panel_main.app)

    response = anonymous.get("/manual-login/novnc/vnc_lite.html", follow_redirects=False)

    assert response.status_code == 303 and response.headers["location"] == "/login"


async def test_websocket_proxy_rejects_an_unauthenticated_connection(panel_main):
    anonymous = TestClient(panel_main.app)

    with pytest.raises(Exception):
        with anonymous.websocket_connect("/manual-login/vnc"):
            pass


# --------------------------------------------------------------------------
# the page itself
# --------------------------------------------------------------------------

async def test_the_page_renders_the_novnc_iframe_and_autostarts(panel_client: TestClient):
    body = panel_client.get("/manual-login").text

    assert 'id="manual-login-frame"' in body
    # The iframe's src is assigned by JS only once status is starting/waiting
    # (see manual_login.html's renderStatus) rather than baked in server-side,
    # so the browser never attempts a VNC connection before one exists.
    assert "/manual-login/novnc/vnc_lite.html?path=" in body
    assert "encodeURIComponent('manual-login/vnc')" in body
    assert "startManualLogin()" in body  # called unconditionally on load


# --------------------------------------------------------------------------
# serving noVNC's own static files
# --------------------------------------------------------------------------

async def test_novnc_asset_serves_the_real_vnc_lite_html(panel_client: TestClient):
    response = panel_client.get("/manual-login/novnc/vnc_lite.html")

    assert response.status_code == 200
    assert "RFB" in response.text


async def test_novnc_asset_serves_nested_js_modules(panel_client: TestClient):
    """vnc_lite.html imports ./core/rfb.js as an ES module — the whole tree
    under novnc, not just the top-level html file, has to be reachable."""
    response = panel_client.get("/manual-login/novnc/core/rfb.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


async def test_novnc_asset_blocks_path_traversal(panel_client: TestClient):
    # A literal "../" gets collapsed by the HTTP client itself before the
    # request is even sent (httpx normalizes the URL), landing on the
    # unrelated /etc/passwd path with no route at all — a 404 that has
    # nothing to do with _resolve_novnc_file's own guard. %2f-encoded
    # separators survive that normalization and reach the route handler as
    # a literal string, which is the payload that actually exercises (and,
    # without the guard, defeats) the containment check.
    response = panel_client.get("/manual-login/novnc/..%2f..%2f..%2f..%2f..%2fetc%2fpasswd")

    assert response.status_code == 404
    assert "root:" not in response.text


async def test_novnc_asset_404s_for_an_unknown_file(panel_client: TestClient):
    response = panel_client.get("/manual-login/novnc/does-not-exist.html")

    assert response.status_code == 404


# --------------------------------------------------------------------------
# the real start / status / cancel flow
# --------------------------------------------------------------------------

async def test_start_status_cancel_flow_through_the_real_routes(
    panel_client_one_loop: TestClient, panel_manual_login_session, site: FakeSite, monkeypatch
):
    panel_client = panel_client_one_loop
    _point_at(monkeypatch, site, markers="all")

    start_body = panel_client.post("/manual-login/start").json()
    assert start_body["state"] == "waiting_for_login"

    status_body = panel_client.get("/manual-login/status").json()
    assert status_body["state"] == "waiting_for_login"

    cancel_body = panel_client.post("/manual-login/cancel").json()
    assert cancel_body["state"] == "cancelled"

    final = panel_client.get("/manual-login/status").json()
    assert final["state"] == "cancelled"


# --------------------------------------------------------------------------
# the vnc proxy itself, authenticated, against the real x11vnc
# --------------------------------------------------------------------------

async def test_websocket_proxy_streams_real_rfb_bytes_once_authenticated(
    panel_client_one_loop: TestClient, panel_manual_login_session, site: FakeSite, monkeypatch
):
    panel_client = panel_client_one_loop
    _point_at(monkeypatch, site, markers="all")
    panel_client.post("/manual-login/start")
    await _wait_until(lambda: panel_manual_login_session.status()["state"] == "waiting_for_login")

    with panel_client.websocket_connect("/manual-login/vnc") as ws:
        handshake = ws.receive_bytes()

    assert handshake == b"RFB 003.008\n"

    panel_client.post("/manual-login/cancel")


# --------------------------------------------------------------------------
# a full login end to end through the real routes
# --------------------------------------------------------------------------

async def test_a_real_login_through_the_page_updates_the_session_store(
    panel_client_one_loop: TestClient, panel_manual_login_session, panel_session_store, site: FakeSite, monkeypatch
):
    panel_client = panel_client_one_loop
    _point_at(monkeypatch, site, markers="all")
    panel_client.post("/manual-login/start")
    await _wait_until(lambda: panel_manual_login_session.status()["state"] == "waiting_for_login")

    # what a person does through the VNC view, driven directly against the
    # same real page for the reason test_manual_login.py gives: this checks
    # the panel's own wiring end to end, not noVNC's canvas/input handling.
    # That page was created inside the /start request's handler, so — like
    # everything else about panel_client_one_loop — it lives on the pinned
    # portal loop, not this test function's own loop; driving it directly
    # with a plain `await` here would be a cross-loop call into Playwright's
    # connection and hang exactly like the unpinned client did. Routing
    # through the same portal keeps it on the one loop that actually owns it.
    portal = panel_client_one_loop.portal
    page = panel_manual_login_session._page
    portal.call(page.locator("#user-name").fill, USERNAME)
    portal.call(page.locator("#password").fill, PASSWORD)
    portal.call(page.locator("button[type='submit']").click)

    reached = await _wait_until(
        lambda: panel_manual_login_session.status()["state"] != "waiting_for_login"
    )
    assert reached
    assert panel_manual_login_session.status()["state"] == "success"

    status = await panel_session_store.get()
    assert status["valid"] is True
    assert status["consecutive_failures"] == 0

    body = panel_client.get("/manual-login/status").json()
    assert body["state"] == "success"
