from __future__ import annotations

import asyncio
import logging
import os
import secrets
from datetime import datetime

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from bot.broker.mofid_playwright import MofidPlaywrightClient
from bot.config import Settings, TEHRAN_TZ, parse_tehran_datetime
from bot.logging_setup import setup_logging
from bot.models import Order, OrderStatus, OrderType, Side
from bot.scheduler import run_order
from bot.screenshots import BASE_DIR as SCREENSHOT_DIR, purge_old, run_dir
from panel.db import OrderStore
from panel.errors import to_persian
from panel.jalali import PERSIAN_MONTHS, current_jalali_year, jalali_to_gregorian_str, to_jalali_str, today_jalali_ymd

log = logging.getLogger(__name__)

setup_logging()
settings = Settings.load()

PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_hex(32)
DB_PATH = os.getenv("PANEL_DB_PATH", "panel.db")

if not PANEL_PASSWORD:
    raise RuntimeError("PANEL_PASSWORD must be set in .env before starting the panel")

store = OrderStore(DB_PATH)
templates = Jinja2Templates(directory="panel/templates")
running_tasks: dict[str, asyncio.Task] = {}

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax")

# The dashboard is read on a phone: it shows the newest few orders and sends
# the rest to /history, so the form stays reachable without scrolling.
DASHBOARD_ORDERS = 3

STATUS_FA = {
    "pending": "در انتظار",
    "sent": "ارسال شد",
    "failed": "ناموفق",
    "skipped": "لغو / رد شده",
}


def broker_factory(order: Order) -> MofidPlaywrightClient:
    return MofidPlaywrightClient(
        username=settings.username,
        password=settings.password,
        dry_run=settings.dry_run,
        headless=settings.headless,
        storage_state_path=settings.storage_state_path,
        screenshot_dir=run_dir(SCREENSHOT_DIR, f"{order.symbol}_{order.id}"),
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


@app.on_event("startup")
async def on_startup() -> None:
    if settings.dry_run:
        log.warning("DRY_RUN is enabled: orders will fill the form but not submit it.")
    for order in await store.pending():
        schedule(order)
    asyncio.create_task(_purge_screenshots_periodically())
    log.info("panel started, %d pending order(s) rescheduled", len(running_tasks))


def _order_rows(rows) -> list[dict]:
    """One dict per order, ready to render: a Jalali date and a time on their
    own lines, and the error as the Persian sentence the panel shows."""
    orders = []
    for r in rows:
        scheduled_at = r["scheduled_at"]
        try:
            local = parse_tehran_datetime(scheduled_at[:16].replace("T", " "))
            date_fa, time_fa = to_jalali_str(local), local.strftime("%H:%M")
        except ValueError:
            date_fa, time_fa = scheduled_at, ""
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
            }
        )
    return orders


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

    orders = _order_rows(await store.list_all())

    flash = request.session.pop("flash", None)
    flash_error = request.session.pop("flash_error", False)
    current_year = current_jalali_year()
    today_year, today_month, today_day = today_jalali_ymd()
    now_time = datetime.now(TEHRAN_TZ).strftime("%H:%M")
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "orders": orders[:DASHBOARD_ORDERS],
            "hidden_count": max(0, len(orders) - DASHBOARD_ORDERS),
            "flash": flash,
            "flash_error": flash_error,
            "months": list(enumerate(PERSIAN_MONTHS, start=1)),
            "years": [current_year, current_year + 1],
            "today_year": today_year,
            "today_month": today_month,
            "today_day": today_day,
            "now_time": now_time,
        },
    )


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
    return templates.TemplateResponse(
        request, "history.html", {"orders": _order_rows(await store.list_all())}
    )


def _screenshot_runs() -> list[dict]:
    """One entry per order run (newest first), with its shots and — when the
    run failed — the page markup the connector dumped alongside them."""
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


@app.get("/screenshots", response_class=HTMLResponse)
async def screenshots(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "screenshots.html",
        {"runs": _screenshot_runs(), "retention_days": settings.screenshot_retention_days},
    )


@app.get("/screenshots/{run}/{name}")
async def screenshot_file(request: Request, run: str, name: str, download: int = 0):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    # Only serve run/name pairs that actually appear in the directory listing,
    # so a crafted path can never escape SCREENSHOT_DIR.
    if not _is_known_file(run, name):
        return RedirectResponse("/screenshots", status_code=303)

    path = SCREENSHOT_DIR / run / name
    if path.suffix == ".png":
        return FileResponse(path, media_type="image/png")

    # A page dump is markup captured from the broker's site. Serve it as plain
    # text, never as text/html: rendering it here would run someone else's
    # scripts on the panel's own origin (and its asset links point at the
    # broker anyway). The source is what you need to fix a selector.
    return FileResponse(
        path,
        media_type="text/plain; charset=utf-8",
        filename=f"{run}_{name}" if download else None,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )
