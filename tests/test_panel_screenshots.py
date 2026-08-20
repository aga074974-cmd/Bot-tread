"""Tests for the panel's debug-screenshot page, including the page dumps a
failed run leaves next to its screenshots."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PANEL_PASSWORD = "panel-test-pass"
# panel.main reads these at import time and opens the SQLite file eagerly, so
# they have to be set before the import below — never against a real panel.db.
os.environ["PANEL_PASSWORD"] = PANEL_PASSWORD
os.environ["SESSION_SECRET"] = "panel-test-secret"
os.environ["PANEL_DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="panel-test-")) / "panel.db")

from panel import main as panel_main  # noqa: E402

RUN = "20260820-101500_دارونو_ab12cd34"
DUMP = "<html><body><p>در حال بارگذاری…</p></body></html>"


@pytest.fixture
def runs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for logs/screenshots holding one failed run."""
    run = tmp_path / RUN
    run.mkdir(parents=True)
    (run / "01_login_stuck.png").write_bytes(b"\x89PNG\r\n\x1a\n fake")
    (run / "page.html").write_text(DUMP, encoding="utf-8")
    monkeypatch.setattr(panel_main, "SCREENSHOT_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def client(runs_dir: Path) -> TestClient:
    # No context manager: the panel's startup hook reschedules orders and
    # starts a purge loop, neither of which belongs in a route test.
    client = TestClient(panel_main.app)
    response = client.post("/login", data={"password": PANEL_PASSWORD}, follow_redirects=False)
    assert response.status_code == 303
    return client


def test_page_dump_is_listed_with_view_and_download_links(client: TestClient):
    body = client.get("/screenshots").text

    assert f"/screenshots/{RUN}/page.html" in body
    assert f"/screenshots/{RUN}/page.html?download=1" in body
    assert "نمایش" in body and "دانلود" in body


def test_a_run_with_only_a_dump_is_still_listed(client: TestClient, runs_dir: Path):
    """A run can fail before its first screenshot lands; the dump is all there
    is, and it still has to show up."""
    early = runs_dir / "20260820-090000_وبملت_ff00ff00"
    early.mkdir()
    (early / "page.html").write_text(DUMP, encoding="utf-8")

    body = client.get("/screenshots").text

    assert f"/screenshots/{early.name}/page.html" in body


def test_dump_is_served_as_text_never_as_html(client: TestClient):
    """It is markup from someone else's site: rendering it on the panel's own
    origin would run their scripts with the panel's session."""
    response = client.get(f"/screenshots/{RUN}/page.html")

    assert response.status_code == 200
    assert response.text == DUMP
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-disposition" not in response.headers


def test_dump_can_be_downloaded(client: TestClient):
    response = client.get(f"/screenshots/{RUN}/page.html?download=1")

    assert response.status_code == 200
    assert response.text == DUMP
    assert "attachment" in response.headers["content-disposition"]


def test_screenshots_are_still_served_as_images(client: TestClient):
    response = client.get(f"/screenshots/{RUN}/01_login_stuck.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


@pytest.mark.parametrize(
    "name",
    ["secret.txt", "..%2F..%2Fpanel.db", "../../panel.db"],
)
def test_files_outside_the_listing_are_refused(client: TestClient, name: str):
    response = client.get(f"/screenshots/{RUN}/{name}", follow_redirects=False)

    assert response.status_code in (303, 307, 404)
    assert "page.html" not in response.text


def test_dumps_need_a_login(runs_dir: Path):
    anonymous = TestClient(panel_main.app)

    listing = anonymous.get("/screenshots", follow_redirects=False)
    dump = anonymous.get(f"/screenshots/{RUN}/page.html", follow_redirects=False)

    assert listing.status_code == 303 and listing.headers["location"] == "/login"
    assert dump.status_code == 303 and dump.headers["location"] == "/login"
