from __future__ import annotations

import contextlib
import functools
import http.server
import os
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlencode

import anyio.from_thread
import pytest

from bot.broker import mofid_playwright

SITE_DIR = Path(__file__).parent / "fake_site"
USERNAME = "0012345678"
PASSWORD = "s3cret-pass"

# Long enough to absorb a slow container, short enough that a genuinely stuck
# test fails in seconds instead of the production 45s.
TEST_READY_TIMEOUT_MS = 10_000

# What the connector ships with, captured before any test shortens it — the
# test that checks the post-submit screenshots really lag behind each other
# uses these.
SHIPPED_MIDPOINT_MS = mofid_playwright.SUBMIT_MIDPOINT_MS
SHIPPED_SETTLE_MS = mofid_playwright.SUBMIT_SETTLE_MS


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # keep pytest output readable
        pass


class FakeSite:
    """The stand-in broker UI in tests/fake_site, served over http so the
    connector reaches it exactly the way it reaches the real site."""

    def __init__(self, port: int) -> None:
        self.port = port

    def url(self, **scenario: object) -> str:
        query = urlencode({k: v for k, v in scenario.items() if v is not None})
        return f"http://127.0.0.1:{self.port}/index.html" + (f"?{query}" if query else "")


@pytest.fixture(scope="session")
def site() -> FakeSite:
    handler = functools.partial(_QuietHandler, directory=str(SITE_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield FakeSite(server.server_port)
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def fast_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mofid_playwright, "PAGE_READY_TIMEOUT_MS", TEST_READY_TIMEOUT_MS)
    monkeypatch.setattr(mofid_playwright, "SUCCESS_TIMEOUT_MS", 3_000)
    monkeypatch.setattr(mofid_playwright, "SUBMIT_MIDPOINT_MS", 50)
    monkeypatch.setattr(mofid_playwright, "SUBMIT_SETTLE_MS", 150)


@pytest.fixture
async def make_client(site: FakeSite, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build a client pointed at the fake site. Keyword arguments become query
    params selecting a scenario, e.g. make_client(input="stubborn")."""
    clients: list[mofid_playwright.MofidPlaywrightClient] = []

    def _make(
        *,
        dry_run: bool = False,
        storage_state_path: str | Path | None = None,
        screenshot_dir: str | Path | None = None,
        on_login_result=None,
        **scenario: object,
    ) -> mofid_playwright.MofidPlaywrightClient:
        monkeypatch.setattr(mofid_playwright, "LOGIN_URL", site.url(**scenario))
        client = mofid_playwright.MofidPlaywrightClient(
            username=USERNAME,
            password=PASSWORD,
            dry_run=dry_run,
            headless=True,
            screenshot_dir=screenshot_dir if screenshot_dir is not None else tmp_path / "shots",
            storage_state_path=str(storage_state_path or tmp_path / "auth_state.json"),
            on_login_result=on_login_result,
        )
        clients.append(client)
        return client

    yield _make

    for client in clients:
        with contextlib.suppress(Exception):
            await client.close()


async def record(client: mofid_playwright.MofidPlaywrightClient) -> dict:
    """What the fake site saw the connector do."""
    return await client._page.evaluate("window.__record")


def shots(directory: Path) -> list[str]:
    return sorted(p.name for p in Path(directory).glob("*.png"))


# ---------------------------------------------------------------------------
# panel
# ---------------------------------------------------------------------------
PANEL_PASSWORD = "panel-test-pass"

# panel.main reads these when it is imported and opens its SQLite file eagerly,
# so they are set here — conftest is imported before any test module — and
# never point at a real panel.db.
os.environ["PANEL_PASSWORD"] = PANEL_PASSWORD
os.environ["SESSION_SECRET"] = "panel-test-secret"
os.environ["PANEL_DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="panel-test-")) / "panel.db")


@pytest.fixture(scope="session")
def panel_main():
    from panel import main

    return main


@pytest.fixture
def panel_store(panel_main, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A database of its own for each test, so orders never leak between them."""
    from panel.db import OrderStore

    store = OrderStore(str(tmp_path / "panel.db"))
    monkeypatch.setattr(panel_main, "store", store)
    return store


@pytest.fixture
def panel_session_store(panel_main, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A session-state database of its own for each test, for the same
    isolation reason as panel_store above — the login-status light's streak
    must never leak between tests either."""
    from panel.session_state import SessionStateStore

    store = SessionStateStore(str(tmp_path / "session.db"))
    monkeypatch.setattr(panel_main, "session_store", store)
    return store


@pytest.fixture(autouse=True)
def fast_manual_login_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """manual_login.py's own version of fast_timeouts above — a real second
    per poll would make every test in tests/test_manual_login.py and
    tests/test_panel_manual_login.py needlessly slow."""
    from panel import manual_login

    monkeypatch.setattr(manual_login, "POLL_INTERVAL_SECONDS", 0.3)


@pytest.fixture
def panel_manual_login_session(panel_main, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A ManualLoginSession of its own for each test — the real one in
    panel/main.py is a module-level singleton, and reusing it across tests
    would leak a running Xvfb/browser/x11vnc from one test into the next.
    Wired to the app's own on_success hook, so a successful login here goes
    through the exact same session_store update a real one would (and lands
    in the already-isolated panel_session_store, since that hook reads
    panel_main.session_store by name at call time)."""
    from panel.manual_login import ManualLoginSession

    session = ManualLoginSession(
        storage_state_path=str(tmp_path / "auth_state.json"),
        on_success=panel_main._on_manual_login_success,
    )
    monkeypatch.setattr(panel_main, "manual_login_session", session)
    return session


@pytest.fixture
def panel_client(panel_main, panel_store, panel_session_store, panel_manual_login_session):
    """Signed in. Deliberately not used as a context manager: the panel's
    startup hook reschedules orders and starts a purge loop, and neither
    belongs in a route test."""
    from fastapi.testclient import TestClient

    client = TestClient(panel_main.app)
    response = client.post("/login", data={"password": PANEL_PASSWORD}, follow_redirects=False)
    assert response.status_code == 303
    return client


@pytest.fixture
def panel_client_one_loop(panel_client, panel_manual_login_session):
    """panel_client, but every call reuses one pinned event loop instead of
    TestClient's default of a fresh thread+loop per request (see Starlette's
    TestClient._portal_factory). Ordinary request/response tests never
    notice, but manual-login's start() creates a background asyncio.Task
    tied to whichever loop handles that one request — a later call's own
    fresh loop can neither observe nor await it, so the poll loop silently
    freezes mid-sleep (a real login is never detected) and a later cancel()/
    status() call that tries to await that orphaned task deadlocks forever.
    One shared loop for the whole test matches production, where a single
    uvicorn event loop runs for the app's entire lifetime.

    Deliberately not `with TestClient(...) as client:` — that also drives
    the app's startup lifespan (order rescheduling, purge loops), which
    panel_client's own docstring explains is kept out of route tests.

    Teardown drains panel_manual_login_session through this same portal —
    a session left mid cleanup (e.g. a test that let a login succeed
    naturally instead of cancelling) is still running on this pinned loop,
    and closing the portal out from under it would orphan its Xvfb/x11vnc
    exactly like an un-awaited poll task did before this fixture existed."""
    with anyio.from_thread.start_blocking_portal() as portal:
        panel_client.portal = portal
        try:
            yield panel_client
        finally:
            async def _drain_manual_login_session() -> None:
                with contextlib.suppress(Exception):
                    await panel_manual_login_session.cancel()
                poll_task = panel_manual_login_session._poll_task
                if poll_task is not None:
                    with contextlib.suppress(Exception):
                        await poll_task

            portal.call(_drain_manual_login_session)
            panel_client.portal = None


def has_shot(directory: Path, label: str) -> bool:
    """Whether a screenshot with this label was taken, whatever its number.
    Only test_every_step_leaves_a_numbered_screenshot pins the numbering."""
    return any(name.endswith(f"_{label}.png") for name in shots(directory))


def shot_named(directory: Path, label: str) -> Path:
    """The actual numbered file for a label, whatever its number turned out
    to be — for a test that needs the file itself, not just whether it exists."""
    matches = [name for name in shots(directory) if name.endswith(f"_{label}.png")]
    assert len(matches) == 1, f"expected exactly one shot for {label!r}, found {matches}"
    return Path(directory) / matches[0]


def page_dump(directory: Path) -> str | None:
    """The markup a failed run left behind, or None if it left none."""
    path = Path(directory) / mofid_playwright.PAGE_HTML_NAME
    return path.read_text(encoding="utf-8") if path.exists() else None
