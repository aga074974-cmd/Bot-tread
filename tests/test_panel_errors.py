"""The panel is read on a phone, in Persian. These pin what it says for each
error the connector can raise."""
from __future__ import annotations

from pathlib import Path

import pytest

from bot.scheduler import RETRY_DEADLINE_MARK
from panel.errors import DEADLINE_FA, FALLBACK, _MESSAGES, to_persian

CONNECTOR = Path(__file__).parents[1] / "bot" / "broker" / "mofid_playwright.py"

# Some fragments only exist once an f-string has been filled in, and one is
# Playwright's rather than ours: this says what to look for in the source.
SOURCE_FRAGMENT: dict[str, str | None] = {
    "could not enter the username": "could not enter the {what}",
    "could not enter the password": "could not enter the {what}",
    "box would not clear": "box would not clear",
    "box reads": "box reads",
    "Timeout": None,  # raised by Playwright, not written by us
}


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_an_order_without_an_error_says_nothing(raw):
    assert to_persian(raw) == ""


def test_the_brokers_own_words_win():
    """It is already Persian, and nothing we write beats the broker naming the
    problem itself."""
    raw = (
        "login did not reach the app — the site says: نام کاربری یا کلمه عبور اشتباه است. "
        "Still at: https://m.easytrader.ir/ — see the login_failed screenshot"
    )

    assert to_persian(raw) == "نام کاربری یا کلمه عبور اشتباه است"


def test_a_login_failure_without_the_brokers_words_still_reads_in_persian():
    raw = (
        "login did not reach the app — wrong credentials, or an extra step "
        "(OTP/agreement) is in the way. Still at: https://m.easytrader.ir/"
    )

    message = to_persian(raw)

    assert message.startswith("ورود انجام نشد")
    assert "wrong credentials" not in message


def test_a_sent_order_with_an_unknown_reply_warns_to_check_the_portfolio():
    """This one is not a plain failure: the order may be live."""
    raw = "order was sent, but no message matching 'با موفقیت' appeared — it may well have gone through."

    assert "پرتفوی" in to_persian(raw)


def test_an_unrecognised_error_still_answers_in_persian():
    assert to_persian("KeyError: 'symbol'") == FALLBACK


def test_an_order_abandoned_on_the_deadline_says_so_and_why():
    """The scheduler stopping early and the thing that broke are two separate
    facts; the panel shows both."""
    raw = f"login did not reach the app — wrong credentials — {RETRY_DEADLINE_MARK}"

    message = to_persian(raw)

    assert message.startswith(DEADLINE_FA)
    assert "ورود انجام نشد" in message


def test_the_deadline_note_also_fronts_the_brokers_own_words():
    raw = (
        "login did not reach the app — the site says: نام کاربری یا کلمه عبور اشتباه است. "
        f"Still at: https://m.easytrader.ir/ — {RETRY_DEADLINE_MARK}"
    )

    assert to_persian(raw) == f"{DEADLINE_FA} نام کاربری یا کلمه عبور اشتباه است"


@pytest.mark.parametrize("fragment,message", _MESSAGES)
def test_every_mapping_is_reachable(fragment: str, message: str):
    assert to_persian(f"BrokerError: {fragment} ...") == message


@pytest.mark.parametrize(
    "fragment", [f for f, _ in _MESSAGES if SOURCE_FRAGMENT.get(f, f) is not None]
)
def test_mapped_fragments_still_exist_in_the_connector(fragment: str):
    """Guards the seam between the two: if an error message is reworded over
    there and not here, the panel quietly falls back to the generic sentence."""
    expected = SOURCE_FRAGMENT.get(fragment, fragment)

    assert expected in CONNECTOR.read_text(encoding="utf-8"), (
        f"panel/errors.py maps {fragment!r}, and {expected!r} no longer appears "
        f"in {CONNECTOR.name} — update the mapping or the panel will show the "
        "generic message for it"
    )
