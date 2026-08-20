from __future__ import annotations

import contextlib
import functools
import http.server
import threading
from pathlib import Path
from urllib.parse import urlencode

import pytest

from bot.broker import mofid_playwright

SITE_DIR = Path(__file__).parent / "fake_site"
USERNAME = "0012345678"
PASSWORD = "s3cret-pass"

# Long enough to absorb a slow container, short enough that a genuinely stuck
# test fails in seconds instead of the production 45s.
TEST_READY_TIMEOUT_MS = 10_000


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


def page_dump(directory: Path) -> str | None:
    """The markup a failed run left behind, or None if it left none."""
    path = Path(directory) / mofid_playwright.PAGE_HTML_NAME
    return path.read_text(encoding="utf-8") if path.exists() else None
