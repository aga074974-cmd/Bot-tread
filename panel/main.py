from __future__ import annotations

import asyncio
import logging
import os
import secrets
from datetime import datetime

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from bot.broker.mofid_playwright import MofidPlaywrightClient
from bot.config import Settings, TEHRAN_TZ, parse_tehran_datetime
from bot.logging_setup import setup_logging
from bot.models import Order, OrderStatus, OrderType, Side
from bot.scheduler import run_order
from panel.db import OrderStore
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

STATUS_FA = {
    "pending": "در انتظار",
    "sent": "ارسال شد",
    "failed": "ناموفق",
    "skipped": "لغو / رد شده",
}


def broker_factory() -> MofidPlaywrightClient:
    return MofidPlaywrightClient(
        username=settings.username,
        password=settings.password,
        dry_run=settings.dry_run,
        headless=settings.headless,
    )


async def on_status_change(order: Order) -> None:
    await store.update_status(order)
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


@app.on_event("startup")
async def on_startup() -> None:
    if settings.dry_run:
        log.warning("DRY_RUN is enabled: orders will fill the form but not submit it.")
    for order in await store.pending():
        schedule(order)
    log.info("panel started, %d pending order(s) rescheduled", len(running_tasks))


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
    orders = []
    for r in rows:
        scheduled_at = r["scheduled_at"]
        try:
            local = parse_tehran_datetime(scheduled_at[:16].replace("T", " "))
            scheduled_at_local = f"{to_jalali_str(local)} {local.strftime('%H:%M')}"
        except ValueError:
            scheduled_at_local = scheduled_at
        orders.append(
            {
                "id": r["id"],
                "symbol": r["symbol"],
                "side": r["side"],
                "order_type": r["order_type"],
                "quantity": r["quantity"],
                "scheduled_at_local": scheduled_at_local,
                "status": r["status"],
                "status_fa": STATUS_FA.get(r["status"], r["status"]),
            }
        )

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
    order_type: str = Form(...),
    price: int | None = Form(None),
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
            order_type=OrderType(order_type),
            price=price if order_type == "limit" else None,
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
