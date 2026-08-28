"""Manual login via a live, remotely-viewable browser.

Automating the login form is brittle (a redesigned field breaks it) and
cannot get past anything meant to stop automation in the first place — a
CAPTCHA, an SMS/OTP prompt, whatever the broker adds next. This module
sidesteps all of that by handing a human a real browser instead: a
non-headless Chromium is launched on a virtual X display (Xvfb), shared out
read/write over VNC (x11vnc), and the moment the app's own navbar shows up —
exactly the signal MofidPlaywrightClient.login() already uses — the
session's storage_state is captured to auth_state.json, the same file an
automated login() would have written.

This is meant to become the login-status light's permanent fallback once a
few automatic logins fail in a row (see panel/main.py's
MANUAL_LOGIN_THRESHOLD) instead of the username/password form — kept in its
own module, with ManualLoginSession as the whole public surface, so wiring
that up later is a matter of calling .start() from there instead of here.
Independent of panel/main.py otherwise: it only needs a storage_state path
and an optional on_success callback, so it is just as usable stand-alone.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from bot.broker import mofid_playwright

log = logging.getLogger(__name__)

# A fixed display/port: only one admin drives this panel at a time, so there
# is no need to hand out a fresh pair per session — and a fixed pair means a
# crashed-and-restarted panel can find (and clean up) a stale one by name.
DISPLAY = ":77"
VNC_PORT = 5977
# The screen is sized around two measured facts about Chromium on a display
# with no window manager, both of which cut the page off before:
#
#   * it refuses to make a window narrower than 500px, so a 480px-wide screen
#     got a 500px window with 20px hanging off the right edge, and
#   * its tab strip and address bar always cost CHROME_UI_HEIGHT of the
#     window, whatever the window's size (--kiosk cannot reclaim them: with
#     no window manager to ask, it is silently ignored).
#
# So the screen is the page area we actually want, plus that chrome, at a
# width Chromium will agree to. 520px keeps the site firmly in its mobile
# layout while clearing the 500px floor.
PAGE_WIDTH = 520
PAGE_HEIGHT = 920
CHROME_UI_HEIGHT = 88  # measured; the page also loses a 1px window border
SCREEN_WIDTH = PAGE_WIDTH
SCREEN_HEIGHT = PAGE_HEIGHT + CHROME_UI_HEIGHT
# 16-bit colour (not 24) roughly halves what x11vnc has to push over the
# websocket on every change, which matters more than colour depth ever will
# for a login form on a mobile connection.
SCREEN_DEPTH = 16
SCREEN_SIZE = f"{SCREEN_WIDTH}x{SCREEN_HEIGHT}x{SCREEN_DEPTH}"

POLL_INTERVAL_SECONDS = 2.0
XVFB_READY_TIMEOUT_SECONDS = 10.0
VNC_READY_TIMEOUT_SECONDS = 10.0

# The broker's site is a heavy Angular app that then bounces through an OAuth
# redirect, so on a small VPS it can be ten to twenty seconds before the login
# form is on screen. Saying which of the two it is beats a silent wait.
LOADING_DETAIL = "در حال باز کردن صفحه‌ی ورود..."
READY_DETAIL = "منتظر ورود دستی..."

# How many consecutive polls must agree before a session is saved. See
# _app_is_visible for why one is not enough.
REQUIRED_CONFIRMATIONS = 2

# How long a live session may go unwatched before it closes itself. The viewer
# polls /manual-login/status every couple of seconds, so silence means nobody
# is looking any more — the page was closed, the phone lost signal, the tab
# was killed. Generous on purpose: a backgrounded phone browser throttles its
# timers to about one poll a minute, and someone stepping away to fetch a
# one-time code out of an SMS must not come back to a dead session.
ABANDONED_AFTER_SECONDS = 180.0

# The app's own host, as opposed to the OAuth host (login.emofid.com) it
# redirects to and back from. Taken from the URL the browser is pointed at,
# so the two cannot drift apart.
APP_HOST = urlparse(mofid_playwright.LOGIN_URL).hostname


class ManualLoginState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    WAITING_FOR_LOGIN = "waiting_for_login"
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"


class ManualLoginSession:
    """One at a time: call start(), have a person finish the login in the
    VNC view, and either the poll loop catches the app appearing or cancel()
    is called. Every path — success, error, or cancel — ends the same way,
    with the browser, Xvfb, and x11vnc all torn down again."""

    def __init__(
        self,
        storage_state_path: str,
        on_success: Callable[[], Awaitable[None]] | None = None,
        display: str = DISPLAY,
        vnc_port: int = VNC_PORT,
    ) -> None:
        self._storage_state_path = Path(storage_state_path)
        self._on_success = on_success
        self._display = display
        self._vnc_port = vnc_port
        self._lock = asyncio.Lock()

        self._state = ManualLoginState.IDLE
        self._detail = ""
        self._xvfb: asyncio.subprocess.Process | None = None
        self._x11vnc: asyncio.subprocess.Process | None = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._poll_task: asyncio.Task | None = None
        self._nav_task: asyncio.Task | None = None
        self._last_seen = 0.0

    def status(self) -> dict:
        return {"state": self._state.value, "detail": self._detail}

    def status_for_viewer(self) -> dict:
        """Same status, but asking counts as being watched. The viewer polls
        this while the page is open, so the last call is how _poll_for_login
        tells a live session from an abandoned one."""
        self._last_seen = time.monotonic()
        return self.status()

    async def start(self) -> dict:
        async with self._lock:
            if self._state in (ManualLoginState.STARTING, ManualLoginState.WAITING_FOR_LOGIN):
                return self.status()  # already running — this is just a status check
            await self._cancel_poll_task()
            await self._cleanup_resources()
            self._state = ManualLoginState.STARTING
            self._detail = ""

        try:
            await self._launch()
        except Exception as exc:
            log.exception("manual login failed to start")
            self._state = ManualLoginState.ERROR
            self._detail = f"راه‌اندازی مرورگر زنده شکست خورد: {exc}"
            await self._cleanup_resources()
            return self.status()

        self._state = ManualLoginState.WAITING_FOR_LOGIN
        self._detail = LOADING_DETAIL
        self._last_seen = time.monotonic()
        self._poll_task = asyncio.create_task(self._poll_for_login())
        return self.status()

    async def cancel(self) -> dict:
        async with self._lock:
            if self._state not in (ManualLoginState.STARTING, ManualLoginState.WAITING_FOR_LOGIN):
                return self.status()
            self._state = ManualLoginState.CANCELLED
            self._detail = "توسط کاربر لغو شد."
            await self._cancel_poll_task()
            await self._cleanup_resources()
            return self.status()

    # ------------------------------------------------------------------
    # launch
    # ------------------------------------------------------------------

    async def _launch(self) -> None:
        await self._start_xvfb()
        await self._start_x11vnc()

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=False,
            env={**os.environ, "DISPLAY": self._display},
            args=[
                # No window manager runs on this display, so the window is
                # placed and sized by these flags alone — and it is sized to
                # the whole screen, since there is no way to reclaim the
                # chrome (see the SCREEN_* notes above).
                "--window-position=0,0",
                f"--window-size={SCREEN_WIDTH},{SCREEN_HEIGHT}",
                # This VPS has no GPU, so every pixel is drawn on the CPU.
                # Pinning the scale factor to 1 keeps that count equal to the
                # pixels actually on screen.
                "--force-device-scale-factor=1",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                # Chromium offered to translate the (Persian) broker site,
                # covering the top of the page with a language bar right as it
                # finished loading. Running its UI in Persian means it no
                # longer sees a foreign page to offer that for. (Not
                # --disable-features=Translate: Playwright already passes a
                # --disable-features list of its own, and a second one would
                # replace it rather than add to it.)
                "--lang=fa-IR",
                "--disable-translate",
            ],
        )
        # Deliberately *not* the full Pixel 5 profile the headless connector
        # uses. That profile pins an emulated 393x851 viewport at a 2.75x
        # scale factor, which Chromium renders at a fixed size no matter how
        # big the window is — the page came out cropped inside the window,
        # and every frame cost ~7.5x the rasterising work on a machine with
        # no GPU to do it. Here a person needs to see and drive the whole
        # page, so it gets the real window as its viewport instead. Only the
        # user agent is carried over, and that is the part that matters: it
        # is what makes the site serve its mobile layout, and what the saved
        # session is issued against.
        device = self._playwright.devices["Pixel 5"]
        self._context = await self._browser.new_context(
            user_agent=device["user_agent"],
            no_viewport=True,
            # Matches --lang above, so the site is asked for Persian — the
            # language the automated flow's selectors are written against.
            locale="fa-IR",
        )
        self._page = await self._context.new_page()
        # Deliberately not awaited. Loading this site takes ten to twenty
        # seconds on this machine, and awaiting it here held up the reply to
        # /manual-login/start — which is what the viewer waits for before it
        # connects, so the whole panel sat frozen on a blank card for the
        # entire load and only then showed a page mid-spin. Handing the caller
        # back a live browser immediately means the person watches it load,
        # which is the same wait but a legible one.
        self._nav_task = asyncio.create_task(self._navigate())

    async def _navigate(self) -> None:
        try:
            await self._page.goto(mofid_playwright.LOGIN_URL)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Whatever went wrong is on screen in the VNC view (a Chromium
            # error page), and the person can retry from there, so this does
            # not fail the session.
            log.warning("manual login: opening %s: %r", mofid_playwright.LOGIN_URL, exc)

    async def _start_xvfb(self) -> None:
        self._remove_stale_xvfb_lock(Path(f"/tmp/.X{self._display.lstrip(':')}-lock"))
        socket_path = Path(f"/tmp/.X11-unix/X{self._display.lstrip(':')}")
        self._xvfb = await asyncio.create_subprocess_exec(
            "Xvfb", self._display, "-screen", "0", SCREEN_SIZE, "-nolisten", "tcp",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        deadline = asyncio.get_event_loop().time() + XVFB_READY_TIMEOUT_SECONDS
        while not socket_path.exists():
            if self._xvfb.returncode is not None:
                raise RuntimeError("Xvfb exited before it was ready")
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError("Xvfb did not become ready in time")
            await asyncio.sleep(0.1)

    @staticmethod
    def _remove_stale_xvfb_lock(lock_path: Path) -> None:
        """A crashed or force-killed Xvfb (this process's own SIGKILL
        fallback in _cleanup_resources included) can leave its lock file
        behind, which makes every future Xvfb on this same display refuse
        to start even though nothing is actually holding it any more. Only
        removed once the pid recorded inside it is confirmed dead — a lock
        held by a real, still-running Xvfb is left alone."""
        if not lock_path.exists():
            return
        try:
            pid = int(lock_path.read_text().strip())
        except (OSError, ValueError):
            with contextlib.suppress(OSError):
                lock_path.unlink()
            return
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            with contextlib.suppress(OSError):
                lock_path.unlink()
        except PermissionError:
            pass  # exists, just not ours to signal — leave it alone

    async def _start_x11vnc(self) -> None:
        # -localhost: only a connection from this same machine is accepted —
        # the real security boundary is the panel's own authenticated
        # WebSocket proxy (see proxy_vnc below), which is the only thing
        # that can reach this port from anywhere else. -nopw is safe given
        # that: a second, VNC-level password would protect nothing a person
        # with shell access to this host couldn't already read directly out
        # of auth_state.json.
        # -noxdamage is deliberately *not* passed: with the DAMAGE extension
        # (which Xvfb does provide) x11vnc is told which rectangles actually
        # changed instead of re-scanning the whole screen every pass, which is
        # most of the difference between a sluggish and a usable feed.
        # No -wait override either. Polling at 100Hz (the 10ms this used to
        # ask for) buys nothing once XDAMAGE is telling x11vnc where to look,
        # and it spends CPU that this machine needs for rendering the page —
        # the thing that was actually slow.
        self._x11vnc = await asyncio.create_subprocess_exec(
            "x11vnc", "-display", self._display, "-localhost", "-nopw",
            "-forever", "-shared", "-quiet",
            "-rfbport", str(self._vnc_port),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        deadline = asyncio.get_event_loop().time() + VNC_READY_TIMEOUT_SECONDS
        while True:
            if self._x11vnc.returncode is not None:
                raise RuntimeError("x11vnc exited before it was ready")
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", self._vnc_port)
            except OSError:
                if asyncio.get_event_loop().time() > deadline:
                    raise RuntimeError("x11vnc did not become ready in time")
                await asyncio.sleep(0.1)
                continue
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return

    # ------------------------------------------------------------------
    # polling for the login to complete
    # ------------------------------------------------------------------

    async def _poll_for_login(self) -> None:
        confirmations = 0
        try:
            while True:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)

                if self._detail == LOADING_DETAIL and self._nav_task is not None:
                    if self._nav_task.done():
                        self._detail = READY_DETAIL

                unwatched = time.monotonic() - self._last_seen
                if unwatched > ABANDONED_AFTER_SECONDS:
                    # Nobody has asked for the status in minutes, so there is
                    # nobody left to finish this login. Chromium, Xvfb and
                    # x11vnc are not cheap on this machine to leave running.
                    self._state = ManualLoginState.CANCELLED
                    self._detail = "چون کسی صفحه را باز نگه نداشت، بسته شد."
                    log.info("manual login: unwatched for %.0fs, closing", unwatched)
                    await self._cleanup_resources()
                    return

                if self._page is None or self._page.is_closed():
                    # The browser itself is gone; nobody can log in now, and
                    # unlike the races below this does not recover.
                    self._state = ManualLoginState.ERROR
                    self._detail = "مرورگر زنده بسته شد."
                    log.warning("manual login: the live browser closed before login finished")
                    await self._cleanup_resources()
                    return

                try:
                    seen = await self._app_is_visible()
                except Exception as exc:
                    # This page navigates a lot — the app boots, bounces to
                    # the OAuth host, comes back — and a question asked at the
                    # wrong moment can raise. Playwright rides most of that
                    # out on its own, but a session someone is part-way
                    # through is far too expensive to end over a transient
                    # read, so the poll skips a beat instead.
                    log.debug("manual login: page busy while checking (%r)", exc)
                    confirmations = 0
                    continue

                # Two in a row: the app shell can flash by mid-boot, and
                # saving a session then would store one that was never
                # logged in.
                confirmations = confirmations + 1 if seen else 0
                if confirmations >= REQUIRED_CONFIRMATIONS:
                    await self._on_login_detected()
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("manual login poll loop crashed")
            self._state = ManualLoginState.ERROR
            self._detail = f"خطا هنگام بررسی وضعیت ورود: {exc}"
            await self._cleanup_resources()

    async def _app_is_visible(self) -> bool:
        """Whether we are looking at the logged-in app.

        Stricter than MofidPlaywrightClient.login()'s check, because this one
        decides whether to save a session. Being *in the DOM* is not enough:
        the app's own shell carries these hooks while it is still starting up
        and on its way to the login page. So the marker has to actually be on
        screen, and the page has to be the app itself rather than the OAuth
        host it hands you off to.
        """
        if self._page is None or self._page.is_closed():
            return False
        if urlparse(self._page.url).hostname != APP_HOST:
            return False
        first, *rest = mofid_playwright.APP_MARKERS
        locator = self._page.locator(first)
        for marker in rest:
            locator = locator.or_(self._page.locator(marker))
        return await locator.first.is_visible()

    async def _on_login_detected(self) -> None:
        # Not _cancel_poll_task() here: this coroutine *is* the poll task: it
        # is already on its way to returning, and a task cancelling and then
        # awaiting itself is a deadlock, not a no-op.
        await self._context.storage_state(path=str(self._storage_state_path))
        self._storage_state_path.chmod(0o600)  # contains session cookies
        log.info("manual login succeeded, saved session to %s", self._storage_state_path)
        self._state = ManualLoginState.SUCCESS
        self._detail = "ورود با موفقیت ثبت شد."
        if self._on_success is not None:
            try:
                await self._on_success()
            except Exception:
                log.exception("manual login on_success callback failed")
        await self._cleanup_resources()

    # ------------------------------------------------------------------
    # teardown
    # ------------------------------------------------------------------

    async def _cancel_poll_task(self) -> None:
        if self._poll_task is None:
            return
        self._poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._poll_task
        self._poll_task = None

    async def _cleanup_resources(self) -> None:
        """Everything except the poll task: safe to call from outside
        (start()'s pre-flight, cancel()) and from inside the poll task's own
        success/error path alike."""
        # First, so a still-running goto() cannot be left holding a page that
        # is about to be closed out from under it.
        if self._nav_task is not None:
            self._nav_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._nav_task
            self._nav_task = None
        if self._context is not None:
            with contextlib.suppress(Exception):
                await self._context.close()
            self._context = None
        self._page = None
        if self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
            self._playwright = None

        for attr in ("_x11vnc", "_xvfb"):
            proc = getattr(self, attr)
            if proc is not None and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    proc.kill()
                    with contextlib.suppress(Exception):
                        await proc.wait()
            setattr(self, attr, None)

    # ------------------------------------------------------------------
    # the VNC view itself
    # ------------------------------------------------------------------

    async def proxy_vnc(self, websocket) -> None:
        """Pumps bytes between an already-accepted, already-authenticated
        WebSocket (noVNC's client) and x11vnc's plain TCP port. This *is*
        this feature's "websockify" — written directly against x11vnc's
        loopback-only port rather than shelling out to another process,
        so the panel's own auth (checked by the caller before this is ever
        invoked) is the one and only gate in front of the VNC stream."""
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", self._vnc_port)
        except OSError as exc:
            log.warning("manual-login vnc proxy: could not reach x11vnc: %r", exc)
            await websocket.close(code=1011)
            return

        async def ws_to_tcp() -> None:
            while True:
                data = await websocket.receive_bytes()
                writer.write(data)
                await writer.drain()

        async def tcp_to_ws() -> None:
            while True:
                data = await reader.read(65536)
                if not data:
                    return
                await websocket.send_bytes(data)

        tasks = [asyncio.create_task(ws_to_tcp()), asyncio.create_task(tcp_to_ws())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            with contextlib.suppress(Exception):
                await websocket.close()
