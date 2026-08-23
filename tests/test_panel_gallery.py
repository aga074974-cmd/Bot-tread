"""The inline per-order gallery that replaced the separate /screenshots page:
a ▶ trigger next to an order's status, present only when that order actually
left screenshots behind, carrying the run's files as the JSON the page's own
script reads to build the thumbnail grid."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from bot.config import TEHRAN_TZ
from bot.models import Order, OrderStatus, Side

BASE_TIME = datetime(2026, 8, 21, 19, 22, tzinfo=TEHRAN_TZ)


def an_order(symbol: str, *, hours: int = 0) -> Order:
    return Order(symbol=symbol, side=Side.BUY, quantity=10, scheduled_at=BASE_TIME + timedelta(hours=hours))


def make_run(screenshot_dir: Path, order: Order, *, shots: list[str], pages: list[str]) -> Path:
    """A run folder matching bot.screenshots.run_dir's naming: {stamp}_{symbol}_{order.id}."""
    run = screenshot_dir / f"20260821-192200_{order.symbol}_{order.id}"
    run.mkdir(parents=True)
    for name in shots:
        (run / name).write_bytes(b"\x89PNG\r\n\x1a\n fake")
    for name in pages:
        (run / name).write_text("<html>dump</html>", encoding="utf-8")
    return run


def gallery_json(body: str, order_id: str) -> dict | None:
    """Pull the data-gallery payload for one order's row out of the rendered
    page, or None if that row has no gallery button at all."""
    marker = f'onclick="openGallery(this)"'
    for chunk in body.split("<tr>"):
        if order_id not in chunk:
            continue
        if marker not in chunk:
            return None
        start = chunk.index("data-gallery='") + len("data-gallery='")
        end = chunk.index("'", start)
        return json.loads(chunk[start:end])
    raise AssertionError(f"no row found for order {order_id}")


async def test_an_order_with_screenshots_gets_a_gallery_button(
    panel_client: TestClient, panel_store, panel_main, tmp_path: Path, monkeypatch
):
    order = an_order("دارونو")
    await panel_store.insert(order)
    make_run(tmp_path, order, shots=["01_login_page.png", "02_landing.png"], pages=["keypad.html"])
    monkeypatch.setattr(panel_main, "SCREENSHOT_DIR", tmp_path)

    body = panel_client.get("/").text

    data = gallery_json(body, order.id)
    assert data is not None, "expected a ▶ gallery button for this order"
    assert data["shots"] == ["01_login_page.png", "02_landing.png"]
    assert data["pages"] == ["keypad.html"]
    assert data["run"].endswith(f"_{order.id}")


async def test_an_order_without_any_run_has_no_gallery_button(
    panel_client: TestClient, panel_store, panel_main, tmp_path: Path, monkeypatch
):
    """Never scheduled yet, or scheduled but not due — either way, nothing to
    show, so the button (▶) does not render at all."""
    order = an_order("دارونو")
    await panel_store.insert(order)
    monkeypatch.setattr(panel_main, "SCREENSHOT_DIR", tmp_path)  # empty directory

    body = panel_client.get("/").text

    assert gallery_json(body, order.id) is None


async def test_an_unrelated_orders_run_does_not_leak_into_this_ones_gallery(
    panel_client: TestClient, panel_store, panel_main, tmp_path: Path, monkeypatch
):
    """Matching is by the order id suffix, not by symbol: two orders for the
    same symbol must not be able to see each other's screenshots."""
    order_a = an_order("دارونو", hours=1)
    order_b = an_order("دارونو", hours=2)
    await panel_store.insert(order_a)
    await panel_store.insert(order_b)
    make_run(tmp_path, order_a, shots=["01_a.png"], pages=[])
    monkeypatch.setattr(panel_main, "SCREENSHOT_DIR", tmp_path)

    body = panel_client.get("/").text

    assert gallery_json(body, order_a.id) is not None
    assert gallery_json(body, order_b.id) is None


async def test_the_gallery_appears_in_history_too(
    panel_client: TestClient, panel_store, panel_main, tmp_path: Path, monkeypatch
):
    order = an_order("دارونو")
    await panel_store.insert(order)
    order.status = OrderStatus.SENT
    await panel_store.update_status(order, error=None)
    make_run(tmp_path, order, shots=["01_a.png"], pages=[])
    monkeypatch.setattr(panel_main, "SCREENSHOT_DIR", tmp_path)

    body = panel_client.get("/history").text

    assert gallery_json(body, order.id) is not None


async def test_the_gallery_button_and_script_are_present_when_needed(
    panel_client: TestClient, panel_store, panel_main, tmp_path: Path, monkeypatch
):
    """The shared modal markup and its script load once per page, whenever at
    least one order has a gallery to show."""
    order = an_order("دارونو")
    await panel_store.insert(order)
    make_run(tmp_path, order, shots=["01_a.png"], pages=[])
    monkeypatch.setattr(panel_main, "SCREENSHOT_DIR", tmp_path)

    body = panel_client.get("/").text

    assert 'id="gallery-modal"' in body
    assert "function openGallery" in body
    assert "function showGalleryImage" in body


async def test_filenames_in_the_gallery_are_pinned_left_to_right(panel_client: TestClient, panel_store):
    """Latin, digit-led filenames ("01_login_page.png") read backwards
    ("login_page.png_01") inside this RTL page unless pinned ltr — the same
    bug the old screenshots.html page had to fix once already. Checked at the
    source level: there is no browser here to actually render the bidi text."""
    await panel_store.insert(an_order("دارونو"))  # the modal's script only renders with an order on the page

    body = panel_client.get("/").text

    assert "img.dir = 'ltr'" in body
    assert "title.setAttribute('dir', 'ltr')" in body


async def test_the_grid_and_full_panels_actually_disappear_when_hidden(panel_client: TestClient, panel_store):
    """Both panels set their own explicit display (grid/flex), which — at
    equal specificity — otherwise beats the browser's default
    [hidden]{display:none} and leaves the "hidden" panel fully laid out and
    painted regardless of the JS toggling its hidden attribute."""
    await panel_store.insert(an_order("دارونو"))

    body = panel_client.get("/").text

    assert ".gallery-grid[hidden]" in body
    assert ".gallery-full[hidden]" in body
