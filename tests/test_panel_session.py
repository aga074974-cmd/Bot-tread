"""Tests for the panel's login-session status light: POST /session/check and
POST /session/manual-login. Driven against tests/fake_site exactly like the
connector's own tests (tests/test_mofid_playwright.py), but through the
panel's HTTP routes — this is what actually wires the connector's
check_session()/login() into the panel, not just the template rendering
already covered in tests/test_panel_dashboard.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bot.broker import mofid_playwright
from conftest import PASSWORD, USERNAME, FakeSite


def _point_login_at(monkeypatch: pytest.MonkeyPatch, panel_main, site: FakeSite, tmp_path: Path, **scenario):
    """Wires the panel's own settings/connector at the fake site instead of
    the real broker, the same way tests/conftest.py's make_client does for
    the connector-level tests."""
    monkeypatch.setattr(mofid_playwright, "LOGIN_URL", site.url(**scenario))
    monkeypatch.setattr(panel_main.settings, "storage_state_path", str(tmp_path / "auth_state.json"))


# --------------------------------------------------------------------------
# POST /session/check
# --------------------------------------------------------------------------

async def test_check_reports_a_freshly_logged_in_session_as_valid(
    panel_client, panel_main, panel_session_store, site: FakeSite, tmp_path: Path, monkeypatch
):
    """A session saved by a real login (not just an assumption) is what
    check_session() should read back as valid."""
    state = tmp_path / "auth_state.json"
    _point_login_at(monkeypatch, panel_main, site, tmp_path)
    client = mofid_playwright.MofidPlaywrightClient(
        username=USERNAME, password=PASSWORD, storage_state_path=str(state)
    )
    await client.login()
    await client.close()

    response = panel_client.post("/session/check")

    assert response.status_code == 200
    body = response.json()
    assert body == {"valid": True, "message": "نشست فعال است", "manual_login_required": False}
    status = await panel_session_store.get()
    assert status["valid"] is True


async def test_check_relogs_in_automatically_when_the_session_is_gone(
    panel_client, panel_main, panel_session_store, site: FakeSite, tmp_path: Path, monkeypatch
):
    """No auth_state.json at all: the check finds the login form, and — per
    the panel's own spec for this button — that alone should trigger an
    automatic re-login with the saved MOFID_* credentials."""
    _point_login_at(monkeypatch, panel_main, site, tmp_path)
    monkeypatch.setattr(panel_main.settings, "username", USERNAME)
    monkeypatch.setattr(panel_main.settings, "password", PASSWORD)

    response = panel_client.post("/session/check")

    assert response.status_code == 200
    body = response.json()
    assert body == {"valid": True, "message": "ورود مجدد موفق بود", "manual_login_required": False}
    status = await panel_session_store.get()
    assert status["valid"] is True
    assert status["consecutive_failures"] == 0
    assert (tmp_path / "auth_state.json").exists()


async def test_check_reports_a_short_persian_reason_when_relogin_fails(
    panel_client, panel_main, panel_session_store, site: FakeSite, tmp_path: Path, monkeypatch
):
    _point_login_at(monkeypatch, panel_main, site, tmp_path, badlogin=1)
    monkeypatch.setattr(panel_main.settings, "username", USERNAME)
    monkeypatch.setattr(panel_main.settings, "password", PASSWORD)

    response = panel_client.post("/session/check")

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["message"] == "ورود ناموفق: نام کاربری یا کلمه عبور اشتباه است"
    status = await panel_session_store.get()
    assert status["valid"] is False
    assert status["consecutive_failures"] == 1


async def test_repeated_failures_flip_on_manual_login_required(
    panel_client, panel_main, panel_session_store, site: FakeSite, tmp_path: Path, monkeypatch
):
    _point_login_at(monkeypatch, panel_main, site, tmp_path, badlogin=1)
    monkeypatch.setattr(panel_main.settings, "username", USERNAME)
    monkeypatch.setattr(panel_main.settings, "password", PASSWORD)

    for _ in range(panel_main.MANUAL_LOGIN_THRESHOLD - 1):
        body = panel_client.post("/session/check").json()
        assert body["manual_login_required"] is False

    body = panel_client.post("/session/check").json()
    assert body["manual_login_required"] is True

    dashboard = panel_client.get("/").text
    assert '<div id="manual-login" class="card">' in dashboard


async def test_a_plain_check_does_not_move_the_failure_streak(
    panel_client, panel_main, panel_session_store, site: FakeSite, tmp_path: Path, monkeypatch
):
    """check_session() alone (session already valid, no relogin needed) is
    just a question — it must not touch the consecutive-failure counter."""
    state = tmp_path / "auth_state.json"
    _point_login_at(monkeypatch, panel_main, site, tmp_path)
    client = mofid_playwright.MofidPlaywrightClient(
        username=USERNAME, password=PASSWORD, storage_state_path=str(state)
    )
    await client.login()
    await client.close()
    await panel_session_store.record_login_failure()
    await panel_session_store.record_login_failure()

    panel_client.post("/session/check")

    status = await panel_session_store.get()
    assert status["consecutive_failures"] == 2  # unchanged by the valid check


async def test_check_requires_being_logged_into_the_panel(panel_main):
    anonymous = TestClient(panel_main.app)

    response = anonymous.post("/session/check")

    assert response.status_code == 401


# --------------------------------------------------------------------------
# POST /session/manual-login
# --------------------------------------------------------------------------

async def test_manual_login_resets_the_streak_and_saves_the_session(
    panel_client, panel_main, panel_session_store, site: FakeSite, tmp_path: Path, monkeypatch
):
    _point_login_at(monkeypatch, panel_main, site, tmp_path)  # a scenario that accepts login
    await panel_session_store.record_login_failure()
    await panel_session_store.record_login_failure()
    await panel_session_store.record_login_failure()

    response = panel_client.post(
        "/session/manual-login",
        data={"username": USERNAME, "password": PASSWORD},
        follow_redirects=False,
    )

    assert response.status_code == 303
    status = await panel_session_store.get()
    assert status["valid"] is True
    assert status["consecutive_failures"] == 0
    assert (tmp_path / "auth_state.json").exists()

    dashboard = panel_client.get("/").text
    assert "ورود دستی موفق بود." in dashboard
    assert '<div id="manual-login" class="card" hidden>' in dashboard  # streak reset, form hides again


async def test_manual_login_failure_is_flashed_in_persian_and_counts_toward_the_streak(
    panel_client, panel_main, panel_session_store, site: FakeSite, tmp_path: Path, monkeypatch
):
    _point_login_at(monkeypatch, panel_main, site, tmp_path, badlogin=1)
    await panel_session_store.record_login_failure()

    response = panel_client.post(
        "/session/manual-login",
        data={"username": "wrong-user", "password": "wrong-pass"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    dashboard = panel_client.get("/").text
    assert "ورود دستی ناموفق: نام کاربری یا کلمه عبور اشتباه است" in dashboard
    status = await panel_session_store.get()
    assert status["consecutive_failures"] == 2  # counted, not reset


async def test_manual_login_without_the_checkbox_does_not_touch_env_or_settings(
    panel_client, panel_main, panel_session_store, site: FakeSite, tmp_path: Path, monkeypatch
):
    env_path = tmp_path / ".env"
    env_path.write_text("MOFID_USERNAME=old-user\nMOFID_PASSWORD=old-pass\n", encoding="utf-8")
    monkeypatch.setattr(panel_main, "ENV_PATH", str(env_path))
    monkeypatch.setattr(panel_main.settings, "username", "old-user")
    monkeypatch.setattr(panel_main.settings, "password", "old-pass")
    _point_login_at(monkeypatch, panel_main, site, tmp_path)

    panel_client.post("/session/manual-login", data={"username": USERNAME, "password": PASSWORD})

    assert env_path.read_text(encoding="utf-8") == "MOFID_USERNAME=old-user\nMOFID_PASSWORD=old-pass\n"
    assert panel_main.settings.username == "old-user"
    assert panel_main.settings.password == "old-pass"


async def test_manual_login_with_the_checkbox_updates_env_and_live_settings(
    panel_client, panel_main, panel_session_store, site: FakeSite, tmp_path: Path, monkeypatch
):
    env_path = tmp_path / ".env"
    env_path.write_text("MOFID_USERNAME=old-user\nMOFID_PASSWORD=old-pass\nDRY_RUN=true\n", encoding="utf-8")
    monkeypatch.setattr(panel_main, "ENV_PATH", str(env_path))
    monkeypatch.setattr(panel_main.settings, "username", "old-user")
    monkeypatch.setattr(panel_main.settings, "password", "old-pass")
    _point_login_at(monkeypatch, panel_main, site, tmp_path)

    panel_client.post(
        "/session/manual-login",
        data={"username": USERNAME, "password": PASSWORD, "save": "1"},
    )

    assert panel_main.settings.username == USERNAME
    assert panel_main.settings.password == PASSWORD
    text = env_path.read_text(encoding="utf-8")
    assert f"MOFID_USERNAME={USERNAME}" in text
    assert f"MOFID_PASSWORD={PASSWORD}" in text
    assert "DRY_RUN=true" in text  # untouched


async def test_manual_login_requires_being_logged_into_the_panel(panel_main):
    anonymous = TestClient(panel_main.app)

    response = anonymous.post(
        "/session/manual-login",
        data={"username": "x", "password": "y"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
