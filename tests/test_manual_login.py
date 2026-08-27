"""Tests for panel/manual_login.py's ManualLoginSession — a real, non-headless
Chromium on a real Xvfb display, shared out over a real x11vnc, driven
against tests/fake_site exactly like the connector's own tests. These are
genuinely heavier than the rest of the suite (each start() boots three real
processes), so fast_manual_login_poll (conftest.py) keeps the polling loop
itself from adding to that.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import socket
import stat
from pathlib import Path

import pytest

from bot.broker import mofid_playwright
from panel.manual_login import ManualLoginSession, ManualLoginState
from conftest import PASSWORD, USERNAME, FakeSite


_next_display = itertools.count(78)


@pytest.fixture
async def manual_login(site: FakeSite, tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A session pointed at the fake site, with its own storage_state path
    and — every call — its own never-before-used display/port. Even with
    this class's own defensive stale-lock cleanup (see
    ManualLoginSession._remove_stale_xvfb_lock), a forcibly-killed Xvfb from
    one test can still be mid-teardown when the next test's fixture starts
    a new one; a fresh number each time removes the question entirely."""
    monkeypatch.setattr(mofid_playwright, "LOGIN_URL", site.url(markers="all"))
    sessions: list[ManualLoginSession] = []

    def _make() -> ManualLoginSession:
        number = next(_next_display)
        session = ManualLoginSession(
            storage_state_path=str(tmp_path / "auth_state.json"),
            display=f":{number}",
            vnc_port=15900 + number,
        )
        sessions.append(session)
        return session

    yield _make

    for session in sessions:
        await session.cancel()
        # cancel() is a no-op once a session already reached a terminal state
        # on its own (e.g. SUCCESS) — but the poll task that got it there
        # keeps running past that point to do its own cleanup (see
        # _on_login_detected). A test that doesn't explicitly wait for that
        # cleanup to finish would otherwise let pytest tear down this test's
        # event loop while it's still mid-flight, orphaning the Xvfb/x11vnc
        # pair for good. Awaiting the task here is a no-op if it's already
        # done, and otherwise just waits for the cleanup already in progress.
        poll_task = session._poll_task
        if poll_task is not None:
            with contextlib.suppress(Exception):
                await poll_task


async def _fill_in_the_login_form(session: ManualLoginSession) -> None:
    """Exactly what a person does through the VNC view — used here to drive
    the same real page directly, since these tests check the detection and
    capture logic itself rather than noVNC's own canvas/input handling."""
    await session._page.locator("#user-name").fill(USERNAME)
    await session._page.locator("#password").fill(PASSWORD)
    await session._page.locator("button[type='submit']").click()


async def _wait_until(condition, timeout: float = 6.0, interval: float = 0.1) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(interval)
    return False


async def test_initial_status_is_idle(manual_login):
    session = manual_login()
    assert session.status() == {"state": "idle", "detail": ""}


async def test_start_reaches_waiting_for_login_with_the_real_device_profile(manual_login):
    session = manual_login()

    status = await session.start()

    assert status["state"] == ManualLoginState.WAITING_FOR_LOGIN.value
    # The exact same emulation the headless connector uses — a mismatch here
    # is exactly the bug this whole feature must not have.
    user_agent = await session._page.evaluate("navigator.userAgent")
    has_touch = await session._page.evaluate("'ontouchstart' in window")
    viewport_width = await session._page.evaluate("window.innerWidth")
    assert "Android" in user_agent
    assert has_touch is True
    assert viewport_width < 500


async def test_a_second_start_while_running_does_not_spawn_a_second_browser(manual_login):
    session = manual_login()
    await session.start()
    first_browser = session._browser

    status = await session.start()

    assert status["state"] == ManualLoginState.WAITING_FOR_LOGIN.value
    assert session._browser is first_browser


async def test_the_vnc_server_is_reachable_while_waiting(manual_login):
    session = manual_login()
    await session.start()

    conn = socket.create_connection(("127.0.0.1", session._vnc_port), timeout=3)
    try:
        handshake = conn.recv(64)
    finally:
        conn.close()
    assert handshake == b"RFB 003.008\n"


async def test_cancel_when_idle_is_a_safe_no_op(manual_login):
    session = manual_login()

    status = await session.cancel()

    assert status == {"state": "idle", "detail": ""}


async def test_cancel_tears_everything_down(manual_login):
    session = manual_login()
    await session.start()
    vnc_port = session._vnc_port

    status = await session.cancel()

    assert status["state"] == ManualLoginState.CANCELLED.value
    assert session._browser is None
    assert session._xvfb is None
    assert session._x11vnc is None
    assert session._poll_task is None
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", vnc_port), timeout=1)


async def test_a_cancelled_session_leaves_no_auth_state_file(manual_login, tmp_path):
    session = manual_login()
    await session.start()

    await session.cancel()

    assert not (tmp_path / "auth_state.json").exists()


async def test_starting_again_after_a_cancel_works(manual_login):
    session = manual_login()
    await session.start()
    await session.cancel()

    status = await session.start()

    assert status["state"] == ManualLoginState.WAITING_FOR_LOGIN.value


async def test_a_completed_login_is_captured_and_reported(manual_login, tmp_path):
    calls = []

    async def on_success():
        calls.append(True)

    session = manual_login()
    session._on_success = on_success
    await session.start()

    await _fill_in_the_login_form(session)
    reached_success = await _wait_until(lambda: session.status()["state"] != "waiting_for_login")

    assert reached_success
    assert session.status()["state"] == ManualLoginState.SUCCESS.value
    assert calls == [True]

    state_file = tmp_path / "auth_state.json"
    assert state_file.exists()
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    assert '"origins"' in state_file.read_text(encoding="utf-8")


async def test_a_completed_login_cleans_up_the_browser_and_display(manual_login):
    session = manual_login()
    await session.start()
    vnc_port = session._vnc_port

    await _fill_in_the_login_form(session)
    await _wait_until(lambda: session.status()["state"] != "waiting_for_login")
    # the state flips to SUCCESS just before the cleanup that follows it, and
    # _x11vnc/_xvfb are the last things that cleanup clears — wait for those
    # specifically, or the port check below can race a cleanup still in
    # progress.
    await _wait_until(lambda: session._x11vnc is None, timeout=3.0)

    assert session._browser is None
    assert session._xvfb is None
    assert session._x11vnc is None
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", vnc_port), timeout=1)


async def test_a_stale_xvfb_lock_from_a_dead_process_does_not_block_start(manual_login):
    """On X11 servers that enforce /tmp/.X{N}-lock strictly, a lock left
    behind by an ungraceful (SIGKILL) shutdown — a host crash, an OOM-kill,
    even this class's own SIGKILL fallback in _cleanup_resources — refuses
    a future Xvfb the same display number even though the process it names
    is long gone. The Xvfb build in this sandbox happens to already recover
    from that on its own (verified separately, directly against Xvfb), so
    this integration-level test can only confirm start() still succeeds
    with such a lock in place, not that _remove_stale_xvfb_lock was what
    made it succeed — see the two unit tests below for that: they call
    _remove_stale_xvfb_lock directly and are what actually pins its
    dead-pid-only removal contract down."""
    session = manual_login()

    dead = await asyncio.create_subprocess_exec(
        "true", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await dead.wait()  # definitely dead now, and its pid won't be recycled this fast

    lock_path = Path(f"/tmp/.X{session._display.lstrip(':')}-lock")
    lock_path.write_text(f"{dead.pid}\n")

    try:
        status = await session.start()
        assert status["state"] == ManualLoginState.WAITING_FOR_LOGIN.value
    finally:
        await session.cancel()


async def test_remove_stale_xvfb_lock_removes_a_lock_naming_a_dead_process(manual_login, tmp_path):
    session = manual_login()
    dead = await asyncio.create_subprocess_exec(
        "true", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await dead.wait()
    lock_path = tmp_path / "dead.lock"
    lock_path.write_text(f"{dead.pid}\n")

    session._remove_stale_xvfb_lock(lock_path)

    assert not lock_path.exists()


async def test_remove_stale_xvfb_lock_removes_an_unparseable_lock(manual_login, tmp_path):
    """A lock file that isn't even a plain pid (corrupted, truncated, or
    written by something else entirely) is just as useless as one naming a
    dead process — nothing can ever confirm it's safe to keep, so it's
    removed the same way."""
    session = manual_login()
    lock_path = tmp_path / "garbage.lock"
    lock_path.write_text("not-a-pid")

    session._remove_stale_xvfb_lock(lock_path)

    assert not lock_path.exists()


async def test_remove_stale_xvfb_lock_leaves_a_missing_lock_alone(manual_login, tmp_path):
    session = manual_login()
    lock_path = tmp_path / "does-not-exist.lock"

    session._remove_stale_xvfb_lock(lock_path)  # must not raise

    assert not lock_path.exists()


async def test_a_live_process_holding_the_lock_is_left_alone(manual_login):
    """The flip side of the tests above: a lock naming a process that is
    genuinely still alive must NOT be removed out from under it — only a
    confirmed-dead owner justifies clearing the lock."""
    session = manual_login()
    lock_path = Path(f"/tmp/.X{session._display.lstrip(':')}-lock")
    lock_path.write_text(f"{os.getpid()}\n")  # this test process: definitely alive

    session._remove_stale_xvfb_lock(lock_path)

    assert lock_path.exists()
    lock_path.unlink()
