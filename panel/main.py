from __future__ import annotations

import asyncio
import logging
import os
import secrets
from datetime import datetime
from pathlib import Path

from dotenv import find_dotenv
from fastapi import FastAPI, Form, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from bot.broker.base import BrokerError
from bot.broker.mofid_playwright import MofidPlaywrightClient
from bot.config import Settings, TEHRAN_TZ, parse_tehran_datetime, update_env_values
from bot.logging_setup import setup_logging
from bot.models import Order, OrderStatus, OrderType, Side
from bot.scheduler import run_order
from bot.screenshots import BASE_DIR as SCREENSHOT_DIR, purge_old, run_dir
from panel.db import OrderStore
from panel.errors import to_persian
from panel.jalali import PERSIAN_MONTHS, current_jalali_year, jalali_to_gregorian_str, to_jalali_str, today_jalali_ymd
from panel.manual_login import SCREEN_HEIGHT, SCREEN_WIDTH, ManualLoginSession
from panel.session_state import SessionStateStore

log = logging.getLogger(__name__)

setup_logging()
settings = Settings.load()

PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_hex(32)
DB_PATH = os.getenv("PANEL_DB_PATH", "panel.db")
# The same file Settings.load()'s load_dotenv() actually read, so a manual
# login that asks to be "saved" updates the .env the running process is
# using — not just some ".env" in whatever the current directory happens to
# be at request time.
ENV_PATH = find_dotenv() or ".env"

# "2 or 3 attempts in a row" per the panel's own spec for this guard; 3 gives
# the automatic retry a little more room before it bothers a person.
MANUAL_LOGIN_THRESHOLD = 3

# Where `apt install novnc` puts the noVNC static web client — vnc_lite.html
# and the JS/CSS it imports — that /manual-login/novnc/ serves, behind the
# panel's own login, for the live-browser manual login page.
NOVNC_DIR = Path(os.getenv("NOVNC_DIR", "/usr/share/novnc")).resolve()

if not PANEL_PASSWORD:
    raise RuntimeError("PANEL_PASSWORD must be set in .env before starting the panel")

store = OrderStore(DB_PATH)
session_store = SessionStateStore(DB_PATH)
templates = Jinja2Templates(directory="panel/templates")
running_tasks: dict[str, asyncio.Task] = {}


async def _on_manual_login_success() -> None:
    await session_store.record_login_success()


manual_login_session = ManualLoginSession(
    storage_state_path=settings.storage_state_path,
    on_success=_on_manual_login_success,
)

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax")

# The dashboard is read on a phone: it shows the newest few orders and sends
# the rest to /history, so the form stays reachable without scrolling.
DASHBOARD_ORDERS = 3

STATUS_FA = {
    "pending": "در انتظار",
    "sent": "ارسال شد",
    "failed": "ناموفق",
    # Cancelling deletes the order outright now, so the only thing that still
    # ends up here is the scheduler passing over one that was already too late
    # to be the order that was asked for — never a cancellation.
    "skipped": "اجرا نشد",
}


async def _report_login_result(success: bool, detail: str) -> None:
    """Feeds every automatic login attempt's outcome into the shared session
    state — whether it came from an order actually being placed or from the
    status light's own re-login — so either path can trip the manual-login
    fallback below."""
    if success:
        await session_store.record_login_success()
    else:
        await session_store.record_login_failure()
        log.warning("automatic login failed: %s", detail)


def broker_factory(order: Order) -> MofidPlaywrightClient:
    return MofidPlaywrightClient(
        username=settings.username,
        password=settings.password,
        dry_run=settings.dry_run,
        headless=settings.headless,
        storage_state_path=settings.storage_state_path,
        screenshot_dir=run_dir(SCREENSHOT_DIR, f"{order.symbol}_{order.id}"),
        on_login_result=_report_login_result,
    )


async def on_status_change(order: Order) -> None:
    await store.update_status(order, error=order.error)
    running_tasks.pop(order.id, None)


def schedule(order: Order) -> None:
    task = asyncio.create_task(
        run_order(
            order,
            broker_factory,
            grace_period_seconds=settings.grace_period_seconds,
            max_retries=settings.max_retries,
            retry_delay_seconds=settings.retry_delay_seconds,
            retry_deadline_seconds=settings.retry_deadline_seconds,
            on_status_change=on_status_change,
        )
    )
    running_tasks[order.id] = task


async def _purge_screenshots_periodically() -> None:
    """Drop screenshot folders older than the retention window, then keep
    checking twice a day so a long-running panel doesn't accumulate them."""
    while True:
        await asyncio.to_thread(purge_old, SCREENSHOT_DIR, settings.screenshot_retention_days)
        await asyncio.sleep(12 * 3600)


async def _purge_order_history_periodically() -> None:
    """Same idea, for panel.db: orders older than the retention window are
    dropped so the table (and /history) don't grow forever."""
    while True:
        removed = await store.purge_old(settings.order_history_retention_days)
        if removed:
            log.info(
                "removed %d order(s) older than %d days",
                removed, settings.order_history_retention_days,
            )
        await asyncio.sleep(12 * 3600)


@app.on_event("startup")
async def on_startup() -> None:
    if settings.dry_run:
        log.warning("DRY_RUN is enabled: orders will fill the form but not submit it.")
    for order in await store.pending():
        schedule(order)
    asyncio.create_task(_purge_screenshots_periodically())
    asyncio.create_task(_purge_order_history_periodically())
    log.info("panel started, %d pending order(s) rescheduled", len(running_tasks))


def _order_rows(rows) -> list[dict]:
    """One dict per order, ready to render: a Jalali date and a time on their
    own lines, the error as the Persian sentence the panel shows, and — when
    that order ran far enough to leave one — its screenshot gallery."""
    runs_by_order_id = {run["name"].rsplit("_", 1)[-1]: run for run in _screenshot_runs()}

    orders = []
    for r in rows:
        scheduled_at = r["scheduled_at"]
        try:
            local = parse_tehran_datetime(scheduled_at[:16].replace("T", " "))
            date_fa, time_fa = to_jalali_str(local), local.strftime("%H:%M")
        except ValueError:
            date_fa, time_fa = scheduled_at, ""
        run = runs_by_order_id.get(r["id"])
        orders.append(
            {
                "id": r["id"],
                "symbol": r["symbol"],
                "side": r["side"],
                "quantity": r["quantity"],
                "date_fa": date_fa,
                "time_fa": time_fa,
                "status": r["status"],
                "status_fa": STATUS_FA.get(r["status"], r["status"]),
                "error_fa": to_persian(r["error"]),
                "gallery": {"run": run["name"], "shots": run["shots"], "pages": run["pages"]} if run else None,
            }
        )
    return orders


def _completed(rows) -> list:
    """Orders that have already happened, one way or another. A pending order
    has not happened yet: it belongs on the dashboard, where it can still be
    cancelled, and nowhere else."""
    return [r for r in rows if r["status"] != "pending"]


def _dashboard_split(rows) -> tuple[list, int]:
    """Pending orders stay on the dashboard no matter how many there are or
    how old they are — cancelling one is only possible from there. Completed
    orders (sent/failed/skipped) are capped at DASHBOARD_ORDERS; the rest are
    what /history is for. The combined list is re-sorted so it still reads as
    one chronological table rather than two stacked groups."""
    pending = [r for r in rows if r["status"] == "pending"]
    completed = _completed(rows)
    shown = sorted(pending + completed[:DASHBOARD_ORDERS], key=lambda r: r["scheduled_at"], reverse=True)
    return shown, max(0, len(completed) - DASHBOARD_ORDERS)


def require_auth(request: Request) -> bool:
    return bool(request.session.get("authenticated"))


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    if secrets.compare_digest(password, PANEL_PASSWORD):
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": "رمز عبور اشتباه است"})


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)

    rows = await store.list_all()
    shown_rows, hidden_count = _dashboard_split(rows)
    orders = _order_rows(shown_rows)
    session_status = await session_store.get()

    flash = request.session.pop("flash", None)
    flash_error = request.session.pop("flash_error", False)
    current_year = current_jalali_year()
    today_year, today_month, today_day = today_jalali_ymd()
    now_time = datetime.now(TEHRAN_TZ).strftime("%H:%M")
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "orders": orders,
            "hidden_count": hidden_count,
            "flash": flash,
            "flash_error": flash_error,
            "months": list(enumerate(PERSIAN_MONTHS, start=1)),
            "years": [current_year, current_year + 1],
            "today_year": today_year,
            "today_month": today_month,
            "today_day": today_day,
            "now_time": now_time,
            "session_valid": session_status["valid"],
            "manual_login_required": session_status["consecutive_failures"] >= MANUAL_LOGIN_THRESHOLD,
        },
    )


def _session_check_client() -> MofidPlaywrightClient:
    """A client for a plain status check — always headless regardless of
    HEADLESS in .env (that setting is for watching an order run locally, not
    for a background status check on a server with no display), and never
    wired to the failure counter: check_session() itself never attempts a
    login, so there is nothing here for that counter to count."""
    return MofidPlaywrightClient(
        username=settings.username,
        password=settings.password,
        dry_run=True,
        headless=True,
        storage_state_path=settings.storage_state_path,
    )


@app.post("/session/check")
async def session_check(request: Request):
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    client = _session_check_client()
    try:
        valid = await client.check_session()
    finally:
        await client.close()

    if valid:
        await session_store.set_valid(True)
        return JSONResponse({"valid": True, "message": "نشست فعال است", "manual_login_required": False})

    # Invalid — automatically try to sign back in with the saved credentials
    # before bothering anyone. Both outcomes go through the same reporting
    # path as an order's own login (_report_login_result), so this attempt
    # counts toward the same streak.
    relogin_client = MofidPlaywrightClient(
        username=settings.username,
        password=settings.password,
        dry_run=True,
        headless=True,
        storage_state_path=settings.storage_state_path,
        on_login_result=_report_login_result,
    )
    try:
        await relogin_client.login()
    except BrokerError as exc:
        status = await session_store.get()
        return JSONResponse({
            "valid": False,
            "message": f"ورود ناموفق: {to_persian(str(exc))}",
            "manual_login_required": status["consecutive_failures"] >= MANUAL_LOGIN_THRESHOLD,
        })
    else:
        status = await session_store.get()
        return JSONResponse({
            "valid": True,
            "message": "ورود مجدد موفق بود",
            "manual_login_required": status["consecutive_failures"] >= MANUAL_LOGIN_THRESHOLD,
        })
    finally:
        await relogin_client.close()


@app.post("/session/manual-login")
async def session_manual_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    save: str | None = Form(None),
):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)

    client = MofidPlaywrightClient(
        username=username,
        password=password,
        dry_run=True,
        headless=True,
        storage_state_path=settings.storage_state_path,
        on_login_result=_report_login_result,
    )
    try:
        await client.login()
    except BrokerError as exc:
        request.session["flash"] = f"ورود دستی ناموفق: {to_persian(str(exc))}"
        request.session["flash_error"] = True
        return RedirectResponse("/", status_code=303)
    finally:
        await client.close()

    # The typed credentials are only ever used for this one attempt unless
    # asked to be kept: only then do they replace what future automatic
    # logins (a session run, or the next click on the light) will use.
    if save == "1":
        settings.username = username
        settings.password = password
        await asyncio.to_thread(
            update_env_values, ENV_PATH, {"MOFID_USERNAME": username, "MOFID_PASSWORD": password}
        )
        request.session["flash"] = "ورود دستی موفق بود و برای دفعات بعد ذخیره شد."
    else:
        request.session["flash"] = "ورود دستی موفق بود."
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------
# manual login via a live, remotely-viewable browser (panel/manual_login.py)
# --------------------------------------------------------------------------

@app.get("/manual-login", response_class=HTMLResponse)
async def manual_login_page(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    # The viewer is shaped from the real virtual screen, so the remote
    # browser lands in the frame at its own proportions — a hardcoded guess
    # here would letterbox or squash it the moment that screen changes.
    return templates.TemplateResponse(
        request,
        "manual_login.html",
        {
            "screen_width": SCREEN_WIDTH,
            "screen_height": SCREEN_HEIGHT,
        },
    )


@app.post("/manual-login/start")
async def manual_login_start(request: Request):
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(await manual_login_session.start())


@app.get("/manual-login/status")
async def manual_login_status(request: Request):
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(manual_login_session.status_for_viewer())


@app.post("/manual-login/cancel")
async def manual_login_cancel(request: Request):
    if not require_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(await manual_login_session.cancel())


# noVNC's own toolbar — the pull-out tab and the column of buttons behind it —
# is dead weight on this page: the panel drives the one control that matters
# (the keyboard) from its own button, and on a phone the bar covers a third of
# the remote screen. The rule goes in as the page is served rather than being
# injected once it has loaded, so the bar never flashes up first. It does not
# take the phone keyboard with it: the hidden textarea that raises it lives in
# #noVNC_container, not in the bar. Both ids have been stable since noVNC 1.3.
NOVNC_HIDE_TOOLBAR = (
    "<style>#noVNC_control_bar_anchor, #noVNC_hint_anchor "
    "{ display: none !important; }</style>"
)


def _without_novnc_toolbar(markup: str) -> str:
    if "</head>" in markup:
        return markup.replace("</head>", NOVNC_HIDE_TOOLBAR + "</head>", 1)
    # No head to put it in — a stylesheet is honoured anywhere in the document,
    # so append rather than silently serve the page with its toolbar.
    return markup + NOVNC_HIDE_TOOLBAR


def _resolve_novnc_file(path: str) -> Path | None:
    """Same defensive shape as _is_known_file below: resolve the requested
    path and refuse anything that escapes NOVNC_DIR (a `../../etc/passwd`
    style request), rather than trusting the path as given."""
    candidate = (NOVNC_DIR / path).resolve()
    if candidate != NOVNC_DIR and NOVNC_DIR not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


@app.get("/manual-login/novnc/{path:path}")
async def manual_login_novnc_asset(request: Request, path: str):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    resolved = _resolve_novnc_file(path)
    if resolved is None:
        return HTMLResponse("یافت نشد", status_code=404)
    if resolved.name == "vnc.html":
        return HTMLResponse(_without_novnc_toolbar(resolved.read_text(encoding="utf-8")))
    return FileResponse(resolved)


@app.websocket("/manual-login/vnc")
async def manual_login_vnc(websocket: WebSocket):
    # Checked before accept(): closing here (rather than accepting and then
    # closing) makes an unauthenticated attempt fail the handshake itself —
    # this is the one and only thing standing between the open internet and
    # a live, logged-in-once-it-succeeds broker session.
    if not websocket.session.get("authenticated"):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    await manual_login_session.proxy_vnc(websocket)


@app.post("/orders")
async def create_order(
    request: Request,
    symbol: str = Form(...),
    side: str = Form(...),
    quantity: int = Form(...),
    jalali_year: int = Form(...),
    jalali_month: int = Form(...),
    jalali_day: int = Form(...),
    time: str = Form(...),
):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)

    try:
        date = jalali_to_gregorian_str(jalali_year, jalali_month, jalali_day)
        scheduled_at = parse_tehran_datetime(f"{date} {time}")
        order = Order(
            symbol=symbol.strip(),
            side=Side(side),
            quantity=quantity,
            order_type=OrderType.MARKET,
            scheduled_at=scheduled_at,
        )
    except (ValueError, KeyError) as exc:
        request.session["flash"] = f"ورودی نامعتبر: {exc}"
        request.session["flash_error"] = True
        return RedirectResponse("/", status_code=303)

    await store.insert(order)
    schedule(order)
    request.session["flash"] = f"سفارش {order.symbol} برای {to_jalali_str(scheduled_at)} {scheduled_at.strftime('%H:%M')} ثبت شد."
    return RedirectResponse("/", status_code=303)


@app.post("/orders/{order_id}/cancel")
async def cancel_order(request: Request, order_id: str):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)

    task = running_tasks.pop(order_id, None)
    if task:
        task.cancel()
    changed = await store.cancel(order_id)
    request.session["flash"] = "سفارش لغو شد." if changed else "این سفارش دیگر قابل لغو نیست."
    request.session["flash_error"] = not changed
    return RedirectResponse("/", status_code=303)


@app.get("/history", response_class=HTMLResponse)
async def history(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    # Completed only. A pending order showing up here read as a second copy of
    # what the dashboard is already showing, and the count on the dashboard's
    # own link to this page never counted them either.
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "orders": _order_rows(_completed(await store.list_all())),
            "empty_text": "هنوز سفارشی انجام نشده — سفارش‌های در انتظار در صفحه‌ی اصلی هستند.",
        },
    )


def _screenshot_runs() -> list[dict]:
    """One entry per order run (newest first), with its shots and — when the
    run failed — the page markup the connector dumped alongside them. Backs
    both each order's inline gallery and the path check below."""
    if not SCREENSHOT_DIR.is_dir():
        return []
    runs = []
    for folder in sorted(SCREENSHOT_DIR.iterdir(), reverse=True):
        if not folder.is_dir():
            continue
        shots = sorted(p.name for p in folder.glob("*.png"))
        pages = sorted(p.name for p in folder.glob("*.html"))
        if shots or pages:
            runs.append({"name": folder.name, "shots": shots, "pages": pages})
    return runs


def _is_known_file(run: str, name: str) -> bool:
    return any(
        r["name"] == run and (name in r["shots"] or name in r["pages"])
        for r in _screenshot_runs()
    )


@app.get("/screenshots/{run}/{name}")
async def screenshot_file(request: Request, run: str, name: str, download: int = 0):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    # Only serve run/name pairs that actually appear in the directory listing,
    # so a crafted path can never escape SCREENSHOT_DIR.
    if not _is_known_file(run, name):
        return RedirectResponse("/", status_code=303)

    path = SCREENSHOT_DIR / run / name
    # ?download=1 asks for the file itself rather than a view of it. The run is
    # folded into the name because every run calls its shots the same thing,
    # and a phone's downloads folder is one flat pile.
    filename = f"{run}_{name}" if download else None
    if path.suffix == ".png":
        return FileResponse(path, media_type="image/png", filename=filename)

    # A page dump is markup captured from the broker's site. Serve it as plain
    # text, never as text/html: rendering it here would run someone else's
    # scripts on the panel's own origin (and its asset links point at the
    # broker anyway). The source is what you need to fix a selector.
    return FileResponse(
        path,
        media_type="text/plain; charset=utf-8",
        filename=filename,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )
