"""Tests for the panel's screenshot/page-dump file route (/screenshots/<run>/
<name>), which still serves every order's gallery even though the separate
/screenshots listing page it originally backed is gone — see
test_panel_gallery.py for the gallery itself."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

RUN = "20260820-101500_دارونو_ab12cd34"
DUMP = "<html><body><p>در حال بارگذاری…</p></body></html>"


@pytest.fixture
def runs_dir(panel_main, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for logs/screenshots holding one failed run."""
    run = tmp_path / RUN
    run.mkdir(parents=True)
    (run / "01_login_stuck.png").write_bytes(b"\x89PNG\r\n\x1a\n fake")
    (run / "page.html").write_text(DUMP, encoding="utf-8")
    monkeypatch.setattr(panel_main, "SCREENSHOT_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def client(runs_dir: Path, panel_client: TestClient) -> TestClient:
    return panel_client


def test_the_old_listing_page_is_gone(client: TestClient):
    """/screenshots (no run/name) used to list every run; each order's own
    gallery replaced it. The file route below it, /screenshots/<run>/<name>,
    is a different route and stays."""
    assert client.get("/screenshots").status_code == 404


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
    """Only run/name pairs that actually appear in the directory listing are
    served, so a crafted path can never escape SCREENSHOT_DIR. A refusal now
    lands on the dashboard rather than the removed listing page."""
    response = client.get(f"/screenshots/{RUN}/{name}", follow_redirects=False)

    assert response.status_code in (303, 307, 404)
    if response.status_code == 303:
        assert response.headers["location"] == "/"
    assert "page.html" not in response.text


def test_a_file_route_needs_a_login(runs_dir: Path, panel_main):
    anonymous = TestClient(panel_main.app)

    response = anonymous.get(f"/screenshots/{RUN}/page.html", follow_redirects=False)

    assert response.status_code == 303 and response.headers["location"] == "/login"
