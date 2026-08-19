from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)

BASE_DIR = Path("logs/screenshots")
_UNSAFE = re.compile(r"[^\w؀-ۿ-]+")


def run_dir(base: Path | str, label: str) -> Path:
    """Directory for one order's screenshots, e.g.
    logs/screenshots/20260819-194942_دارونو_88679daa — so each run's shots
    stay together instead of overwriting the previous run's numbered files."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = _UNSAFE.sub("-", label).strip("-") or "run"
    return Path(base) / f"{stamp}_{safe}"


def purge_old(base: Path | str = BASE_DIR, retention_days: int = 3) -> int:
    """Delete run directories (and stray files) last modified more than
    retention_days ago. Returns how many were removed."""
    base = Path(base)
    if not base.is_dir():
        return 0

    cutoff = time.time() - retention_days * 86400
    removed = 0
    for entry in base.iterdir():
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError as exc:
            log.warning("could not remove old screenshots at %s: %s", entry, exc)

    if removed:
        log.info("removed %d screenshot folder(s) older than %d days", removed, retention_days)
    return removed
