"""bot.config.Settings.load() reads booleans from .env, where a typo is just
a string nobody validates at the shell. This is what caught DRY_RUN=fasle
silently running live orders through a dry-run that never happened."""
from __future__ import annotations

import logging
import stat
from pathlib import Path

import pytest

from bot.config import Settings, update_env_values


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


# --------------------------------------------------------------------------
# update_env_values — rewriting .env for the panel's "save credentials" option
# --------------------------------------------------------------------------

def test_update_env_values_replaces_an_existing_key_in_place(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("MOFID_USERNAME=old-user\nMOFID_PASSWORD=old-pass\nDRY_RUN=true\n", encoding="utf-8")

    update_env_values(env, {"MOFID_PASSWORD": "new-pass"})

    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines == ["MOFID_USERNAME=old-user", "MOFID_PASSWORD=new-pass", "DRY_RUN=true"]


def test_update_env_values_preserves_comments_and_blank_lines(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\nMOFID_USERNAME=old-user\n\n# another\nMOFID_PASSWORD=old-pass\n",
        encoding="utf-8",
    )

    update_env_values(env, {"MOFID_USERNAME": "new-user"})

    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines == ["# a comment", "MOFID_USERNAME=new-user", "", "# another", "MOFID_PASSWORD=old-pass"]


def test_update_env_values_appends_a_missing_key(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("DRY_RUN=true\n", encoding="utf-8")

    update_env_values(env, {"MOFID_USERNAME": "u", "MOFID_PASSWORD": "p"})

    text = env.read_text(encoding="utf-8")
    assert "DRY_RUN=true" in text
    assert "MOFID_USERNAME=u" in text
    assert "MOFID_PASSWORD=p" in text


def test_update_env_values_creates_the_file_if_it_does_not_exist(tmp_path: Path):
    env = tmp_path / ".env"

    update_env_values(env, {"MOFID_USERNAME": "u"})

    assert env.read_text(encoding="utf-8") == "MOFID_USERNAME=u\n"


def test_update_env_values_leaves_the_file_owner_only(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("MOFID_USERNAME=old\n", encoding="utf-8")

    update_env_values(env, {"MOFID_USERNAME": "new"})

    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_update_env_values_never_logs_the_values_it_writes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    env = tmp_path / ".env"
    env.write_text("MOFID_PASSWORD=old\n", encoding="utf-8")

    with caplog.at_level(logging.DEBUG):
        update_env_values(env, {"MOFID_PASSWORD": "super-secret-value"})

    assert "super-secret-value" not in caplog.text
