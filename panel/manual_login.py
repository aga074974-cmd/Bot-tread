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
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable

from playwright.async_api import async_playwright

from bot.broker import mofid_playwright

log = logging.getLogger(__name__)

# A fixed display/port: only one admin drives this panel at a time, so there
# is no need to hand out a fresh pair per session — and a fixed pair means a
# crashed-and-restarted panel can find (and clean up) a stale one by name.
DISPLAY = ":77"
VNC_PORT = 5977
# Roughly a phone's aspect ratio. 16-bit colour (not 24) roughly halves what
# x11vnc has to push over the websocket on every change, which matters more
# than colour depth ever will for a login form on a mobile connection.
SCREEN_WIDTH = 480
SCREEN_HEIGHT = 920
SCREEN_DEPTH = 16
SCREEN_SIZE = f"{SCREEN_WIDTH}x{SCREEN_HEIGHT}x{SCREEN_DEPTH}"

POLL_INTERVAL_SECONDS = 2.0
XVFB_READY_TIMEOUT_SECONDS = 10.0
VNC_READY_TIMEOUT_SECONDS = 10.0


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

    def status(self) -> dict:
        return {"state": self._state.value, "detail": self._detail}

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
        self._detail = "منتظر ورود دستی..."
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
            # There is no window manager on this Xvfb display, so a browser
            # window opened at Chromium's own default size (much wider than
            # our 480px-wide screen) simply gets clipped at the screen edge —
            # that's the "the page doesn't fully show up" bug: most of the
            # page was rendering off-screen, not missing. Pinning the window
            # to exactly the Xvfb screen's size and 0,0 position fixes that.
            args=[
                "--window-position=0,0",
                f"--window-size={SCREEN_WIDTH},{SCREEN_HEIGHT}",
                "--disable-gpu",
                "--disable-dev-shm-usage",
            ],
        )
        # The exact same device profile the headless connector uses: a
        # mismatch here means the site serves its desktop layout instead,
        # which not only looks wrong on a phone's VNC view but would save a
        # storage_state the mobile-only automated flow can't reuse.
        device = self._playwright.devices["Pixel 5"]
        self._context = await self._browser.new_context(**device)
        self._page = await self._context.new_page()
        await self._page.goto(mofid_playwright.LOGIN_URL)

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
        self._x11vnc = await asyncio.create_subprocess_exec(
            "x11vnc", "-display", self._display, "-localhost", "-nopw",
            "-forever", "-shared", "-noxdamage", "-quiet",
            "-wait", "10",  # poll every 10ms instead of the 20ms default
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
        try:
            while True:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                if await self._app_is_visible():
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
        """Same signal MofidPlaywrightClient._app_markers() waits for in
        login() — any one of the app's own bottom-nav hooks on screen."""
        if self._page is None or self._page.is_closed():
            return False
        first, *rest = mofid_playwright.APP_MARKERS
        locator = self._page.locator(first)
        for marker in rest:
            locator = locator.or_(self._page.locator(marker))
        return await locator.first.count() > 0

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
