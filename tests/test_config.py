"""bot.config.Settings.load() reads booleans from .env, where a typo is just
a string nobody validates at the shell. This is what caught DRY_RUN=fasle
silently running live orders through a dry-run that never happened."""
from __future__ import annotations

import logging

import pytest

from bot.config import Settings


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings.load() calls load_dotenv(), which would otherwise pull in
    this repo's own .env (if one exists) and leak its values into the test."""
    for name in (
        "DRY_RUN", "HEADLESS", "MOFID_USERNAME", "MOFID_PASSWORD",
        "GRACE_PERIOD_SECONDS", "MAX_RETRIES", "RETRY_DELAY_SECONDS",
        "RETRY_DEADLINE_SECONDS", "STORAGE_STATE_PATH", "SCREENSHOT_RETENTION_DAYS",
        "ORDER_HISTORY_RETENTION_DAYS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("bot.config.load_dotenv", lambda *a, **k: None)


def test_dry_run_defaults_true_when_unset():
    assert Settings.load().dry_run is True


@pytest.mark.parametrize("value", ["false", "False", "FALSE", "0", "no", " false "])
def test_recognised_false_spellings_disable_dry_run(monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv("DRY_RUN", value)
    assert Settings.load().dry_run is False


@pytest.mark.parametrize("value", ["true", "1", "yes"])
def test_recognised_true_spellings_keep_dry_run_on(monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv("DRY_RUN", value)
    assert Settings.load().dry_run is True


def test_a_typo_reads_as_true_but_is_logged(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    """The exact incident this guards against: DRY_RUN=fasle silently kept
    every order in dry-run, on a server the person running it believed was
    live. It cannot fail loudly (there is no schema to reject it against) —
    but it must not fail silently either."""
    monkeypatch.setenv("DRY_RUN", "fasle")

    with caplog.at_level(logging.WARNING, logger="bot.config"):
        settings = Settings.load()

    assert settings.dry_run is True
    assert "DRY_RUN" in caplog.text
    assert "fasle" in caplog.text


def test_an_unrecognised_headless_value_is_also_logged(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    monkeypatch.setenv("HEADLESS", "nope")

    with caplog.at_level(logging.WARNING, logger="bot.config"):
        settings = Settings.load()

    assert settings.headless is True
    assert "HEADLESS" in caplog.text


def test_a_recognised_value_logs_nothing(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    """The warning is for typos, not for every startup — a correctly spelled
    value must stay quiet."""
    monkeypatch.setenv("DRY_RUN", "false")

    with caplog.at_level(logging.WARNING, logger="bot.config"):
        Settings.load()

    assert caplog.text == ""
